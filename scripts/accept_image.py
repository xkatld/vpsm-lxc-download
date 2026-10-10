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
        return "验收已中断"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "命令执行超时"
    if isinstance(exc, subprocess.CalledProcessError):
        return f"命令执行失败，退出码：{exc.returncode}"
    if isinstance(exc, OSError):
        return "文件或可执行程序操作失败"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "产物或运行数据格式无效"
    return "发生未预期的运行错误"


def parse_os_release(text: str) -> dict[str, str]:
    """Parse assignments, never source guest-controlled shell code."""
    result = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, raw = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in result:
            raise CheckError("系统版本文件的字段格式错误")
        values = shlex.split(raw, comments=False, posix=True)
        if len(values) > 1:
            raise CheckError("系统版本文件的字段值无效")
        result[key] = values[0] if values else ""
    return result


def check_os_release(text: str, spec: ImageSpec) -> None:
    values = parse_os_release(text)
    if values.get("ID") != spec.distro:
        raise CheckError("镜像发行版不匹配")
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
        raise CheckError("镜像版本不匹配")


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
            raise CheckError("SSH 主机公钥格式无效")
        algorithm = parts[0]
        if algorithm not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256",
                              "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"} or algorithm in fingerprints:
            raise CheckError("SSH 主机公钥类型不支持或重复")
        try:
            blob = base64.b64decode(parts[1], validate=True)
        except ValueError:
            raise CheckError("SSH 主机公钥编码无效") from None
        # Check the SSH wire-format type and fields, not the filename/comment.
        fields = []
        rest = blob
        while rest:
            if len(rest) < 4:
                raise CheckError("SSH 主机公钥二进制数据无效")
            size = int.from_bytes(rest[:4], "big")
            if size == 0 or size > len(rest) - 4:
                raise CheckError("SSH 主机公钥二进制数据无效")
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
            raise CheckError("SSH 主机公钥字段无效")
        fingerprints[algorithm] = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    if not fingerprints:
        raise CheckError("未发现 SSH 主机公钥")
    return fingerprints


class Acceptance:
    READY_TIMEOUT = 120
    STAGE_NAMES = {
        "report-directory": "准备验收报告目录", "preflight": "检查运行环境",
        "verify-export": "校验导出产物", "create-project": "创建隔离验收项目",
        "profile-root": "配置验收存储", "profile-network": "配置双栈网络",
        "import-image": "重新导入镜像", "create-instance": "创建验收容器",
        "os-release": "检查发行版与版本", "packages": "检查预装软件包",
        "ca-bundle": "检查证书信任库", "readme": "检查镜像说明",
        "root-lock": "检查初始密码锁定", "credential-backups": "检查账户备份及登录残留",
        "test-password": "设置验收密码", "restart": "重启验收容器",
        "identity-stability": "检查重启前后身份不变", "create-clone": "创建独立克隆",
        "identity-uniqueness": "检查克隆身份不同", "cleanup-project": "清理隔离验收项目",
        "start": "启动容器", "network": "等待双栈地址",
        "ssh-ipv4": "验证 IPv4 SSH 登录", "ssh-ipv6": "验证 IPv6 SSH 登录",
        "sshd-config": "检查 SSH 服务配置", "identity": "采集实例身份摘要",
    }
    STATUS_NAMES = {"PASS": "通过", "FAIL": "失败", "NOT_RUN": "未执行",
                    "success": "通过", "failed": "失败"}
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
                "仅验收内网 IPv4 与 IPv6 SSH，公网 IPv6 和 HTTPS 不属于本次验收范围。",
                "账户备份与登录残留检查在首次启动后、设置验收密码前执行，不等同于原始归档检查。",
            ],
        }

    @classmethod
    def stage_name(cls, label: str) -> str:
        if label in cls.STAGE_NAMES:
            return cls.STAGE_NAMES[label]
        for prefix, name in (("cold-", "首次启动"), ("restart-", "重启后"), ("clone-", "独立克隆")):
            if label.startswith(prefix):
                return name + "：" + cls.STAGE_NAMES.get(label[len(prefix):], "附加检查")
        for resource, name in (("instances", "容器"), ("images", "镜像"), ("profiles", "配置")):
            if label == "cleanup-list-" + resource:
                return "列出待清理" + name
            if label.startswith("cleanup-" + resource + "-"):
                return "清理验收" + name + " " + label.rsplit("-", 1)[-1]
        return "附加检查"

    def redact(self, text: str) -> str:
        for value in sorted(filter(None, self.secrets), key=len, reverse=True):
            text = text.replace(value, "[已隐藏]")
        text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)",
                      "[已隐藏私钥]", text, flags=re.S)
        text = re.sub(r"\$(?:[1256]|y|gy|2[aby])\$[^\s:]+", "[已隐藏密码摘要]", text)
        text = re.sub(r"(?im)(password|token|secret|authorization)(\s*[:=]\s*)[^\s,;]+",
                      r"\1\2[已隐藏]", text)
        return text

    def diagnostic_text(self, text: str) -> str:
        text = self.redact(text)
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        text = "".join(char if char.isprintable() or char in "\n\t" else " " for char in text)
        return text.replace("::", "：：")[:4096]

    def step(self, label: str, action: Callable):
        record = next((item for item in self.report["steps"] if item["stage"] == label), None)
        if record is None:
            record = {"stage": label, "status": "NOT_RUN"}
            self.report["steps"].append(record)
        record["name"] = self.stage_name(label)
        print(f"[验收：{record['name']}] 执行中", flush=True)
        try:
            result = action()
        except (Exception, KeyboardInterrupt) as exc:
            record.update(status="FAIL", error=self.redact(safe_error(exc)))
            if "error" not in self.report:
                self.report["error"] = {"stage": label, "message": record["error"]}
            print(f"[验收：{record['name']}] 失败：{record['error']}", flush=True)
            raise
        record["status"] = "PASS"
        print(f"[验收：{record['name']}] 通过", flush=True)
        return result

    def command(self, args: Sequence[str], *, input: str | None = None,
                timeout: float = 60, password: bool = False,
                diagnose: bool = False) -> subprocess.CompletedProcess:
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise CheckError("验收总时限已到")
            timeout = min(timeout, remaining)
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"IMAGE_ROOT_PASSWORD", "SSHPASS"}}
        if password:
            environment["SSHPASS"] = self.password
        try:
            return subprocess.run(list(args), input=input, text=True, capture_output=True,
                                  check=True, timeout=timeout, env=environment)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            if diagnose and not password and input is None:
                details = exc.stderr or exc.stdout or ""
                if isinstance(details, bytes):
                    details = details.decode("utf-8", errors="replace")
                message = safe_error(exc)
                if details:
                    message += "：" + self.diagnostic_text(details)
                raise CheckError(message) from None
            raise

    def incus(self, args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return self.command(["incus", "--project", self.project, *args], **kwargs)

    def guest(self, args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return self.incus(["exec", self.instance, "--", *args], **kwargs)

    def prepare_report(self) -> None:
        if self.report_dir == self.output_dir or self.output_dir in self.report_dir.parents:
            raise CheckError("验收报告目录不能放在镜像产物目录内")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            path = Path(summary).resolve()
            if path == self.output_dir or self.output_dir in path.parents:
                raise CheckError("工作流摘要不能放在镜像产物目录内")
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.report_ready = True

    def preflight(self) -> None:
        spec = self.spec
        if spec.architecture not in RUNNERS or NATIVE_ARCHITECTURES.get(platform.machine().lower()) != spec.architecture:
            raise CheckError("必须使用与镜像架构匹配的原生运行器")
        valid_release = False
        if spec.distro in RELEASE_DISPLAY:
            valid_release = spec.release in RELEASE_DISPLAY[spec.distro]
        elif spec.distro == "alpine":
            valid_release = bool(re.fullmatch(r"3\.[0-9]+", spec.release))
        elif spec.distro == "almalinux":
            valid_release = spec.release in {"8", "9", "10"}
        if not valid_release:
            raise CheckError("不支持此发行版或版本")
        if not math.isfinite(self.READY_TIMEOUT) or self.READY_TIMEOUT <= 0:
            raise CheckError("就绪等待时限必须是有限正数")
        for executable in ("incus", "sshpass", "ssh"):
            if shutil.which(executable) is None:
                raise CheckError("缺少必需的 incus、sshpass 或 ssh 程序")
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
        addresses = self.wait(probe, "双栈地址等待超时，eth0 必须同时具有 IPv4 和非链路本地 IPv6 地址")
        self.report["addresses"][phase] = addresses
        return addresses

    def check_packages(self) -> None:
        for package in COMMON_PACKAGES.split():
            if self.spec.distro == "alpine":
                self.guest(["apk", "info", "-e", package])
            elif self.spec.distro in {"debian", "ubuntu"}:
                result = self.guest(["dpkg-query", "-W", "-f=${Status}", package])
                if result.stdout.strip() != "install ok installed":
                    raise CheckError("必需软件包未处于已安装状态")
            else:
                self.guest(["rpm", "-q", package])

    def check_readme(self) -> None:
        if self.guest(["cat", "/root/README.md"]).stdout != README_CONTENT + "\n":
            raise CheckError("镜像说明与构建器约定内容不一致")

    def check_root_lock(self) -> None:
        # Shadow data is captured but never returned, logged or persisted.
        rows = [line.split(":") for line in self.guest(["cat", "/etc/shadow"]).stdout.splitlines()
                if line.split(":", 1)[0] == "root"]
        if len(rows) != 1 or len(rows[0]) != 9 or rows[0][1] != "!":
            raise CheckError("发布镜像的 root 密码锁定字段必须精确为 !")

    def check_credential_backups(self) -> None:
        result = self.guest(["sh", "-c", """set -eu
for path in /etc/shadow- /etc/shadow~ /etc/shadow.bak \\
            /etc/gshadow- /etc/gshadow~ /etc/gshadow.bak \\
            /etc/passwd- /etc/passwd~ /etc/passwd.bak \\
            /etc/group- /etc/group~ /etc/group.bak \\
            /var/backups/shadow* /var/backups/gshadow* \\
            /var/backups/passwd* /var/backups/group* \\
            /root/.ssh /root/.bash_history /root/.ash_history /root/.zsh_history; do
    if [ -e "$path" ] || [ -L "$path" ]; then
        printf '%s\\n' "$path"
    fi
done
"""])
        paths = result.stdout.splitlines()
        if paths:
            self.report.setdefault("residue", {})[self.instance] = paths[:64]
            names = self.diagnostic_text("、".join(paths))
            raise CheckError("发现账户备份或登录残留：" + names)

    def ssh(self, family: int, address: str) -> None:
        parsed = ipaddress.ip_address(address)
        if family not in (4, 6) or parsed.version != family:
            raise CheckError("SSH 连接地址族不匹配")
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
        self.wait(probe, "SSH 密码登录或 root 身份检查超时")

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
            raise CheckError("机器标识必须是非全零的 32 位小写十六进制值")
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
            raise CheckError("重启前后机器标识或 SSH 主机公钥发生变化")

    @staticmethod
    def check_identity_uniqueness(primary: dict, clone: dict) -> None:
        if primary["machine_id_sha256"] == clone["machine_id_sha256"]:
            raise CheckError("独立克隆使用了相同的机器标识")
        first = primary["ssh_host_key_fingerprints"]
        second = clone["ssh_host_key_fingerprints"]
        if not first or first.keys() != second.keys():
            raise CheckError("独立克隆必须提供相同且非空的 SSH 主机公钥类型集合")
        if set(first.values()) & set(second.values()):
            raise CheckError("独立克隆使用了相同的 SSH 主机公钥")

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

    def collect_diagnostics(self) -> None:
        if not self.project_created:
            return
        probes = [("验收项目资源", ["list", "--format", "json"])]
        for instance in (self.instance, self.clone_instance):
            probes.append((instance + " 启动日志", ["info", instance, "--show-log"]))
            probes.append((instance + " 账户路径元数据", ["exec", instance, "--", "sh", "-c", """
for path in /etc/shadow /etc/gshadow /etc/passwd /etc/group \\
            /etc/shadow- /etc/gshadow- /etc/passwd- /etc/group- \\
            /var/backups/shadow* /var/backups/gshadow* /var/backups/passwd* /var/backups/group* \\
            /root/.ssh /root/.bash_history /root/.ash_history /root/.zsh_history; do
    if [ -e "$path" ] || [ -L "$path" ]; then
        stat -c '%n | %F | %s | %y | %z' -- "$path" || exit 1
    fi
done
"""]))
            if self.spec.distro != "alpine":
                probes.append((instance + " 首次启动服务日志", ["exec", instance, "--", "journalctl",
                    "-b", "--no-pager", "-n", "60", "-u", "systemd-firstboot.service",
                    "-u", "systemd-sysusers.service", "-u", "vpsm-firstboot.service",
                    "-u", "ssh.service", "-u", "sshd.service"]))
        records = self.report.setdefault("diagnostics", [])
        for name, args in probes:
            if self.deadline is not None and time.monotonic() >= self.deadline:
                records.append({"name": "故障取证", "error": "已达到取证时限"})
                break
            try:
                result = self.incus(args, timeout=5)
                text = self.diagnostic_text(result.stdout)
                records.append({"name": name, "output": text})
                print(f"[诊断：{name}]\n{text}", flush=True)
            except Exception as exc:
                records.append({"name": name, "error": safe_error(exc)})

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
            rows = ["## 导出镜像验收", "", f"结果：**{self.STATUS_NAMES[self.report['status']]}**", "",
                    "| 验收项目 | 结果 |", "|---|---|"]
            rows += [f"| {self.stage_name(item['stage'])} | {self.STATUS_NAMES[item['status']]} |"
                     for item in self.report["steps"]]
            if "error" in self.report:
                error = self.report["error"]
                rows += ["", f"失败项目：{self.stage_name(error['stage'])}",
                         "", "```text", error['message'].replace("```", "｀｀｀"), "```"]
            if self.report.get("diagnostics"):
                rows += ["", "### 清理前故障取证"]
                for item in self.report["diagnostics"]:
                    rows += ["", item["name"], "```text",
                             item.get("output", item.get("error", "")).replace("```", "｀｀｀"), "```"]
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
                "image", "import", *map(str, paths), "--alias", self.alias], timeout=300, diagnose=True))
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
                self.deadline = time.monotonic() + 20
                if self.report["status"] != "success":
                    try:
                        self.collect_diagnostics()
                    except Exception:
                        print("[诊断] 取证未完成，继续清理验收资源", flush=True)
                self.deadline = time.monotonic() + 90
                if not self.cleanup():
                    self.report["status"] = "failed"
                if self.report_ready:
                    try:
                        self.write_report()
                    except Exception:
                        self.report["status"] = "failed"
                        print("[验收报告] 失败：无法写入验收报告或工作流摘要", flush=True)
            finally:
                signal.signal(signal.SIGTERM, previous)
                signal.signal(signal.SIGINT, old_int)
        return 0 if self.report["status"] == "success" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在原生运行器重新导入镜像并验收内网双栈及实例身份")
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
