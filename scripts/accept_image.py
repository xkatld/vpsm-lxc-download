#!/usr/bin/env python3
"""Reimport and accept one exported image on its native GitHub runner.

The caller provisions default storage and a dual-stack incusbr0. This program
never downloads images, installs packages, repairs services, or changes the
shared network. Invoke with python3 -I; reports must stay outside the artifact.
"""
from __future__ import annotations

import argparse
import base64
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
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_pipeline import (  # noqa: E402
    COMMON_PACKAGES, NATIVE_ARCHITECTURES, README_CONTENT, RELEASE_DISPLAY,
    RUNNERS, ImageSpec,
)
from scripts.verify_artifacts import verify_artifact  # noqa: E402


class CheckError(RuntimeError):
    """Only code-owned messages may be used here, never guest/command output."""


class AcceptanceTerminated(RuntimeError):
    pass


def safe_error(exc: BaseException) -> str:
    # Never stringify external exceptions: argv/output can contain credentials.
    if isinstance(exc, CheckError):
        return str(exc)
    if isinstance(exc, (AcceptanceTerminated, KeyboardInterrupt)):
        return "acceptance interrupted"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "command timed out"
    if isinstance(exc, subprocess.CalledProcessError):
        return "command returned a nonzero exit status"
    if isinstance(exc, OSError):
        return "filesystem or executable operation failed"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "invalid artifact or runtime data"
    return "unexpected runtime failure"


def parse_os_release(text: str) -> dict[str, str]:
    """Parse assignments, never source guest-controlled shell code."""
    result = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, raw = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in result:
            raise CheckError("malformed os-release assignments")
        values = shlex.split(raw, comments=False, posix=True)
        if len(values) > 1:
            raise CheckError("malformed os-release value")
        result[key] = values[0] if values else ""
    return result


def check_os_release(text: str, spec: ImageSpec) -> None:
    values = parse_os_release(text)
    if values.get("ID") != spec.distro:
        raise CheckError("os-release distribution mismatch")
    version = values.get("VERSION_ID", "")
    codename = values.get("VERSION_CODENAME", "")
    if spec.distro == "debian":
        expected = RELEASE_DISPLAY["debian"][spec.release]
        # Development Debian may omit VERSION_ID, but must identify its codename.
        valid = bool(version or codename) and (not version or version == expected) and (
            not codename or codename == spec.release)
    elif spec.distro == "ubuntu":
        number = RELEASE_DISPLAY["ubuntu"][spec.release]
        valid = version == number[:2] + "." + number[2:] and (
            not codename or codename == spec.release)
    else:
        expected = spec.release.split("-", 1)[0]
        valid = bool(re.fullmatch(re.escape(expected) + r"(?:\.[0-9]+)*", version))
        if spec.distro == "centos":
            valid = valid and "CentOS Stream" in values.get("NAME", "")
    if not valid:
        raise CheckError("os-release version mismatch")


def import_paths(root: Path, spec: ImageSpec) -> list[Path]:
    """Verify the exact single-image artifact before selecting import arguments."""
    expected = {key: getattr(spec, key) for key in ("distro", "release", "architecture")}
    verify_artifact(root, expected)
    files = json.loads((root / "build-summary.json").read_text(encoding="utf-8"))["exported_files"]
    prefix = spec.container_name()
    # Both modern target + target.root and older .tar.* + .rootfs.tar.*
    # exports require metadata first, rootfs second, irrespective of list order.
    return [root / name for name in sorted(files, key=lambda name: (
        name[len(prefix):] == ".root" or name[len(prefix):].startswith(".rootfs."), name))]


def instance_addresses(text: str, name: str) -> dict[str, str]:
    rows = json.loads(text)
    matches = [row for row in rows if row.get("name") == name and row.get("status") == "Running"]
    if len(matches) != 1:
        return {}
    interface = (matches[0].get("state") or {}).get("network", {}).get("eth0", {})
    addresses = {}
    for entry in interface.get("addresses", []):
        raw = entry.get("address", "")
        if "%" in raw:
            continue
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if (address.is_loopback or address.is_unspecified or address.is_link_local
                or address.is_multicast or getattr(address, "ipv4_mapped", None)):
            continue
        family = "ipv4" if address.version == 4 else "ipv6"
        if entry.get("family") == ("inet" if address.version == 4 else "inet6"):
            addresses.setdefault(family, str(address))
    return addresses


def public_hostkey_fingerprints(text: str) -> dict[str, str]:
    """Hash decoded public blobs on the runner, ignoring guest key comments."""
    fingerprints = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            raise CheckError("malformed SSH host public key")
        algorithm = parts[0]
        if algorithm not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256",
                              "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"} or algorithm in fingerprints:
            raise CheckError("unsupported or duplicate SSH host public key type")
        try:
            blob = base64.b64decode(parts[1], validate=True)
        except ValueError:
            raise CheckError("malformed SSH host public key encoding") from None
        # Check the SSH wire-format type and fields, not the filename/comment.
        fields = []
        rest = blob
        while rest:
            if len(rest) < 4:
                raise CheckError("malformed SSH host public key blob")
            size = int.from_bytes(rest[:4], "big")
            if size == 0 or size > len(rest) - 4:
                raise CheckError("malformed SSH host public key blob")
            fields.append(rest[4:4 + size])
            rest = rest[4 + size:]
        valid = bool(fields) and fields[0] == algorithm.encode("ascii")
        if algorithm == "ssh-ed25519":
            valid = valid and len(fields) == 2 and len(fields[1]) == 32
        elif algorithm == "ssh-rsa":
            valid = valid and len(fields) == 3
        else:
            curve = algorithm.removeprefix("ecdsa-sha2-")
            valid = valid and len(fields) == 3 and fields[1] == curve.encode("ascii")
            valid = valid and len(fields[2]) == {"nistp256": 65, "nistp384": 97, "nistp521": 133}[curve]
        if not valid:
            raise CheckError("malformed SSH host public key fields")
        fingerprints[algorithm] = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    if not fingerprints:
        raise CheckError("no SSH host public keys found")
    return fingerprints


class Acceptance:
    READY_TIMEOUT = 120
    STAGES = (
        "report-directory", "preflight", "verify-export", "create-project", "profile-root",
        "profile-network", "import-image", "create-instance", "cold-start",
        "cold-network", "os-release", "packages", "ca-bundle", "readme", "root-lock",
        "credential-backups", "test-password", "cold-ssh-ipv4", "cold-ssh-ipv6", "cold-sshd-config",
        "cold-identity", "restart", "restart-network", "restart-ssh-ipv4", "restart-ssh-ipv6",
        "restart-sshd-config", "restart-identity", "identity-stability",
        "create-clone", "clone-start", "clone-network", "clone-root-lock", "clone-credential-backups",
        "clone-test-password", "clone-ssh-ipv4", "clone-ssh-ipv6", "clone-sshd-config",
        "clone-identity", "identity-uniqueness",
    )

    def __init__(self, spec: ImageSpec, output_dir: Path, report_dir: Path) -> None:
        self.spec = spec
        self.output_dir = output_dir.resolve()
        self.report_dir = report_dir.resolve()
        suffix = uuid.uuid4().hex
        self.project = "vpsm-accept-" + suffix
        self.instance = "accept-" + suffix
        self.clone_instance = "clone-" + suffix
        self.alias = "export-" + suffix
        self.project_created = False
        self.report_ready = False
        self.deadline: float | None = None
        self.password = secrets.token_urlsafe(32)
        self.secrets = [self.password, *(os.environ.get(k, "") for k in ("SSHPASS", "IMAGE_ROOT_PASSWORD"))]
        self.report = {
            "status": "failed", "started_at": datetime.now(timezone.utc).isoformat(),
            "steps": [{"stage": stage, "status": "NOT_RUN"} for stage in self.STAGES],
            "addresses": {},
            "identities": {},
            "limitations": [
                "Only controlled-network IPv4/IPv6 SSH is tested; public IPv6 and HTTPS are not tested.",
                "Credential backup and root login residue checks inspect the first boot before setting test credentials, not raw archives.",
            ],
        }

    def redact(self, text: str) -> str:
        for value in sorted(filter(None, self.secrets), key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return text

    def step(self, label: str, action: Callable):
        record = next((item for item in self.report["steps"] if item["stage"] == label), None)
        if record is None:
            record = {"stage": label, "status": "NOT_RUN"}
            self.report["steps"].append(record)
        print(f"[accept:{label}] running", flush=True)
        try:
            result = action()
        except (Exception, KeyboardInterrupt) as exc:
            record.update(status="FAIL", error=self.redact(safe_error(exc)))
            if "error" not in self.report:
                self.report["error"] = {"stage": label, "message": record["error"]}
            print(f"[accept:{label}] FAIL: {record['error']}", flush=True)
            raise
        record["status"] = "PASS"
        print(f"[accept:{label}] PASS", flush=True)
        return result

    def command(self, args: Sequence[str], *, input: str | None = None,
                timeout: float = 60, password: bool = False) -> subprocess.CompletedProcess:
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise CheckError("overall acceptance deadline exceeded")
            timeout = min(timeout, remaining)
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"IMAGE_ROOT_PASSWORD", "SSHPASS"}}
        if password:
            environment["SSHPASS"] = self.password
        return subprocess.run(list(args), input=input, text=True, capture_output=True,
                              check=True, timeout=timeout, env=environment)

    def incus(self, args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return self.command(["incus", "--project", self.project, *args], **kwargs)

    def guest(self, args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return self.incus(["exec", self.instance, "--", *args], **kwargs)

    def prepare_report(self) -> None:
        if self.report_dir == self.output_dir or self.output_dir in self.report_dir.parents:
            raise CheckError("report directory must be outside the image artifact directory")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            path = Path(summary).resolve()
            if path == self.output_dir or self.output_dir in path.parents:
                raise CheckError("GitHub summary must be outside the image artifact directory")
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.report_ready = True

    def preflight(self) -> None:
        spec = self.spec
        if spec.architecture not in RUNNERS or NATIVE_ARCHITECTURES.get(platform.machine().lower()) != spec.architecture:
            raise CheckError("a matching native architecture runner is required")
        valid_release = False
        if spec.distro in RELEASE_DISPLAY:
            valid_release = spec.release in RELEASE_DISPLAY[spec.distro]
        elif spec.distro == "alpine":
            valid_release = bool(re.fullmatch(r"3\.[0-9]+", spec.release))
        elif spec.distro == "almalinux":
            valid_release = spec.release in {"8", "9", "10"}
        if not valid_release:
            raise CheckError("unsupported distribution or release")
        if not math.isfinite(self.READY_TIMEOUT) or self.READY_TIMEOUT <= 0:
            raise CheckError("readiness timeout must be finite and positive")
        for executable in ("incus", "sshpass", "ssh"):
            if shutil.which(executable) is None:
                raise CheckError("required incus, sshpass or ssh executable is missing")
        self.report["image"] = {key: getattr(spec, key) for key in ("distro", "release", "architecture")}

    def create_project(self) -> None:
        self.command(["incus", "--project", "default", "project", "create", self.project,
                      "-c", "features.images=true", "-c", "features.profiles=true",
                      "-c", "features.networks=false"])
        # A failed create never authorizes deletion of a possibly unowned project.
        self.project_created = True

    def wait(self, probe: Callable[[float], object], error: str):
        deadline = time.monotonic() + self.READY_TIMEOUT
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                result = probe(min(10, remaining))
                if result and time.monotonic() <= deadline:
                    return result
            except (subprocess.SubprocessError, OSError):
                pass
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise CheckError(error)

    def network_ready(self, phase: str) -> dict[str, str]:
        def probe(timeout: float):
            addresses = instance_addresses(self.incus(
                ["list", self.instance, "--format", "json"], timeout=timeout).stdout, self.instance)
            return addresses if set(addresses) == {"ipv4", "ipv6"} else None
        addresses = self.wait(probe, "dual-stack readiness timed out (requires IPv4 and non-link-local IPv6 on eth0)")
        self.report["addresses"][phase] = addresses
        return addresses

    def check_packages(self) -> None:
        for package in COMMON_PACKAGES.split():
            if self.spec.distro == "alpine":
                self.guest(["apk", "info", "-e", package])
            elif self.spec.distro in {"debian", "ubuntu"}:
                result = self.guest(["dpkg-query", "-W", "-f=${Status}", package])
                if result.stdout.strip() != "install ok installed":
                    raise CheckError("required package is not installed in the package database")
            else:
                self.guest(["rpm", "-q", package])

    def check_readme(self) -> None:
        if self.guest(["cat", "/root/README.md"]).stdout != README_CONTENT + "\n":
            raise CheckError("image README does not exactly match builder content")

    def check_root_lock(self) -> None:
        # Shadow data is captured but never returned, logged or persisted.
        rows = [line.split(":") for line in self.guest(["cat", "/etc/shadow"]).stdout.splitlines()
                if line.split(":", 1)[0] == "root"]
        if len(rows) != 1 or len(rows[0]) != 9 or rows[0][1] != "!":
            raise CheckError("published root shadow password must be exactly !")

    def check_credential_backups(self) -> None:
        # Read-only path checks: do not print/read secrets, repair the image, or
        # reject normal boot-created /tmp entries. -L also catches dangling links.
        self.guest(["sh", "-c", """set -eu
for path in /etc/shadow- /etc/shadow~ /etc/shadow.bak \\
            /etc/gshadow- /etc/gshadow~ /etc/gshadow.bak \\
            /etc/passwd- /etc/passwd~ /etc/passwd.bak \\
            /etc/group- /etc/group~ /etc/group.bak \\
            /var/backups/shadow* /var/backups/gshadow* \\
            /var/backups/passwd* /var/backups/group* \\
            /root/.ssh /root/.bash_history /root/.ash_history /root/.zsh_history; do
    if [ -e "$path" ] || [ -L "$path" ]; then
        exit 1
    fi
done
"""])

    def ssh(self, family: int, address: str) -> None:
        parsed = ipaddress.ip_address(address)
        if family not in (4, 6) or parsed.version != family:
            raise CheckError("SSH address family mismatch")
        def probe(timeout: float):
            result = self.command([
                "sshpass", "-e", "ssh", f"-{family}", "-F", "/dev/null",
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5",
                "-o", "ConnectionAttempts=1", "-o", "NumberOfPasswordPrompts=1",
                "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
                "-o", "LogLevel=ERROR", f"root@{address}", "whoami",
            ], timeout=timeout, password=True)
            return result.stdout.strip() == "root"
        self.wait(probe, "SSH password authentication or root identity check timed out")

    def check_boot(self, phase: str, addresses: dict[str, str]) -> None:
        self.step(phase + "-ssh-ipv4", lambda: self.ssh(4, addresses["ipv4"]))
        self.step(phase + "-ssh-ipv6", lambda: self.ssh(6, addresses["ipv6"]))
        # Socket activation may create /run/sshd only upon the first connection.
        # Test the unmodified config after normal service/socket startup.
        self.step(phase + "-sshd-config", lambda: self.guest(["/usr/sbin/sshd", "-t"]))

    def sample_identity(self, phase: str) -> dict:
        # Read only runtime guest identities, never image properties or private keys.
        machine_id = self.guest(["cat", "/etc/machine-id"]).stdout
        if not re.fullmatch(r"[0-9a-f]{32}\n?", machine_id) or machine_id.rstrip("\n") == "0" * 32:
            raise CheckError("machine-id must be a nonzero lowercase 32-hex value")
        public_keys = self.guest(["sh", "-c", """set -eu
for path in /etc/ssh/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_rsa_key.pub /etc/ssh/ssh_host_ecdsa_key.pub; do
    if [ -e "$path" ] || [ -L "$path" ]; then
        cat "$path"
        printf '\\n'
    fi
done
"""]).stdout
        # The extra line separator allows key files without a trailing newline.
        keys = public_hostkey_fingerprints("\n".join(line for line in public_keys.splitlines() if line.strip()))
        identity = {
            "machine_id_sha256": hashlib.sha256(machine_id.rstrip("\n").encode("ascii")).hexdigest(),
            "ssh_host_key_fingerprints": keys,
        }
        self.report["identities"][phase] = identity
        return identity

    @staticmethod
    def check_identity_stability(initial: dict, restarted: dict) -> None:
        if initial != restarted:
            raise CheckError("machine-id or SSH host public keys changed across restart")

    @staticmethod
    def check_identity_uniqueness(primary: dict, clone: dict) -> None:
        if primary["machine_id_sha256"] == clone["machine_id_sha256"]:
            raise CheckError("clones share the same machine-id")
        first = primary["ssh_host_key_fingerprints"]
        second = clone["ssh_host_key_fingerprints"]
        if not first or first.keys() != second.keys():
            raise CheckError("clones must expose the same nonempty set of SSH host public key types")
        if set(first.values()) & set(second.values()):
            raise CheckError("clones share an SSH host public key")

    def check_clone(self, primary_identity: dict) -> None:
        primary_instance, primary_password = self.instance, self.password
        self.instance = self.clone_instance
        self.password = secrets.token_urlsafe(32)
        self.secrets.append(self.password)
        try:
            # Reuse the very same imported alias: never copy the mutated primary.
            self.step("create-clone", lambda: self.incus([
                "init", self.alias, self.instance, "--profile", "default", "-c", "security.privileged=false"], timeout=180))
            self.step("clone-start", lambda: self.incus(["start", self.instance], timeout=90))
            addresses = self.step("clone-network", lambda: self.network_ready("clone"))
            self.step("clone-root-lock", self.check_root_lock)
            self.step("clone-credential-backups", self.check_credential_backups)
            self.step("clone-test-password", lambda: self.guest(["chpasswd"], input=f"root:{self.password}\n"))
            self.check_boot("clone", addresses)
            identity = self.step("clone-identity", lambda: self.sample_identity("clone"))
            self.step("identity-uniqueness", lambda: self.check_identity_uniqueness(primary_identity, identity))
        finally:
            self.instance, self.password = primary_instance, primary_password

    def cleanup(self) -> bool:
        if not self.project_created:
            return True
        ok = True
        # Enumerate the OWN project to cover partially completed import/init calls.
        # Fingerprints can also exist globally; every deletion remains project-scoped.
        for resource, listing, key, delete in (
            ("instances", ["list", "--format", "json"], "name", ["delete"]),
            ("images", ["image", "list", "--format", "json"], "fingerprint", ["image", "delete"]),
            ("profiles", ["profile", "list", "--format", "json"], "name", ["profile", "delete"]),
        ):
            try:
                rows = self.step("cleanup-list-" + resource, lambda: json.loads(self.incus(listing, timeout=30).stdout))
                for index, row in enumerate(rows):
                    # Incus forbids deleting default; project deletion removes it.
                    if resource == "profiles" and row[key] == "default":
                        continue
                    args = [*delete, row[key]] + (["--force"] if resource == "instances" else [])
                    try:
                        self.step(f"cleanup-{resource}-{index}", lambda: self.incus(args, timeout=60))
                    except Exception:
                        ok = False
            except Exception:
                ok = False
        try:
            self.step("cleanup-project", lambda: self.incus(["project", "delete", self.project], timeout=60))
            self.project_created = False
        except Exception:
            ok = False
        return ok

    def write_report(self) -> None:
        self.report["finished_at"] = datetime.now(timezone.utc).isoformat()
        path = self.report_dir / "acceptance-report.json"
        path.write_text(self.redact(json.dumps(self.report, ensure_ascii=False, indent=2)) + "\n", encoding="utf-8")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            rows = ["## Exported image acceptance", "", f"Status: **{self.report['status']}**", "",
                    "| Stage | Result |", "|---|---|"]
            rows += [f"| {item['stage']} | {item['status']} |" for item in self.report["steps"]]
            if "error" in self.report:
                error = self.report["error"]
                rows += ["", f"Failure: {error['stage']}: {error['message']}"]
            rows += ["", *self.report["limitations"], ""]
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(self.redact("\n".join(rows)) + "\n")

    @staticmethod
    def interrupted(signum: int, frame: object) -> None:
        raise AcceptanceTerminated()

    def run(self) -> int:
        previous = signal.signal(signal.SIGTERM, self.interrupted)
        # Leave two minutes of a 20-minute CI step for bounded cleanup/reporting.
        self.deadline = time.monotonic() + 18 * 60
        try:
            self.step("report-directory", self.prepare_report)
            self.step("preflight", self.preflight)
            paths = self.step("verify-export", lambda: import_paths(self.output_dir, self.spec))
            self.step("create-project", self.create_project)
            self.step("profile-root", lambda: self.incus([
                "profile", "device", "add", "default", "root", "disk", "pool=default", "path=/"]))
            self.step("profile-network", lambda: self.incus([
                "profile", "device", "add", "default", "eth0", "nic", "network=incusbr0", "name=eth0"]))
            self.step("import-image", lambda: self.incus([
                "image", "import", *map(str, paths), "--alias", self.alias], timeout=300))
            self.step("create-instance", lambda: self.incus([
                "init", self.alias, self.instance, "--profile", "default", "-c", "security.privileged=false"], timeout=180))
            self.step("cold-start", lambda: self.incus(["start", self.instance], timeout=90))
            addresses = self.step("cold-network", lambda: self.network_ready("cold"))
            self.step("os-release", lambda: check_os_release(self.guest(["cat", "/etc/os-release"]).stdout, self.spec))
            self.step("packages", self.check_packages)
            ca_bundle = ("/etc/pki/tls/certs/ca-bundle.crt" if self.spec.distro in {"almalinux", "centos"}
                         else "/etc/ssl/certs/ca-certificates.crt")
            self.step("ca-bundle", lambda: self.guest(["test", "-s", ca_bundle]))
            self.step("readme", self.check_readme)
            self.step("root-lock", self.check_root_lock)
            self.step("credential-backups", self.check_credential_backups)
            self.step("test-password", lambda: self.guest(["chpasswd"], input=f"root:{self.password}\n"))
            self.check_boot("cold", addresses)
            initial_identity = self.step("cold-identity", lambda: self.sample_identity("cold"))
            self.step("restart", lambda: self.incus(["restart", self.instance, "--timeout", "60"], timeout=90))
            addresses = self.step("restart-network", lambda: self.network_ready("restart"))
            self.check_boot("restart", addresses)
            restarted_identity = self.step("restart-identity", lambda: self.sample_identity("restart"))
            self.step("identity-stability", lambda: self.check_identity_stability(initial_identity, restarted_identity))
            self.check_clone(initial_identity)
            self.report["status"] = "success"
        except (Exception, KeyboardInterrupt):
            pass  # step() already stored a safe, specifically labeled failure.
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                self.deadline = time.monotonic() + 90
                if not self.cleanup():
                    self.report["status"] = "failed"
                if self.report_ready:
                    try:
                        self.write_report()
                    except Exception:
                        self.report["status"] = "failed"
                        print("[accept:report] FAIL: could not write acceptance report/summary", flush=True)
            finally:
                signal.signal(signal.SIGTERM, previous)
                signal.signal(signal.SIGINT, old_int)
        return 0 if self.report["status"] == "success" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--report-dir", type=Path, default=Path("acceptance"))
    parser.add_argument("--distro", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--architecture", required=True, choices=tuple(RUNNERS))
    args = parser.parse_args(argv)
    spec = ImageSpec(args.distro, args.release, args.architecture, "default", "acceptance")
    return Acceptance(spec, args.output_dir, args.report_dir).run()


if __name__ == "__main__":
    raise SystemExit(main())
