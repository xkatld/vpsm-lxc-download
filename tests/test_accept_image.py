"""Acceptance tests use fake exports and mocked subprocesses only; never Incus."""
import base64
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build_pipeline import COMMON_PACKAGES, README_CONTENT, ImageSpec
from scripts.accept_image import (
    Acceptance, AcceptanceTerminated, CheckError, check_os_release, import_paths,
    instance_addresses, main, parse_os_release, public_hostkey_fingerprints, safe_error,
)


SPEC = ImageSpec("alpine", "3.24", "amd64", "default", "test")


def artifact(root, spec=SPEC, suffixes=(".tar.gz",)):
    root.mkdir(exist_ok=True)
    names = [spec.container_name() + suffix for suffix in suffixes]
    for name in names:
        (root / name).write_bytes(b"mock export, not a real image")
    row = {key: getattr(spec, key) for key in ("distro", "release", "architecture")}
    (root / "build-summary.json").write_text(json.dumps({
        "status": "success", "architecture": spec.architecture, "images": [row], "exported_files": names,
    }))
    digest = hashlib.sha256(b"mock export, not a real image").hexdigest()
    (root / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name in names))
    return names


def state(name, ipv4="10.0.0.2", ipv6="fd42::2"):
    return json.dumps([{"name": name, "status": "Running", "state": {"network": {
        "eth0": {"addresses": [{"family": "inet", "address": ipv4},
                                 {"family": "inet6", "address": ipv6}]},
    }}}])


def public_keys(seed=1):
    """Plausible SSH wire blobs; synthetic public data, not private key material."""
    def line(algorithm, *fields):
        values = [algorithm.encode(), *fields]
        blob = b"".join(len(value).to_bytes(4, "big") + value for value in values)
        return f"{algorithm} {base64.b64encode(blob).decode()} guest-comment\n"
    return (line("ssh-ed25519", bytes([seed]) * 32)
            + line("ssh-rsa", b"\x01\x00\x01", b"\x00\x80" + bytes([seed]) * 255)
            + line("ecdsa-sha2-nistp256", b"nistp256", b"\x04" + bytes([seed]) * 64))


class FakeRunner:
    def __init__(self, acceptance):
        self.acceptance = acceptance
        self.commands = []
        self.restarted = False
        self.failure = None
        self.readme = README_CONTENT + "\n"
        self.shadow = "root:!:20000:0:99999:7:::\n"
        self.package_status = "install ok installed"
        self.login = "root\n"
        self.machine_ids = {"cold": "1a" * 16 + "\n", "restart": "1a" * 16 + "\n", "clone": "2b" * 16 + "\n"}
        self.host_keys = {"cold": public_keys(), "restart": public_keys(), "clone": public_keys(2)}
        self.instances = set()

    def __call__(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if self.failure:
            self.failure(command, kwargs)
        output = ""
        if command[0] == "sshpass":
            output = self.login
        else:
            args = command[3:]
            if args[:1] == ["init"]:
                self.instances.add(args[2])
            elif args[:1] == ["restart"]:
                self.restarted = True
            elif args[:1] == ["list"]:
                if len(args) > 1 and args[1] == self.acceptance.instance:
                    index = 4 if args[1] == self.acceptance.clone_instance else (3 if self.restarted else 2)
                    output = state(self.acceptance.instance, f"10.0.0.{index}", f"fd42::{index}")
                else:
                    output = json.dumps([{"name": name} for name in sorted(self.instances)])
            elif args[:2] == ["image", "list"]:
                output = json.dumps([{"fingerprint": "a" * 64}])
            elif args[:2] == ["profile", "list"]:
                output = '[{"name":"default"}]'
            elif args[:1] == ["exec"]:
                guest = args[3:]
                phase = "clone" if args[1] == self.acceptance.clone_instance else ("restart" if self.restarted else "cold")
                if guest == ["cat", "/etc/machine-id"]:
                    output = self.machine_ids[phase]
                elif guest[:2] == ["sh", "-c"] and "ssh_host_ed25519_key.pub" in guest[2]:
                    output = self.host_keys[phase]
                elif guest == ["cat", "/etc/os-release"]:
                    output = 'ID=alpine\nVERSION_ID="3.24.1"\n'
                elif guest == ["cat", "/root/README.md"]:
                    output = self.readme
                elif guest == ["cat", "/etc/shadow"]:
                    output = self.shadow
                elif guest[:1] == ["dpkg-query"]:
                    output = self.package_status
        return subprocess.CompletedProcess(command, 0, output, "")


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dist = root / "dist"
        self.report_dir = root / "acceptance"
        artifact(self.dist)
        self.accept = Acceptance(SPEC, self.dist, self.report_dir)
        self.fake = FakeRunner(self.accept)
        self.console = io.StringIO()
        self.summary = root / "github-summary.md"
        for target, kwargs in (
            ("scripts.accept_image.subprocess.run", {"side_effect": self.fake}),
            ("scripts.accept_image.platform.machine", {"return_value": "x86_64"}),
            ("scripts.accept_image.shutil.which", {"return_value": "/mock/bin"}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.summary)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.enterContext(contextlib.redirect_stdout(self.console))

    def report(self):
        return json.loads((self.report_dir / "acceptance-report.json").read_text())

    def test_success_reimports_scoped_export_and_rechecks_fresh_addresses(self):
        original = {path.name: path.read_bytes() for path in self.dist.iterdir()}
        self.assertEqual(self.accept.run(), 0)
        report = self.report()
        self.assertEqual(report["status"], "success")
        self.assertTrue(all(step["status"] == "PASS" for step in report["steps"]))
        self.assertEqual(original, {path.name: path.read_bytes() for path in self.dist.iterdir()})
        self.assertIn("public IPv6", self.summary.read_text())
        commands = [cmd for cmd, _ in self.fake.commands]
        project = self.accept.project
        creation = next(cmd for cmd in commands if "create" in cmd)
        self.assertEqual(creation[:5], ["incus", "--project", "default", "project", "create"])
        for option in ("features.images=true", "features.profiles=true", "features.networks=false"):
            self.assertIn(option, creation)
        imports = [cmd for cmd in commands if cmd[3:5] == ["image", "import"]]
        self.assertEqual(imports, [["incus", "--project", project, "image", "import",
                                   str(self.dist / (SPEC.container_name() + ".tar.gz")), "--alias", self.accept.alias]])
        for cmd, kwargs in self.fake.commands:
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["check"])
            self.assertGreater(kwargs["timeout"], 0)
            self.assertNotIn(self.accept.password, " ".join(cmd))
            if cmd[0] == "incus" and cmd != creation:
                self.assertEqual(cmd[1:3], ["--project", project])
            if cmd[0] != "sshpass":
                self.assertNotIn("SSHPASS", kwargs["env"])
            self.assertNotIn("storage", cmd)
        self.assertIn(["incus", "--project", project, "profile", "device", "add", "default", "root",
                       "disk", "pool=default", "path=/"], commands)
        self.assertIn(["incus", "--project", project, "profile", "device", "add", "default", "eth0",
                       "nic", "network=incusbr0", "name=eth0"], commands)
        ssh = [cmd for cmd in commands if cmd[0] == "sshpass"]
        self.assertEqual([cmd[3] for cmd in ssh], ["-4", "-6", "-4", "-6", "-4", "-6"])
        self.assertEqual([cmd[-2:] for cmd in ssh], [["root@10.0.0.2", "whoami"], ["root@fd42::2", "whoami"],
                                                    ["root@10.0.0.3", "whoami"], ["root@fd42::3", "whoami"],
                                                    ["root@10.0.0.4", "whoami"], ["root@fd42::4", "whoami"]])
        shadow_index = next(i for i, cmd in enumerate(commands) if "/etc/shadow" in cmd)
        password_index = next(i for i, cmd in enumerate(commands) if "chpasswd" in cmd)
        self.assertLess(shadow_index, password_index)
        self.assertEqual(self.fake.commands[password_index][1]["input"], f"root:{self.accept.password}\n")
        syntax = [i for i, cmd in enumerate(commands) if "/usr/sbin/sshd" in cmd]
        logins = [i for i, cmd in enumerate(commands) if cmd[0] == "sshpass"]
        self.assertGreater(syntax[0], logins[1])
        self.assertGreater(syntax[1], logins[3])
        self.assertEqual(commands[-1], ["incus", "--project", project, "project", "delete", project])

    def test_clone_identity_uses_same_import_and_runtime_public_data_only(self):
        primary = self.accept.instance
        self.assertEqual(self.accept.run(), 0)
        commands = [cmd for cmd, _ in self.fake.commands]
        inits = [cmd for cmd in commands if cmd[3:4] == ["init"]]
        self.assertEqual(len(inits), 2)
        self.assertEqual([cmd[4] for cmd in inits], [self.accept.alias] * 2)
        self.assertEqual([cmd[5] for cmd in inits], [primary, self.accept.clone_instance])
        self.assertEqual(self.accept.instance, primary)
        self.assertFalse(any(cmd[3:4] == ["copy"] for cmd in commands))
        deleted = {cmd[4] for cmd in commands if cmd[3:4] == ["delete"]}
        self.assertEqual(deleted, {primary, self.accept.clone_instance})
        identities = self.report()["identities"]
        self.assertEqual(set(identities), {"cold", "restart", "clone"})
        self.assertEqual(identities["cold"], identities["restart"])
        self.assertNotEqual(identities["cold"]["machine_id_sha256"], identities["clone"]["machine_id_sha256"])
        for fingerprint in identities["cold"]["ssh_host_key_fingerprints"].values():
            self.assertNotIn(fingerprint, identities["clone"]["ssh_host_key_fingerprints"].values())
        for phase in identities:
            self.assertEqual(set(identities[phase]), {"machine_id_sha256", "ssh_host_key_fingerprints"})
        stages = {item["stage"]: item["status"] for item in self.report()["steps"]}
        self.assertEqual(stages["identity-stability"], "PASS")
        self.assertEqual(stages["identity-uniqueness"], "PASS")
        self.assertNotIn("Clone identity uniqueness is not covered", " ".join(self.report()["limitations"]))
        reads = [i for i, cmd in enumerate(commands) if cmd[-2:] == ["cat", "/etc/machine-id"]]
        ssh_indexes = [i for i, cmd in enumerate(commands) if cmd[0] == "sshpass"]
        for i, read in enumerate(reads):
            self.assertGreater(read, ssh_indexes[2 * i + 1])
        clone_commands = [cmd for cmd in commands if cmd[3:5] == ["exec", self.accept.clone_instance]]
        shadow = next(i for i, cmd in enumerate(clone_commands) if "/etc/shadow" in cmd)
        backups = next(i for i, cmd in enumerate(clone_commands) if "/etc/shadow-" in cmd[-1])
        password = next(i for i, cmd in enumerate(clone_commands) if "chpasswd" in cmd)
        self.assertLess(shadow, password)
        self.assertLess(backups, password)
        inputs = [kwargs["input"] for cmd, kwargs in self.fake.commands if "chpasswd" in cmd]
        self.assertEqual(len(set(inputs)), 2)
        output = json.dumps(self.report()) + self.console.getvalue() + self.summary.read_text()
        for value in [*self.fake.machine_ids.values(), *self.accept.secrets]:
            if value:
                self.assertNotIn(value.strip(), output)
        for keys in self.fake.host_keys.values():
            for line in keys.splitlines():
                self.assertNotIn(line.split()[1], output)
        for cmd in clone_commands:
            if "ssh_host_" in cmd[-1]:
                self.assertNotIn("ssh-keygen", cmd[-1])
                self.assertIn("cat", cmd[-1])
                self.assertEqual(cmd[-1].count("_key.pub"), 3)

    def test_duplicate_machine_id_and_each_public_key_fail_uniqueness(self):
        for duplicate in ("machine-id", "ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256"):
            with self.subTest(duplicate=duplicate):
                self.accept = Acceptance(SPEC, self.dist, self.report_dir)
                self.fake = FakeRunner(self.accept)
                if duplicate == "machine-id":
                    self.fake.machine_ids["clone"] = self.fake.machine_ids["cold"]
                else:
                    original = {line.split()[0]: line for line in self.fake.host_keys["cold"].splitlines()}
                    self.fake.host_keys["clone"] = "\n".join(
                        original[duplicate] if line.split()[0] == duplicate else line
                        for line in self.fake.host_keys["clone"].splitlines())
                with patch("scripts.accept_image.subprocess.run", side_effect=self.fake):
                    self.assertEqual(self.accept.run(), 1)
                self.assertEqual(self.report()["error"]["stage"], "identity-uniqueness")
                self.assertFalse(self.accept.project_created)
                self.assertNotEqual(self.accept.instance, self.accept.clone_instance)
                deleted = {cmd[4] for cmd, _ in self.fake.commands if cmd[3:4] == ["delete"]}
                self.assertEqual(deleted, {self.accept.instance, self.accept.clone_instance})

    def test_restart_machine_id_and_each_host_key_must_stay_stable(self):
        for changed in ("machine-id", "ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "missing-key"):
            with self.subTest(changed=changed):
                self.accept = Acceptance(SPEC, self.dist, self.report_dir)
                self.fake = FakeRunner(self.accept)
                if changed == "machine-id":
                    self.fake.machine_ids["restart"] = self.fake.machine_ids["clone"]
                else:
                    replacements = {line.split()[0]: line for line in public_keys(3).splitlines()}
                    self.fake.host_keys["restart"] = "\n".join(
                        replacements[changed] if line.split()[0] == changed else line
                        for line in self.fake.host_keys["cold"].splitlines())
                    if changed == "missing-key":
                        self.fake.host_keys["restart"] = self.fake.host_keys["restart"].splitlines()[0]
                with patch("scripts.accept_image.subprocess.run", side_effect=self.fake):
                    self.assertEqual(self.accept.run(), 1)
                self.assertEqual(self.report()["error"]["stage"], "identity-stability")
                self.assertFalse(self.accept.project_created)
                self.assertFalse(any(cmd[3:6] == ["init", self.accept.alias, self.accept.clone_instance]
                                     for cmd, _ in self.fake.commands))

    def test_identity_samples_reject_malformed_machine_id_and_missing_keys(self):
        for value in ("", "uninitialized\n", "0" * 32, "A" * 32, "a" * 31, "a" * 33,
                      " " + "a" * 32, "a" * 32 + "\n\n", "private-marker"):
            with self.subTest(machine_id=value):
                self.fake.machine_ids["cold"] = value
                with self.assertRaises(CheckError):
                    self.accept.sample_identity("cold")
                self.assertEqual(self.accept.report["identities"], {})
        self.fake.machine_ids["cold"] = "1a" * 16 + "\n"
        self.fake.host_keys["cold"] = "\n"
        with self.assertRaisesRegex(CheckError, "no SSH host public keys"):
            self.accept.sample_identity("cold")

    def test_clone_without_keys_or_same_key_set_cannot_pass(self):
        for keys, stage in (("", "clone-identity"), (public_keys(2).splitlines()[0], "identity-uniqueness")):
            with self.subTest(stage=stage):
                self.accept = Acceptance(SPEC, self.dist, self.report_dir)
                self.fake = FakeRunner(self.accept)
                self.fake.host_keys["clone"] = keys
                with patch("scripts.accept_image.subprocess.run", side_effect=self.fake):
                    self.assertEqual(self.accept.run(), 1)
                self.assertEqual(self.report()["error"]["stage"], stage)
                self.assertFalse(self.accept.project_created)

    def test_failed_clone_restores_primary_and_cleans_both_instances(self):
        primary, password = self.accept.instance, self.accept.password
        def fail(cmd, kwargs):
            if cmd[3:5] == ["start", self.accept.clone_instance]:
                raise subprocess.TimeoutExpired(cmd, 1, output="private-clone-marker")
        self.fake.failure = fail
        self.assertEqual(self.accept.run(), 1)
        self.assertEqual(self.report()["error"]["stage"], "clone-start")
        self.assertEqual((self.accept.instance, self.accept.password), (primary, password))
        deleted = {cmd[4] for cmd, _ in self.fake.commands if cmd[3:4] == ["delete"]}
        self.assertEqual(deleted, {primary, self.accept.clone_instance})
        self.assertNotIn("private-clone-marker", json.dumps(self.report()) + self.console.getvalue())

    def test_invalid_artifact_fails_before_any_incus_mutation(self):
        (self.dist / (SPEC.container_name() + ".tar.gz")).write_bytes(b"corrupt")
        self.assertEqual(self.accept.run(), 1)
        self.assertEqual(self.fake.commands, [])
        self.assertEqual(self.report()["error"]["stage"], "verify-export")

    def test_architecture_mismatch_is_reported(self):
        with patch("scripts.accept_image.platform.machine", return_value="aarch64"):
            self.assertEqual(self.accept.run(), 1)
        self.assertEqual(self.report()["error"]["stage"], "preflight")
        self.assertEqual(self.fake.commands, [])

    def test_unsupported_release_is_reported_without_echoing_input(self):
        self.accept.spec = ImageSpec("alpine", "secret\n::error::", "amd64", "default", "test")
        self.assertEqual(self.accept.run(), 1)
        self.assertNotIn("secret", (self.report_dir / "acceptance-report.json").read_text())
        self.assertEqual(self.fake.commands, [])

    def test_report_directory_cannot_contaminate_dist(self):
        self.accept.report_dir = self.dist / "acceptance"
        self.assertEqual(self.accept.run(), 1)
        self.assertFalse(self.accept.report_dir.exists())
        self.assertEqual(self.fake.commands, [])

    def test_failed_project_creation_never_deletes_unowned_resources(self):
        def fail(cmd, kwargs):
            raise subprocess.CalledProcessError(1, cmd)
        self.fake.failure = fail
        self.assertEqual(self.accept.run(), 1)
        self.assertEqual(len(self.fake.commands), 1)
        self.assertFalse(self.accept.project_created)
        self.assertEqual(self.report()["error"]["stage"], "create-project")

    def test_partial_import_or_init_failure_is_cleaned_with_project_scope(self):
        for stage, prefix in (("import-image", ["image", "import"]), ("create-instance", ["init"])):
            with self.subTest(stage=stage):
                self.accept = Acceptance(SPEC, self.dist, self.report_dir)
                self.fake.acceptance = self.accept
                self.fake.commands.clear()
                def fail(cmd, kwargs):
                    if cmd[3:3 + len(prefix)] == prefix:
                        raise subprocess.TimeoutExpired(cmd, 1)
                self.fake.failure = fail
                self.assertEqual(self.accept.run(), 1)
                commands = [cmd for cmd, _ in self.fake.commands]
                self.assertTrue(any(cmd[3:5] == ["image", "delete"] for cmd in commands))
                self.assertFalse(any(cmd[3:6] == ["profile", "delete", "default"] for cmd in commands))
                self.assertEqual(self.report()["error"]["stage"], stage)
                self.assertFalse(self.accept.project_created)

    def test_package_database_checks_all_nine_packages_and_status(self):
        for distro, program in (("alpine", "apk"), ("debian", "dpkg-query"),
                                ("ubuntu", "dpkg-query"), ("centos", "rpm"), ("almalinux", "rpm")):
            with self.subTest(distro=distro):
                self.accept.spec = ImageSpec(distro, "unused", "amd64", "default", "test")
                self.fake.commands.clear()
                self.accept.check_packages()
                commands = [cmd for cmd, _ in self.fake.commands]
                self.assertEqual(len(commands), 9)
                self.assertEqual([cmd[-1] for cmd in commands], COMMON_PACKAGES.split())
                self.assertTrue(all(cmd[6] == program for cmd in commands))
        self.accept.spec = ImageSpec("debian", "bookworm", "amd64", "default", "test")
        for status in ("deinstall ok config-files", "install ok unpacked", "install reinstreq installed", ""):
            self.fake.package_status = status
            with self.assertRaises(CheckError):
                self.accept.check_packages()

    def test_missing_package_aborts_and_cleans(self):
        def fail(cmd, kwargs):
            if "apk" in cmd and cmd[-1] == "nano":
                raise subprocess.CalledProcessError(1, cmd, stderr="untrusted")
        self.fake.failure = fail
        self.assertEqual(self.accept.run(), 1)
        self.assertEqual(self.report()["error"]["stage"], "packages")
        self.assertFalse(self.accept.project_created)

    def test_exact_readme_and_exact_root_lock(self):
        self.accept.check_readme()
        self.accept.check_root_lock()
        for text in (README_CONTENT, README_CONTENT + "\nextra", "wrong\n"):
            self.fake.readme = text
            with self.assertRaises(CheckError):
                self.accept.check_readme()
        for marker in ("", "!!", "!$6$secret", "*", "$6$secret"):
            self.fake.shadow = f"root:{marker}:20000:0:99999:7:::\n"
            with self.assertRaises(CheckError):
                self.accept.check_root_lock()
        for text in ("", "root:!\n", "root:!:1:0:1:1:::\nroot:!:1:0:1:1:::\n"):
            self.fake.shadow = text
            with self.assertRaises(CheckError):
                self.accept.check_root_lock()

    def test_unlocked_root_fails_before_password_mutation(self):
        self.fake.shadow = "root:!$6$secret:20000:0:99999:7:::\n"
        self.assertEqual(self.accept.run(), 1)
        self.assertEqual(self.report()["error"]["stage"], "root-lock")
        self.assertFalse(any("chpasswd" in cmd for cmd, _ in self.fake.commands))
        self.assertNotIn("$6$secret", self.console.getvalue())

    def test_credential_residue_gate_is_readonly_and_precedes_test_password(self):
        self.assertEqual(self.accept.run(), 0)
        commands = [cmd for cmd, _ in self.fake.commands]
        check_index = next(i for i, cmd in enumerate(commands) if cmd[6:8] == ["sh", "-c"])
        script = commands[check_index][-1]
        for path in ("/etc/shadow-", "/etc/shadow~", "/etc/shadow.bak", "/etc/gshadow-",
                     "/etc/gshadow~", "/etc/gshadow.bak", "/var/backups/shadow*", "/var/backups/gshadow*",
                     "/etc/passwd-", "/etc/passwd~", "/etc/passwd.bak", "/etc/group-", "/etc/group~", "/etc/group.bak",
                     "/var/backups/passwd*", "/var/backups/group*",
                     "/root/.ssh", "/root/.bash_history", "/root/.ash_history", "/root/.zsh_history"):
            self.assertIn(path, script)
        self.assertIn('[ -e "$path" ] || [ -L "$path" ]', script)
        for forbidden in ("rm ", "cat ", "echo ", "printf ", "/tmp", "chpasswd"):
            self.assertNotIn(forbidden, script)
        self.assertLess(check_index, next(i for i, cmd in enumerate(commands) if "chpasswd" in cmd))
        self.assertIn("not raw archives", " ".join(self.report()["limitations"]))

    def test_credential_residue_failure_blocks_password_and_cleans_without_leaking(self):
        def fail(cmd, kwargs):
            if cmd[6:8] == ["sh", "-c"] and "/etc/gshadow-" in cmd[-1]:
                raise subprocess.CalledProcessError(1, cmd, output="private residue", stderr="private residue")
        self.fake.failure = fail
        self.assertEqual(self.accept.run(), 1)
        report = self.report()
        self.assertEqual(report["error"]["stage"], "credential-backups")
        self.assertFalse(any("chpasswd" in cmd for cmd, _ in self.fake.commands))
        self.assertFalse(self.accept.project_created)
        self.assertNotIn("private residue", self.console.getvalue())
        self.assertNotIn("private residue", json.dumps(report))
        self.assertNotIn("private residue", self.summary.read_text())

    def test_ssh_mismatch_and_nonroot_output_fail_without_family_fallback(self):
        with self.assertRaises(CheckError):
            self.accept.ssh(6, "10.0.0.2")
        self.fake.login = "not-root\n"
        with patch("scripts.accept_image.time.monotonic", side_effect=[0, 0, 121, 121]):
            with self.assertRaises(CheckError):
                self.accept.ssh(6, "fd42::2")
        ssh = [cmd for cmd, _ in self.fake.commands if cmd[0] == "sshpass"]
        self.assertEqual(len(ssh), 1)
        self.assertIn("-6", ssh[0])
        self.assertNotIn("-4", ssh[0])

    def test_runtime_failure_and_term_produce_private_stage_reports_and_cleanup(self):
        secret = "private-credential-marker"
        for error in (subprocess.CalledProcessError(1, [secret], output=secret, stderr=secret),
                      subprocess.TimeoutExpired([secret], 1, output=secret, stderr=secret),
                      RuntimeError(secret), AcceptanceTerminated()):
            with self.subTest(error=type(error).__name__):
                self.accept = Acceptance(SPEC, self.dist, self.report_dir)
                self.accept.password = secret
                self.accept.secrets.append(secret)
                self.fake.acceptance = self.accept
                def fail(cmd, kwargs):
                    if "chpasswd" in cmd:
                        raise error
                self.fake.failure = fail
                self.assertEqual(self.accept.run(), 1)
                self.assertEqual(self.report()["error"]["stage"], "test-password")
                self.assertFalse(self.accept.project_created)
                self.assertNotIn(secret, (self.report_dir / "acceptance-report.json").read_text())
                self.assertNotIn(secret, self.console.getvalue())
                self.assertNotIn(secret, self.summary.read_text())

    def test_cleanup_failure_fails_gate_but_continues_remaining_cleanup(self):
        def fail(cmd, kwargs):
            if cmd[3:5] == ["image", "delete"]:
                raise RuntimeError("do not log this")
        self.fake.failure = fail
        self.assertEqual(self.accept.run(), 1)
        self.assertEqual(self.report()["status"], "failed")
        self.assertEqual(self.report()["error"]["stage"], "cleanup-images-0")
        self.assertEqual(self.fake.commands[-1][0][3:5], ["project", "delete"])
        self.assertNotIn("do not log this", self.console.getvalue())

    def test_inherited_credentials_removed_from_subprocess_environment(self):
        with patch.dict(os.environ, {"IMAGE_ROOT_PASSWORD": "old-secret", "SSHPASS": "old-ssh"}):
            self.accept.guest(["true"])
            self.accept.ssh(4, "10.0.0.2")
        for cmd, kwargs in self.fake.commands:
            self.assertNotIn("IMAGE_ROOT_PASSWORD", kwargs["env"])
            if cmd[0] == "sshpass":
                self.assertEqual(kwargs["env"]["SSHPASS"], self.accept.password)
                self.assertEqual(cmd[:3], ["sshpass", "-e", "ssh"])
            else:
                self.assertNotIn("SSHPASS", kwargs["env"])

    def test_required_network_ssh_ca_and_sshd_failures_fail_the_gate(self):
        cases = ("cold-network", "cold-ssh-ipv6", "restart-ssh-ipv4", "cold-sshd-config", "ca-bundle")
        for stage in cases:
            with self.subTest(stage=stage):
                self.accept = Acceptance(SPEC, self.dist, self.report_dir)
                self.accept.READY_TIMEOUT = 0.005
                self.fake.acceptance = self.accept
                self.fake.restarted = False
                self.fake.commands.clear()
                def fail(cmd, kwargs):
                    matched = (
                        (stage == "cold-network" and cmd[3:5] == ["list", self.accept.instance])
                        or (stage == "cold-ssh-ipv6" and cmd[0] == "sshpass" and "-6" in cmd)
                        or (stage == "restart-ssh-ipv4" and self.fake.restarted and cmd[0] == "sshpass" and "-4" in cmd)
                        or (stage == "cold-sshd-config" and "/usr/sbin/sshd" in cmd)
                        or (stage == "ca-bundle" and cmd[-2:] == ["-s", "/etc/ssl/certs/ca-certificates.crt"])
                    )
                    if matched:
                        raise subprocess.CalledProcessError(1, cmd, stderr="untrusted guest output")
                self.fake.failure = fail
                with patch("scripts.accept_image.time.sleep"):
                    self.assertEqual(self.accept.run(), 1)
                self.assertEqual(self.report()["error"]["stage"], stage)
                self.assertFalse(self.accept.project_created)
                self.assertNotIn("untrusted guest output", self.console.getvalue())
                if stage == "cold-ssh-ipv6":
                    ssh = [cmd for cmd, _ in self.fake.commands if cmd[0] == "sshpass"]
                    self.assertTrue(all("-6" in cmd for cmd in ssh[1:]))

    def test_overall_deadline_bounds_commands_without_invoking_them(self):
        self.accept.deadline = 10
        with patch("scripts.accept_image.time.monotonic", return_value=11):
            with self.assertRaisesRegex(CheckError, "overall acceptance deadline"):
                self.accept.guest(["true"])
        self.assertEqual(self.fake.commands, [])

    def test_main_supports_parent_cli(self):
        with patch.object(Acceptance, "run", return_value=0) as run:
            self.assertEqual(main(["--output-dir", str(self.dist), "--report-dir", str(self.report_dir),
                                   "--distro", "alpine", "--release", "3.24", "--architecture", "amd64"]), 0)
            run.assert_called_once()


class ParsingTests(unittest.TestCase):
    def test_public_fingerprints_hash_actual_blob_and_ignore_comments(self):
        keys = public_keys()
        parsed = public_hostkey_fingerprints(keys)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed, public_hostkey_fingerprints(keys.replace("guest-comment", "other comment")))
        for line in keys.splitlines():
            algorithm, encoded, _ = line.split()
            digest = base64.b64encode(hashlib.sha256(base64.b64decode(encoded)).digest()).decode().rstrip("=")
            self.assertEqual(parsed[algorithm], "SHA256:" + digest)
        for text in ("", "malformed", "ssh-ed25519 !!!", "ssh-ed25519 c2VjcmV0",
                     keys + keys, keys.replace("ssh-ed25519 ", "ssh-dss ", 1),
                     keys.replace("ssh-ed25519 ", "ssh-rsa ", 1)):
            with self.subTest(text=text), self.assertRaises(CheckError):
                public_hostkey_fingerprints(text)

    def test_supported_split_and_unified_imports_metadata_first(self):
        suffixes = (".tar", ".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst")
        cases = [(s,) for s in suffixes] + [(".root", "")]
        cases += [(".rootfs" + root, meta) for root in suffixes for meta in suffixes]
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                artifact(root, suffixes=case)
                paths = import_paths(root, SPEC)
                self.assertNotIn(".root", paths[0].name)
                self.assertEqual(set(paths), {root / (SPEC.container_name() + s) for s in case})

    def test_importer_rejects_inexact_names_and_incomplete_sets(self):
        for suffixes in ((".root",), (".tar.gz", ".root"), ("-lite.tar.gz",), ("2.tar.gz",)):
            with self.subTest(suffixes=suffixes), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                artifact(root, suffixes=suffixes)
                with self.assertRaises(ValueError):
                    import_paths(root, SPEC)

    def test_addresses_ignore_loopback_linklocal_multicast_and_other_instances(self):
        self.assertEqual(instance_addresses(state("mine"), "other"), {})
        for v4, v6 in (("127.0.0.1", "fe80::1"), ("0.0.0.0", "::"),
                       ("169.254.1.2", "::1"), ("224.0.0.1", "ff02::1"),
                       ("garbage", "::ffff:10.0.0.2")):
            self.assertEqual(instance_addresses(state("mine", v4, v6), "mine"), {})
        self.assertEqual(instance_addresses(state("mine"), "mine"), {"ipv4": "10.0.0.2", "ipv6": "fd42::2"})
        payload = json.loads(state("mine"))
        payload[0]["state"]["network"]["eth1"] = payload[0]["state"]["network"].pop("eth0")
        self.assertEqual(instance_addresses(json.dumps(payload), "mine"), {})

    def test_os_release_all_families_and_debian_development(self):
        cases = [
            ("alpine", "3.24", "ID=alpine\nVERSION_ID=3.24.1"),
            ("almalinux", "9", "ID=almalinux\nVERSION_ID=9.6"),
            ("centos", "9-Stream", 'ID=centos\nVERSION_ID=9\nNAME="CentOS Stream"'),
            ("debian", "bookworm", "ID=debian\nVERSION_ID=12\nVERSION_CODENAME=bookworm"),
            ("debian", "forky", "ID=debian\nVERSION_CODENAME=forky"),
            ("ubuntu", "noble", "ID=ubuntu\nVERSION_ID=24.04\nVERSION_CODENAME=noble"),
        ]
        for distro, release, text in cases:
            spec = ImageSpec(distro, release, "amd64", "default", "test")
            check_os_release(text, spec)
            with self.assertRaises(CheckError):
                check_os_release(text.replace("ID=" + distro, "ID=wrong"), spec)
        for text in ("ID=alpine\nVERSION_ID=3.240", "ID=alpine\nVERSION_ID=3.23", "ID=alpine"):
            with self.assertRaises(CheckError):
                check_os_release(text, SPEC)
        with self.assertRaises(CheckError):
            parse_os_release("ID=alpine\nID=ubuntu")
        self.assertEqual(parse_os_release('NAME="$(touch /never-run)"')["NAME"], "$(touch /never-run)")

    def test_errors_never_stringify_untrusted_exception_data(self):
        for exc in (RuntimeError("secret"), ValueError("secret"), OSError("secret"),
                    subprocess.CalledProcessError(1, ["secret"], "secret", "secret"),
                    subprocess.TimeoutExpired(["secret"], 1, "secret", "secret")):
            self.assertNotIn("secret", safe_error(exc))


if __name__ == "__main__":
    unittest.main()
