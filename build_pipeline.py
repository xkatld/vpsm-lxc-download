#!/usr/bin/env python3
"""Build one native Incus image with common packages on a hosted Linux runner.

--matrix is read-only and needs neither Incus nor credentials. Build passwords
are random, temporary test credentials; published images have root locked.
Invoke through sudo --preserve-env when the runner needs elevated Incus access.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import json
import math
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

ARCHITECTURE = "arm64"  # Backwards-compatible load_manifest default only.
RUNNERS = {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
NATIVE_ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
COMMON_PACKAGES = "bash git unzip screen wget curl sudo nano ca-certificates"
README_CONTENT = "本系统为vpsm91.com构建,本文件无实际作用可随意处理。"
RELEASE_DISPLAY = {
    "debian": {"bookworm": "12", "forky": "14", "trixie": "13"},
    "ubuntu": {"jammy": "2204", "noble": "2404", "resolute": "2604"},
    "centos": {"9-Stream": "9-stream", "10-Stream": "10-stream"},
}


@dataclass(frozen=True)
class ImageSpec:
    distro: str
    release: str
    architecture: str
    variant: str
    build_date: str

    @property
    def image_alias(self) -> str:
        return f"{self.distro}-{self.release}".lower()

    @property
    def display_release(self) -> str:
        return RELEASE_DISPLAY.get(self.distro, {}).get(self.release, self.release)

    def container_name(self) -> str:
        release = self.display_release.replace(".", "")
        return f"{self.distro}{release}-{self.architecture}-lxc".lower()

    def remote_path(self) -> str:
        return f"images:{self.distro}/{self.release}/{self.architecture}"


def load_manifest(path: Path, architecture: str = ARCHITECTURE) -> list[ImageSpec]:
    """Read default container rows for the requested architecture."""
    if architecture not in RUNNERS:
        raise ValueError("unsupported manifest architecture")
    with path.open("r", encoding="utf-8", newline="") as handle:
        raw = csv.reader(handle, delimiter="\t")
        try:
            fields = [column.split(":", 1)[0].strip() for column in next(raw)]
        except StopIteration:
            raise ValueError("manifest is empty") from None
        required = {"Distribution", "Release", "Architecture", "Variant", "Build date", "Incus (container)"}
        if not required.issubset(fields):
            raise ValueError("manifest is missing required columns")
        specs = []
        seen = set()
        for number, row in enumerate(csv.DictReader(handle, fieldnames=fields, delimiter="\t"), 2):
            value = lambda key: (row.get(key) or "").strip()
            if value("Architecture") != architecture or value("Variant") != "default" or value("Incus (container)").upper() != "YES":
                continue
            values = [value(key) for key in ("Distribution", "Release", "Architecture", "Variant", "Build date")]
            if not all(values) or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item) for item in values[:4]):
                raise ValueError(f"invalid manifest row {number}")
            key = tuple(values[:4])
            if key in seen:
                raise ValueError(f"duplicate manifest row at line {number}")
            seen.add(key)
            specs.append(ImageSpec(*values))
    if not specs:
        raise ValueError(f"no {architecture}/default Incus container images found")
    return specs


def valid_export_suffixes(suffixes: set[str]) -> bool:
    """Accept exactly one complete unified or split Incus image export."""
    archive_suffixes = {".tar", ".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst"}
    unified = len(suffixes) == 1 and suffixes.issubset(archive_suffixes)
    split = suffixes == {"", ".root"}
    # Also tolerate named split archives from older CLI versions.
    named_split = any(suffixes == {ext, ".rootfs" + root_ext}
                      for ext in archive_suffixes for root_ext in archive_suffixes)
    return unified or split or named_split


def shell_quote(value: str) -> str:
    return shlex.quote(value)


class BuildTerminated(RuntimeError):
    pass


class IncusCommandError(RuntimeError):
    """Only sanitized external-command details, never argv/stdin/stdout."""


class SetupStepError(RuntimeError):
    """A labeled setup failure safe to print and persist."""


class Pipeline:
    def __init__(self, specs: Sequence[ImageSpec], output_dir: Path,
                 root_password: str | None = None, keep_resources: bool = False,
                 skip_ssh_test: bool = False, ssh_timeout: float = 120,
                 setup_diagnostics: bool = False) -> None:
        self.specs = list(specs)
        self.output_dir = output_dir
        self.root_password = root_password if root_password is not None else secrets.token_urlsafe(32)
        self.keep_resources = keep_resources
        self.skip_ssh_test = skip_ssh_test
        self.ssh_timeout = ssh_timeout
        self.setup_diagnostics = setup_diagnostics
        self.inherited_secrets = tuple(os.environ.get(key, "") for key in ("IMAGE_ROOT_PASSWORD", "SSHPASS"))
        self.run_id = uuid.uuid4().hex
        self.resource_prefix = f"vpsm-{self.run_id}-"
        self.created_containers: list[str] = []
        self.created_base_aliases: list[str] = []
        self.created_published_aliases: list[str] = []
        self.sanitized_containers: set[str] = set()
        self.exported_files: list[str] = []
        self.output_ready = False
        self.started_at = datetime.now(timezone.utc)

    def resource_name(self, name: str) -> str:
        # Full UUID scope, with a bounded readable suffix for Incus' name limit.
        suffix = name if len(self.resource_prefix + name) <= 63 else name[:16] + "-" + hashlib.sha256(name.encode()).hexdigest()[:8]
        return self.resource_prefix + suffix

    def container_name(self, spec: ImageSpec) -> str:
        return self.resource_name(spec.container_name())

    def base_alias(self, spec: ImageSpec) -> str:
        return self.resource_name(f"{spec.image_alias}-{spec.architecture}")

    @property
    def containers(self) -> list[str]:
        return [self.container_name(spec) for spec in self.specs]

    @property
    def base_aliases(self) -> list[str]:
        return [self.base_alias(spec) for spec in self.specs]

    @property
    def published_aliases(self) -> list[str]:
        return self.containers

    def safe_error(self, exc: BaseException) -> str:
        # Never stringify subprocess exceptions: their command/output can carry secrets.
        if isinstance(exc, subprocess.TimeoutExpired):
            return "external command timed out"
        if isinstance(exc, subprocess.CalledProcessError):
            return f"external command failed (exit {exc.returncode})"
        if isinstance(exc, OSError):
            return "operating system operation failed"
        return self.redact_secrets(str(exc))

    def redact_secrets(self, text: str) -> str:
        values = {self.root_password, *self.inherited_secrets,
                  *(os.environ.get(key, "") for key in ("IMAGE_ROOT_PASSWORD", "SSHPASS"))}
        # Longest first handles overlapping credentials; redact before truncation.
        for value in sorted(filter(None, values), key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return text

    def safe_setup_stderr(self, stderr: str | bytes | None) -> str:
        if not stderr:
            return ""
        if isinstance(stderr, bytes):  # TimeoutExpired may carry bytes even in text mode.
            stderr = stderr.decode("utf-8", errors="replace")
        text = self.redact_secrets(stderr)
        # Escape terminal controls (including CR, ESC, Unicode bidi/line controls).
        # Only LF survives; prefix EVERY line and neutralize workflow command
        # markers too (some CI parsers search for them anywhere in the line).
        text = "".join(c if c == "\n" or c.isprintable() else ascii(c)[1:-1] for c in text)
        text = text.replace("::", r"\x3a\x3a").replace("##[", r"\x23\x23[")
        lines = text.split("\n")
        rendered = "\n".join("  setup stderr | " + line for line in lines[:12])
        if len(rendered) > 2048 or len(lines) > 12:
            rendered = rendered[:2048] + "\n  setup stderr | [truncated]"
        return rendered

    @staticmethod
    def command_environment() -> dict[str, str]:
        # Workflow may preserve a legacy temporary password through sudo. This
        # pipeline generates its own and must not forward inherited credentials.
        return {key: value for key, value in os.environ.items()
                if key not in {"IMAGE_ROOT_PASSWORD", "SSHPASS"}}

    def incus(self, args: Sequence[str], *, capture: bool = False,
              input: str | None = None, timeout: float | None = None,
              diagnostic_stderr: bool = False) -> subprocess.CompletedProcess[str]:
        # Always capture: neither a failing command's output nor its argv is logged.
        try:
            return subprocess.run(["incus", *args], input=input, check=True, text=True,
                                  capture_output=True, env=self.command_environment(),
                                  timeout=timeout if timeout is not None else 1800)
        except (OSError, subprocess.SubprocessError) as exc:
            error = self.safe_error(exc)
            if diagnostic_stderr and self.setup_diagnostics and input is None:
                detail = self.safe_setup_stderr(getattr(exc, "stderr", None))
                if detail:
                    error += "\n" + detail
            raise IncusCommandError(error) from None

    def exec_container(self, container: str, command: str, *, quiet: bool = False) -> subprocess.CompletedProcess[str]:
        if not quiet:
            print(f"  container: {container}")
        return self.incus(["exec", container, "--", "sh", "-c", command], capture=True)

    def preflight(self) -> None:
        if len(self.specs) != 1:
            raise ValueError("each build must select exactly one manifest row")
        spec = self.specs[0]
        native = NATIVE_ARCHITECTURES.get(platform.machine().lower())
        if spec.architecture not in RUNNERS or native != spec.architecture:
            raise RuntimeError(f"a native {spec.architecture} runner is required")
        if spec.distro not in {"almalinux", "alpine", "centos", "debian", "ubuntu"}:
            raise ValueError("unsupported distro")
        if not self.root_password or any(c in self.root_password for c in "\n\r\x00"):
            raise ValueError("invalid temporary test password")
        if not math.isfinite(self.ssh_timeout) or self.ssh_timeout <= 0:
            raise ValueError("SSH timeout must be finite and positive")
        if self.output_dir.is_symlink() or (self.output_dir.exists() and
                (not self.output_dir.is_dir() or any(self.output_dir.iterdir()))):
            raise RuntimeError("output directory must be absent or empty")
        for executable in (("incus",) if self.skip_ssh_test else ("incus", "sshpass", "ssh")):
            if shutil.which(executable) is None:
                raise RuntimeError(f"required executable not found: {executable}")
        self.incus(["version"])

    def download_images(self) -> None:
        print("[1/8] Downloading base image")
        for spec in self.specs:
            alias = self.base_alias(spec)
            self.incus(["image", "copy", spec.remote_path(), "local:", "--alias", alias, "--quiet"])
            self.created_base_aliases.append(alias)

    def launch_containers(self) -> None:
        print("[2/8] Creating and starting containers")
        for spec in self.specs:
            name = self.container_name(spec)
            # init separates successful creation from a potentially failed start.
            self.incus(["init", self.base_alias(spec), name])
            self.created_containers.append(name)
            self.incus(["start", name])
            self.wait_for_container(name)

    def wait_for_container(self, container: str) -> None:
        deadline = time.monotonic() + self.ssh_timeout
        while time.monotonic() < deadline:
            try:
                self.incus(["exec", container, "--", "true"], timeout=min(10, max(.1, deadline - time.monotonic())))
                return
            except BuildTerminated:
                raise
            except RuntimeError:
                time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise RuntimeError("container startup timed out")

    @staticmethod
    def ssh_config_command() -> str:
        # OpenSSH uses the first value; override image includes without editing them.
        return """set -eu
config=/etc/ssh/sshd_config
{ printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n'; cat "$config"; } > "$config.vpsm"
chmod 600 "$config.vpsm"
mv "$config.vpsm" "$config"
"""

    def setup_step(self, container: str, label: str, command: str | None = None,
                   *, timeout: float | None = None) -> None:
        # Labels/commands are code-owned, never derived from captured output.
        # The only credential-bearing operation has no diagnostic opt-in.
        try:
            if command is None:
                self.incus(["exec", container, "--", "chpasswd"],
                           input=f"root:{self.root_password}\n")
            else:
                self.incus(["exec", container, "--", "sh", "-c", command],
                           timeout=timeout, diagnostic_stderr=True)
        except IncusCommandError as exc:
            raise SetupStepError(f"setup step [{label}] failed: {exc}") from None

    def wait_for_package_index(self, container: str, command: str) -> None:
        deadline = time.monotonic() + self.ssh_timeout
        last_error = "no attempt completed"
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                self.setup_step(container, "package-index", command, timeout=min(30, remaining))
            except SetupStepError as exc:
                last_error = str(exc)  # Already sanitized by incus; never raw subprocess text.
            else:
                if time.monotonic() <= deadline:
                    return
                last_error = "package-index command completed after deadline"
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(2, remaining))
        raise SetupStepError(f"setup step [package-index] readiness timed out: {last_error}") from None

    def setup_one(self, spec: ImageSpec, container: str) -> None:
        prefix = "set -eu\n"
        if spec.distro == "alpine":
            index, install = "apk update", "apk add openssh"
            services = [("ssh-enable", "rc-update add sshd"), ("ssh-restart", "rc-service sshd restart")]
        elif spec.distro in {"debian", "ubuntu"}:
            prefix += "export DEBIAN_FRONTEND=noninteractive\n"
            index, install = "apt-get -o APT::Update::Error-Mode=any update", "apt-get install -y openssh-server"
            services = [("ssh-restart", "(service ssh restart || systemctl restart ssh)")]
        elif spec.distro in {"almalinux", "centos"}:
            index, install = "dnf makecache", "dnf install -y openssh-server"
            services = [("ssh-enable", "systemctl enable sshd"), ("ssh-restart", "systemctl restart sshd")]
        else:
            raise ValueError("unsupported distro in setup stage")
        print(f"  container: {container}")
        # Retry only the idempotent index refresh, not installation or SSH/password setup.
        self.wait_for_package_index(container, prefix + index)
        for label, command in [
            ("ssh-install", install), ("ssh-host-keys", "ssh-keygen -A"),
            ("ssh-config", self.ssh_config_command()), *services,
        ]:
            self.setup_step(container, label, prefix + command)
        self.setup_step(container, "chpasswd")

    def setup_containers(self) -> None:
        print("[3/8] Installing and configuring SSH")
        for spec in self.specs:
            self.setup_one(spec, self.container_name(spec))

    @staticmethod
    def package_install_command(spec: ImageSpec) -> str:
        if spec.distro == "alpine":
            return (f"set -eu\napk add {COMMON_PACKAGES}\n"
                    "update-ca-certificates\ntest -s /etc/ssl/certs/ca-certificates.crt")
        if spec.distro in {"debian", "ubuntu"}:
            return (f"set -eu\nexport DEBIAN_FRONTEND=noninteractive\napt-get update\napt-get install -y {COMMON_PACKAGES}\n"
                    "update-ca-certificates\ntest -s /etc/ssl/certs/ca-certificates.crt")
        if spec.distro in {"almalinux", "centos"}:
            major = spec.release.split("-", 1)[0].split(".", 1)[0]
            if major not in {"8", "9", "10"}:
                raise ValueError("unsupported Enterprise Linux version")
            repo = "powertools" if major == "8" else "crb"
            epel = (f"https://dl.fedoraproject.org/pub/epel/epel-release-latest-{major}.noarch.rpm"
                    if spec.distro == "centos" else "epel-release")
            return (f"set -eu\ndnf install -y dnf-plugins-core\ndnf config-manager --set-enabled {repo}\n"
                    f"dnf install -y {epel}\ndnf install -y {COMMON_PACKAGES}\n"
                    "update-ca-trust extract\ntest -s /etc/pki/tls/certs/ca-bundle.crt")
        raise ValueError("unsupported distro in package stage")

    def install_common_packages(self) -> None:
        print("[4/8] Installing common packages")
        for spec in self.specs:
            self.exec_container(self.container_name(spec), self.package_install_command(spec))

    def write_readmes(self) -> None:
        print("[5/8] Writing image README files")
        for container in self.containers:
            self.exec_container(container, f"printf '%s\\n' {shell_quote(README_CONTENT)} > /root/README.md", quiet=True)

    @staticmethod
    def extract_ip(value: str) -> str | None:
        for candidate in re.findall(r"[0-9a-fA-F:.]+", value):
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if address.version == 4 and not address.is_loopback and not address.is_unspecified and not address.is_link_local:
                return str(address)
        return None

    def container_ip(self, container: str, *, timeout: float = 10) -> str | None:
        result = self.incus(["list", container, "--format", "csv", "-c", "4"], capture=True, timeout=timeout)
        return self.extract_ip(result.stdout)

    def test_ssh(self) -> None:
        if self.skip_ssh_test:
            print("[6/8] Skipping SSH test")
            return
        print("[6/8] Waiting for SSH and testing temporary credentials")
        environment = self.command_environment()
        environment["SSHPASS"] = self.root_password
        for container in self.containers:
            deadline = time.monotonic() + self.ssh_timeout
            while time.monotonic() < deadline:
                try:
                    ip = self.container_ip(container, timeout=min(10, max(.1, deadline - time.monotonic())))
                    if ip:
                        result = subprocess.run([
                            "sshpass", "-e", "ssh", "-F", "/dev/null", "-o", "StrictHostKeyChecking=no",
                            "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5",
                            "-o", "ConnectionAttempts=1", "-o", "NumberOfPasswordPrompts=1",
                            "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
                            f"root@{ip}", "whoami"], text=True, capture_output=True, env=environment,
                            timeout=min(10, max(.1, deadline - time.monotonic())))
                        if result.returncode == 0 and result.stdout.strip() == "root":
                            break
                except BuildTerminated:
                    raise
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    pass  # Retry without exposing remote output or credential-bearing errors.
                time.sleep(min(2, max(0, deadline - time.monotonic())))
            else:
                raise RuntimeError(f"SSH readiness/authentication timed out: {container}")
            print(f"  {container}: OK")

    def cleanup_containers(self) -> None:
        print("[7/8] Removing test credentials and caches; locking root")
        common = """set -eu
printf 'root:!\\n' | chpasswd -e
# Remove password backups as well as login/test state; do not leave a locked hash.
rm -f /etc/shadow- /etc/shadow~ /etc/shadow.bak /var/backups/shadow*
rm -rf /root/.ssh /tmp/* /var/tmp/*
rm -f /root/.bash_history /root/.ash_history /root/.zsh_history
# Verify the published shadow entry is exactly a non-password lock marker.
awk -F: '$1 == "root" { found=1; if ($2 != "!") exit 1 } END { if (!found) exit 1 }' /etc/shadow
"""
        for spec in self.specs:
            if spec.distro == "alpine":
                package_cleanup = "apk cache clean"
            elif spec.distro in {"debian", "ubuntu"}:
                package_cleanup = "apt-get clean && rm -rf /var/lib/apt/lists/*"
            else:
                package_cleanup = "dnf clean all && rm -rf /var/cache/dnf"
            container = self.container_name(spec)
            self.exec_container(container, common + "\n" + package_cleanup, quiet=True)
            self.sanitized_containers.add(container)

    def write_checksums(self) -> None:
        lines = []
        for name in self.exported_files:
            path = self.output_dir / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError("exported image is missing or empty")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            lines.append(f"{digest.hexdigest()}  {name}\n")
        (self.output_dir / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")

    def publish_and_export(self) -> None:
        print("[8/8] Publishing and exporting images")
        for spec in self.specs:
            container = self.container_name(spec)
            if container not in self.sanitized_containers:
                raise RuntimeError("refusing to publish before test credentials are removed")
            self.incus(["stop", container, "--force"])
            self.incus(["publish", container, "--alias", container])
            self.created_published_aliases.append(container)
            output_name = spec.container_name()
            # Discover each export in isolation, never by globbing the output directory.
            with tempfile.TemporaryDirectory(prefix=".export-", dir=self.output_dir) as directory:
                target = Path(directory) / output_name
                self.incus(["image", "export", container, str(target)])
                files = sorted(Path(directory).iterdir())
                if not files or any(path.is_symlink() or not path.is_file() or path.stat().st_size == 0
                                    or not (path.name == output_name or path.name.startswith(output_name + ".")) for path in files):
                    raise RuntimeError(f"missing, empty or invalid export: {output_name}")
                # Incus explicit-target exports: unified target.tar.* OR split
                # target + target.root. A lone extensionless metadata file is
                # not a complete image. Keep the actual filenames, no guesses.
                suffixes = {path.name[len(output_name):] for path in files}
                if not valid_export_suffixes(suffixes):
                    raise RuntimeError(f"unexpected export file set: {output_name}")
                for path in files:
                    destination = self.output_dir / path.name
                    if destination.exists() or destination.is_symlink():
                        raise RuntimeError("refusing to overwrite an existing artifact")
                    # Link is atomic and refuses overwrite; temp and output share a filesystem.
                    os.link(path, destination)
                    self.exported_files.append(path.name)
            self.exported_files.sort()
            self.write_checksums()

    def final_cleanup(self) -> None:
        if self.keep_resources:
            print("Keeping created Incus resources (--keep-resources)")
            return
        # No predicted names and no fingerprint deletion: copied/published blobs can be shared.
        for name in reversed(self.created_containers[:]):
            try:
                self.incus(["delete", name, "--force"], timeout=60)
                self.created_containers.remove(name)
            except Exception:
                print("WARNING: temporary container cleanup failed", file=sys.stderr)
        for aliases in (self.created_published_aliases, self.created_base_aliases):
            for alias in reversed(aliases[:]):
                try:
                    self.incus(["image", "alias", "delete", alias], timeout=60)
                    aliases.remove(alias)
                except Exception:
                    print("WARNING: temporary image alias cleanup failed", file=sys.stderr)

    def write_summary(self, status: str, error: str | None = None) -> None:
        payload = {
            "status": status,
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "architecture": self.specs[0].architecture,
            "images": [asdict(spec) | {"image_alias": self.base_alias(spec)} for spec in self.specs],
            "exported_files": [name for name in self.exported_files if (self.output_dir / name).is_file()],
        }
        if error:
            payload["error"] = self.redact_secrets(error)
        (self.output_dir / "build-summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_file:
            with open(summary_file, "a", encoding="utf-8") as handle:
                handle.write(f"## Incus image build\n\n- Status: **{status}**\n- Architecture: **{self.specs[0].architecture}**\n")
                for name in payload["exported_files"]:
                    handle.write(f"- `{name}`\n")

    @staticmethod
    def _handle_sigterm(signum: int, frame: object) -> None:
        raise BuildTerminated("build interrupted by SIGTERM")

    def run(self) -> None:
        status = "failed"
        error = None
        ready = False
        previous_handler = signal.signal(signal.SIGTERM, self._handle_sigterm)
        try:
            self.preflight()
            # Preflight rejection must neither clean Incus resources nor touch artifacts.
            ready = True
            self.output_dir.mkdir(parents=True, exist_ok=True)
            if any(self.output_dir.iterdir()):
                raise RuntimeError("output directory must be empty")
            self.output_ready = True
            self.download_images()
            self.launch_containers()
            self.setup_containers()
            self.install_common_packages()
            self.write_readmes()
            self.test_ssh()
            self.cleanup_containers()
            self.publish_and_export()
            status = "success"
        except (Exception, KeyboardInterrupt) as exc:
            error = self.safe_error(exc)
            if isinstance(exc, SetupStepError):
                print(error, file=sys.stderr)
            raise RuntimeError(error) from None
        finally:
            # A second TERM cannot interrupt best-effort bounded cleanup. Hosted VM
            # destruction is the final backstop for SIGKILL or timed-out Incus calls.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                if ready:
                    self.final_cleanup()
                if self.output_ready:
                    try:
                        self.write_summary(status, error)
                    except Exception:
                        if error is None:
                            raise RuntimeError("could not write build summary") from None
                        print("WARNING: could not write failure summary", file=sys.stderr)
            finally:
                signal.signal(signal.SIGTERM, previous_handler)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("镜像.md"))
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument("--architecture", choices=tuple(RUNNERS))
    parser.add_argument("--distro")
    parser.add_argument("--release")
    parser.add_argument("--keep-resources", action="store_true")
    parser.add_argument("--skip-ssh-test", action="store_true")
    parser.add_argument("--ssh-timeout", type=float, default=120)
    parser.add_argument("--setup-diagnostics", action="store_true",
                        help="include bounded, redacted stderr for credential-free setup failures")
    args = parser.parse_args(argv)
    if not args.matrix and not all((args.architecture, args.distro, args.release)):
        parser.error("build requires --architecture, --distro and --release")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.matrix:
            include = [{"distro": spec.distro, "release": spec.release, "architecture": architecture, "runner": runner}
                       for architecture, runner in RUNNERS.items() for spec in load_manifest(args.manifest, architecture)]
            print(json.dumps({"include": include}, separators=(",", ":")))
            return 0
        specs = [spec for spec in load_manifest(args.manifest, args.architecture)
                 if spec.distro == args.distro and spec.release == args.release]
        if len(specs) != 1:
            raise ValueError("selection must match exactly one manifest row")
        # Never use a GitHub Secret as a published (or test) image password.
        Pipeline(specs, args.output_dir, keep_resources=args.keep_resources,
                 skip_ssh_test=args.skip_ssh_test, ssh_timeout=args.ssh_timeout,
                 setup_diagnostics=args.setup_diagnostics).run()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        # Detailed sanitized errors are in build-summary.json when a build started.
        print("Build failed; see build-summary.json if created (no credentials logged).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
