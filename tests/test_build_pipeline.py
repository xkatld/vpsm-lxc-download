#!/usr/bin/env python3
"""Hermetic regression tests: all Incus and SSH subprocesses are mocked."""
import contextlib
import hashlib
import io
import json
import os
import signal
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import build_pipeline


# Deliberately independent of the checked-in manifest. This is the supported
# release contract, not a count inferred from whatever the manifest contains.
RELEASES = (
    ("almalinux", "8"), ("almalinux", "9"), ("almalinux", "10"),
    ("alpine", "3.21"), ("alpine", "3.22"), ("alpine", "3.23"), ("alpine", "3.24"),
    ("centos", "9-Stream"), ("centos", "10-Stream"),
    ("debian", "bookworm"), ("debian", "trixie"), ("debian", "forky"),
    ("ubuntu", "jammy"), ("ubuntu", "noble"), ("ubuntu", "resolute"),
)
RUNNERS = {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
HEADER = (
    "Distribution: 发行版\tRelease: 版本\tArchitecture: 架构\tVariant: 变体\t"
    "Build date: 构建日期\tLXC (privileged): LXC (特权容器)\t"
    "LXC (unprivileged): LXC (非特权容器)\tIncus (container): Incus (容器)\tIncus (VM): Incus (虚拟机)\n"
)


def manifest_row(distro, release, architecture="arm64", variant="default", container="YES", date="20261001_01:23"):
    return f"{distro}\t{release}\t{architecture}\t{variant}\t{date}\tYES\tYES\t{container}\tYES\n"


MANIFEST = HEADER + "".join(
    manifest_row(distro, release, architecture)
    for architecture in RUNNERS
    for distro, release in RELEASES
)


def spec_for(distro="debian", release="bookworm", architecture="arm64"):
    return build_pipeline.ImageSpec(distro, release, architecture, "default", "20261001_01:23")


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(["mock-command"], returncode, stdout, stderr)


def make_archive(path, members):
    """Create tiny, real archives without executing any external commands."""
    with tarfile.open(path, "w:xz") as archive:
        for name in members:
            data = b"architecture: aarch64\n" if name == "metadata.yaml" else b"test fixture\n"
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))


class HermeticTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / "dist"
        self.manifest = self.root / "manifest.tsv"
        self.manifest.write_text(MANIFEST, encoding="utf-8")
        self.spec = spec_for()
        self.password = "test-'$(never-run); & secret"
        self.subprocess = self.enterContext(patch("build_pipeline.subprocess.run", return_value=completed()))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def pipeline(self, **kwargs):
        specs = kwargs.pop("specs", [self.spec])
        output = kwargs.pop("output_dir", self.output)
        password = kwargs.pop("root_password", self.password)
        return build_pipeline.Pipeline(specs, output, password, **kwargs)

    def native_tools(self, machine="aarch64"):
        self.enterContext(patch("build_pipeline.platform.machine", return_value=machine))
        self.enterContext(patch("build_pipeline.shutil.which", return_value="/mock/tool"))

    def fake_clock(self):
        now = [0.0]
        self.enterContext(patch("build_pipeline.time.monotonic", side_effect=lambda: now[0]))

        def advance(seconds):
            self.assertGreater(seconds, 0)
            now[0] += seconds
            self.assertLess(now[0], 1000, "retry loop did not honor its deadline")

        sleep = self.enterContext(patch("build_pipeline.time.sleep", side_effect=advance))
        return now, sleep


class ManifestTests(HermeticTestCase):
    def test_all_fifteen_releases_for_each_architecture(self):
        for architecture in RUNNERS:
            with self.subTest(architecture=architecture):
                specs = build_pipeline.load_manifest(self.manifest, architecture)
                self.assertEqual([(spec.distro, spec.release) for spec in specs], list(RELEASES))
                self.assertEqual({spec.architecture for spec in specs}, {architecture})
                self.assertEqual({spec.variant for spec in specs}, {"default"})

    def test_default_architecture_remains_arm64(self):
        self.assertEqual({spec.architecture for spec in build_pipeline.load_manifest(self.manifest)}, {"arm64"})

    def test_filters_variant_and_container_support(self):
        self.manifest.write_text(
            MANIFEST
            + manifest_row("ubuntu", "ignored-cloud", variant="cloud")
            + manifest_row("ubuntu", "ignored-vm-only", container="NO")
            + manifest_row("ubuntu", "ignored-architecture", architecture="riscv64"),
            encoding="utf-8",
        )
        self.assertEqual(len(build_pipeline.load_manifest(self.manifest)), 15)

    def test_duplicate_selected_row_rejected(self):
        self.manifest.write_text(MANIFEST + manifest_row("debian", "bookworm"), encoding="utf-8")
        with self.assertRaises(ValueError):
            build_pipeline.load_manifest(self.manifest)

    def test_empty_or_missing_columns_rejected(self):
        for text in ("", "Distribution\tRelease\n", HEADER):
            with self.subTest(text=text):
                self.manifest.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    build_pipeline.load_manifest(self.manifest)

    def test_missing_required_selected_value_rejected(self):
        self.manifest.write_text(HEADER + manifest_row("debian", "bookworm", date=""), encoding="utf-8")
        with self.assertRaises(ValueError):
            build_pipeline.load_manifest(self.manifest)

    def test_absent_architecture_rejected(self):
        self.manifest.write_text(HEADER + manifest_row("debian", "bookworm"), encoding="utf-8")
        with self.assertRaises(ValueError):
            build_pipeline.load_manifest(self.manifest, "amd64")

    def test_display_names_aliases_and_remote_paths(self):
        display = {
            ("debian", "bookworm"): "12", ("debian", "trixie"): "13", ("debian", "forky"): "14",
            ("ubuntu", "jammy"): "2204", ("ubuntu", "noble"): "2404", ("ubuntu", "resolute"): "2604",
        }
        for architecture in RUNNERS:
            for distro, release in RELEASES:
                spec = spec_for(distro, release, architecture)
                with self.subTest(distro=distro, release=release, architecture=architecture):
                    self.assertEqual(spec.image_alias, f"{distro}-{release}".lower())
                    self.assertEqual(spec.remote_path(), f"images:{distro}/{release}/{architecture}")
                    version = display.get((distro, release), release).replace(".", "").lower()
                    self.assertEqual(spec.container_name(), f"{distro}{version}-{architecture}-lxc")


class CliTests(HermeticTestCase):
    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                result = build_pipeline.main(["--manifest", str(self.manifest), *args])
            except SystemExit as exc:
                result = exc.code
        return result, stdout.getvalue(), stderr.getvalue()

    def test_matrix_is_exact_thirty_jobs_and_requires_no_password_or_tools(self):
        with patch.object(build_pipeline.Pipeline, "run") as run, patch("build_pipeline.shutil.which") as which:
            status, stdout, _ = self.invoke(["--matrix"])
        self.assertEqual(status, 0)
        payload = json.loads(stdout)
        self.assertEqual(set(payload), {"include"})
        expected = {
            (distro, release, architecture, runner)
            for architecture, runner in RUNNERS.items()
            for distro, release in RELEASES
        }
        self.assertEqual(len(payload["include"]), 30)
        self.assertEqual(
            {(item["distro"], item["release"], item["architecture"], item["runner"]) for item in payload["include"]},
            expected,
        )
        for item in payload["include"]:
            self.assertEqual(set(item), {"distro", "release", "architecture", "runner"})
        run.assert_not_called()
        which.assert_not_called()
        self.subprocess.assert_not_called()

    def test_build_requires_architecture_distro_and_release(self):
        args = {"--architecture": "arm64", "--distro": "debian", "--release": "bookworm"}
        for missing in args:
            with self.subTest(missing=missing), patch.object(build_pipeline.Pipeline, "run") as run:
                status, _, _ = self.invoke([value for key, value in args.items() if key != missing for value in (key, value)])
                self.assertNotEqual(status, 0)
                run.assert_not_called()
        self.subprocess.assert_not_called()

    def test_invalid_architecture_is_rejected_before_build(self):
        with patch.object(build_pipeline.Pipeline, "run") as run:
            status, _, _ = self.invoke(["--architecture", "riscv64", "--distro", "debian", "--release", "bookworm"])
        self.assertNotEqual(status, 0)
        run.assert_not_called()

    def test_build_selects_exactly_one_spec_without_password_environment(self):
        for architecture in RUNNERS:
            with self.subTest(architecture=architecture), patch.object(build_pipeline.Pipeline, "run", autospec=True) as run:
                status, _, _ = self.invoke([
                    "--architecture", architecture, "--distro", "centos", "--release", "10-Stream",
                    "--output-dir", str(self.output),
                ])
                self.assertEqual(status, 0)
                pipeline = run.call_args.args[0]
                self.assertEqual([(s.distro, s.release, s.architecture) for s in pipeline.specs], [("centos", "10-Stream", architecture)])
                self.assertTrue(pipeline.root_password)
                self.assertEqual(pipeline.output_dir, self.output)
        self.subprocess.assert_not_called()

    def test_unknown_distro_release_selection_fails_without_commands(self):
        for distro, release in (("ubuntu", "missing"), ("missing", "noble")):
            with self.subTest(distro=distro), patch.object(build_pipeline.Pipeline, "run") as run:
                status, _, _ = self.invoke(["--architecture", "amd64", "--distro", distro, "--release", release])
                self.assertNotEqual(status, 0)
                run.assert_not_called()
        self.subprocess.assert_not_called()


class PreflightAndIdentityTests(HermeticTestCase):
    def test_native_architecture_aliases_supported(self):
        for machine, architecture in (("aarch64", "arm64"), ("arm64", "arm64"), ("x86_64", "amd64"), ("amd64", "amd64")):
            with self.subTest(machine=machine), patch("build_pipeline.platform.machine", return_value=machine), patch("build_pipeline.shutil.which", return_value="/mock/tool"):
                pipeline = self.pipeline(specs=[spec_for(architecture=architecture)])
                with patch.object(pipeline, "incus", return_value=completed()) as incus:
                    pipeline.preflight()
                self.assertTrue(any(args.args[0] == ["version"] for args in incus.call_args_list))
                self.assertFalse(pipeline.output_ready)
                self.assertFalse(self.output.exists())

    def test_foreign_or_unknown_architecture_rejected(self):
        for machine in ("x86_64", "riscv64"):
            with self.subTest(machine=machine), patch("build_pipeline.platform.machine", return_value=machine), patch("build_pipeline.shutil.which", return_value="/mock/tool"):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.pipeline().preflight()

    def test_preflight_requires_exactly_one_spec(self):
        self.native_tools()
        for specs in ([], [self.spec, spec_for("ubuntu", "noble")]):
            with self.subTest(count=len(specs)):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.pipeline(specs=specs).preflight()
        self.subprocess.assert_not_called()

    def test_missing_incus_or_sshpass_is_rejected(self):
        self.native_tools()
        for missing in ("incus", "sshpass"):
            with self.subTest(missing=missing), patch("build_pipeline.shutil.which", side_effect=lambda name: None if name == missing else "/mock/tool"):
                with self.assertRaises(RuntimeError):
                    self.pipeline().preflight()

    def test_skip_ssh_does_not_require_sshpass(self):
        self.native_tools()
        with patch("build_pipeline.shutil.which", side_effect=lambda name: None if name == "sshpass" else "/mock/tool"):
            self.pipeline(skip_ssh_test=True).preflight()

    def test_nonempty_output_is_rejected_without_deleting_contents(self):
        self.native_tools()
        self.output.mkdir()
        sentinel = self.output / "previous-build.tar.xz"
        sentinel.write_bytes(b"must remain")
        pipeline = self.pipeline()
        with self.assertRaises((ValueError, RuntimeError)):
            pipeline.preflight()
        self.assertEqual(sentinel.read_bytes(), b"must remain")
        self.assertFalse(pipeline.output_ready)

    def test_file_output_path_is_rejected(self):
        self.native_tools()
        self.output.write_bytes(b"existing-file")
        with self.assertRaises((OSError, RuntimeError, ValueError)):
            self.pipeline().preflight()
        self.assertEqual(self.output.read_bytes(), b"existing-file")

    def test_run_ids_are_unique_even_for_same_github_run(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "1"}):
            pipelines = [self.pipeline() for _ in range(3)]
        self.assertEqual(len({pipeline.run_id for pipeline in pipelines}), 3)
        for pipeline in pipelines:
            self.assertRegex(pipeline.run_id, r"^[a-z0-9-]+$")
            self.assertTrue(pipeline.container_name(self.spec).startswith(f"vpsm-{pipeline.run_id}-"))
            self.assertEqual(pipeline.base_alias(self.spec), pipeline.resource_name(f"{self.spec.image_alias}-{self.spec.architecture}"))
            self.assertTrue(all(len(name) <= 63 for name in pipeline.containers))
            self.assertEqual(pipeline.created_containers, [])
            self.assertEqual(pipeline.created_base_aliases, [])
            self.assertEqual(pipeline.created_published_aliases, [])
            self.assertEqual(pipeline.sanitized_containers, set())

    def test_automatic_password_is_nonempty_and_unique(self):
        first = build_pipeline.Pipeline([self.spec], self.output)
        second = build_pipeline.Pipeline([self.spec], self.output)
        self.assertTrue(first.root_password)
        self.assertNotEqual(first.root_password, second.root_password)


class SafeCommandTests(HermeticTestCase):
    @patch.dict("os.environ", {"IMAGE_ROOT_PASSWORD": "inherited-secret", "SSHPASS": "old-secret", "PATH": "/bin"})
    def test_incus_environment_excludes_inherited_test_credentials(self):
        self.pipeline().incus(["version"])
        environment = self.subprocess.call_args.kwargs["env"]
        self.assertNotIn("IMAGE_ROOT_PASSWORD", environment)
        self.assertNotIn("SSHPASS", environment)
        self.assertEqual(environment["PATH"], "/bin")

    def test_incus_forwards_stdin_and_timeout_without_shell(self):
        pipeline = self.pipeline()
        result = pipeline.incus(["exec", "owned", "--", "chpasswd"], input=f"root:{self.password}\n", timeout=7, capture=True)
        self.assertIs(result, self.subprocess.return_value)
        args, kwargs = self.subprocess.call_args
        self.assertEqual(args[0], ["incus", "exec", "owned", "--", "chpasswd"])
        self.assertEqual(kwargs["input"], f"root:{self.password}\n")
        self.assertEqual(kwargs["timeout"], 7)
        self.assertTrue(kwargs["text"])
        self.assertTrue(kwargs["check"])
        self.assertFalse(kwargs.get("shell", False))

    def test_incus_failure_does_not_expose_command_or_captured_output(self):
        for error in (
            subprocess.CalledProcessError(1, ["incus", self.password], output=self.password, stderr=self.password),
            subprocess.TimeoutExpired(["incus", self.password], 1, output=self.password, stderr=self.password),
        ):
            with self.subTest(error=type(error).__name__):
                self.subprocess.side_effect = error
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    with self.assertRaises(RuntimeError) as raised:
                        self.pipeline().incus(["exec", "owned", "--", "sh", "-c", self.password])
                self.assertNotIn(self.password, str(raised.exception))
                self.assertNotIn(self.password, output.getvalue())
                self.assertTrue(raised.exception.__suppress_context__)

    def test_incus_captures_output_even_without_capture_requested(self):
        self.pipeline().incus(["version"])
        kwargs = self.subprocess.call_args.kwargs
        self.assertTrue(kwargs.get("capture_output") or (kwargs.get("stdout") == subprocess.PIPE and kwargs.get("stderr") == subprocess.PIPE))
        self.assertGreater(kwargs["timeout"], 0)

    def test_setup_passes_password_only_to_chpasswd_stdin(self):
        for distro, release in RELEASES:
            with self.subTest(distro=distro, release=release):
                spec = spec_for(distro, release)
                pipeline = self.pipeline(specs=[spec])
                with patch.object(pipeline, "incus", return_value=completed()) as incus:
                    pipeline.setup_one(spec, "owned-container")
                password_calls = [item for item in incus.call_args_list if item.args[0] == ["exec", "owned-container", "--", "chpasswd"]]
                self.assertEqual(len(password_calls), 1)
                self.assertEqual(password_calls[0].kwargs.get("input"), f"root:{self.password}\n")
                for item in incus.call_args_list:
                    self.assertNotIn(self.password, " ".join(item.args[0]))

    def test_chpasswd_failure_stops_setup(self):
        pipeline = self.pipeline()

        def fail_password(args, **kwargs):
            if args[-1] == "chpasswd":
                raise RuntimeError("password setup failed")
            return completed()

        with patch.object(pipeline, "incus", side_effect=fail_password):
            with self.assertRaises(RuntimeError):
                pipeline.setup_one(self.spec, "owned-container")

    def test_package_commands_cover_all_supported_releases(self):
        for distro, release in RELEASES:
            with self.subTest(distro=distro, release=release):
                command = build_pipeline.Pipeline.package_install_command(spec_for(distro, release))
                self.assertNotIn("|| true", command)
                for package in build_pipeline.COMMON_PACKAGES.split():
                    self.assertIn(package, command)
                if distro == "alpine":
                    self.assertIn("apk add", command)
                elif distro in {"debian", "ubuntu"}:
                    self.assertRegex(command, r"apt(?:-get)? install")
                else:
                    self.assertIn("dnf", command)
                    self.assertIn("powertools" if release == "8" else "crb", command)
                    self.assertIn("epel", command.lower())
                    if distro == "centos":
                        major = release.split("-", 1)[0]
                        self.assertIn(f"https://dl.fedoraproject.org/pub/epel/epel-release-latest-{major}.noarch.rpm", command)

    def test_ca_bundle_is_explicitly_installed_refreshed_and_nonempty(self):
        for architecture in RUNNERS:
            for distro, release in RELEASES:
                with self.subTest(architecture=architecture, distro=distro, release=release):
                    command = build_pipeline.Pipeline.package_install_command(spec_for(distro, release, architecture))
                    self.assertIn("ca-certificates", command)
                    refresh, bundle = (("update-ca-trust extract", "/etc/pki/tls/certs/ca-bundle.crt")
                                       if distro in {"almalinux", "centos"} else
                                       ("update-ca-certificates", "/etc/ssl/certs/ca-certificates.crt"))
                    self.assertLess(command.index("ca-certificates"), command.index(refresh))
                    self.assertTrue(command.endswith(f"{refresh}\ntest -s {bundle}"))
                    self.assertTrue(command.startswith("set -eu\n"))
                    self.assertNotIn("|| true", command)

    def test_image_readme_uses_current_notice_domain(self):
        self.assertEqual(build_pipeline.README_CONTENT,
                         "本系统为vpsm91.com构建,本文件无实际作用可随意处理。")
        pipeline = self.pipeline()
        with patch.object(pipeline, "exec_container", return_value=completed()) as execute:
            pipeline.write_readmes()
        execute.assert_called_once()
        args, kwargs = execute.call_args
        self.assertEqual(args[0], pipeline.container_name(self.spec))
        self.assertIn("vpsm91.com", args[1])
        self.assertNotIn("vpsm.link", args[1])
        self.assertIn("> /root/README.md", args[1])
        self.assertTrue(kwargs["quiet"])

    def test_install_common_packages_targets_the_single_image(self):
        self.assertEqual(build_pipeline.COMMON_PACKAGES.split(),
                         ["bash", "git", "unzip", "screen", "wget", "curl", "sudo", "nano", "ca-certificates"])
        for architecture in RUNNERS:
            for distro, release in RELEASES:
                with self.subTest(architecture=architecture, distro=distro, release=release):
                    spec = spec_for(distro, release, architecture)
                    pipeline = self.pipeline(specs=[spec])
                    self.assertEqual(pipeline.containers, [pipeline.container_name(spec)])
                    with patch.object(pipeline, "exec_container", return_value=completed()) as execute:
                        pipeline.install_common_packages()
                    execute.assert_called_once_with(pipeline.container_name(spec), pipeline.package_install_command(spec))


class SetupReadinessTests(HermeticTestCase):
    def test_every_release_recovers_index_before_installing_the_single_image(self):
        self.fake_clock()
        for architecture in RUNNERS:
            for distro, release in RELEASES:
                with self.subTest(architecture=architecture, distro=distro, release=release):
                    spec = spec_for(distro, release, architecture)
                    pipeline = self.pipeline(specs=[spec])
                    attempts = {}
                    calls = []
                    index = ("apk update" if distro == "alpine" else
                             "apt-get -o APT::Update::Error-Mode=any update" if distro in {"debian", "ubuntu"} else
                             "dnf makecache")

                    def invoke(args, **kwargs):
                        calls.append((args, kwargs))
                        container, command = args[2], args[-1]
                        if command.endswith(index):
                            attempts[container] = attempts.get(container, 0) + 1
                            self.assertGreater(kwargs["timeout"], 0)
                            self.assertLessEqual(kwargs["timeout"], 30)
                            if attempts[container] == 1:
                                raise subprocess.CalledProcessError(1, args, stderr="temporary DNS failure")
                        else:
                            self.assertEqual(attempts[container], 2)
                        return completed()

                    self.subprocess.side_effect = invoke
                    pipeline.setup_containers()
                    self.assertEqual(attempts, {container: 2 for container in pipeline.containers})
                    for container in pipeline.containers:
                        commands = [args[-1] for args, _ in calls if args[2] == container]
                        self.assertEqual(commands[-1], "chpasswd")
                        self.assertEqual(sum("ssh-keygen -A" in command for command in commands), 1)
                        self.assertEqual(sum("sshd_config" in command for command in commands), 1)
                        self.assertEqual(sum("openssh" in command for command in commands), 1)
                        if distro in {"debian", "ubuntu"}:
                            self.assertTrue(all("export DEBIAN_FRONTEND=noninteractive" in command for command in commands[:-1]))
                            self.assertIn("(service ssh restart || systemctl restart ssh)", commands[-2])
                        else:
                            enable = "rc-update add sshd" if distro == "alpine" else "systemctl enable sshd"
                            restart = "rc-service sshd restart" if distro == "alpine" else "systemctl restart sshd"
                            self.assertTrue(commands[-3].endswith(enable))
                            self.assertTrue(commands[-2].endswith(restart))

    def test_index_failure_stops_at_deadline_without_install_or_password(self):
        now, sleep = self.fake_clock()
        pipeline = self.pipeline(ssh_timeout=5)
        self.subprocess.side_effect = subprocess.CalledProcessError(1, ["private argv"], stderr="DNS unavailable")
        with self.assertRaisesRegex(build_pipeline.SetupStepError, r"\[package-index\] readiness timed out"):
            pipeline.setup_one(self.spec, "owned")
        self.assertEqual(now[0], 5)
        self.assertEqual([item.kwargs["timeout"] for item in self.subprocess.call_args_list], [5, 3, 1])
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [2, 2, 1])
        self.assertTrue(all(item.args[0][-1].endswith(" update") for item in self.subprocess.call_args_list))

    def test_per_call_timeout_and_total_deadline_include_command_runtime(self):
        now, sleep = self.fake_clock()
        pipeline = self.pipeline(ssh_timeout=35, setup_diagnostics=True)

        def timeout(args, **kwargs):
            now[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(args, kwargs["timeout"], stderr=b"DNS still unavailable")

        self.subprocess.side_effect = timeout
        with self.assertRaises(build_pipeline.SetupStepError) as raised:
            pipeline.setup_one(self.spec, "owned")
        self.assertEqual(now[0], 35)
        self.assertEqual([item.kwargs["timeout"] for item in self.subprocess.call_args_list], [30, 3])
        sleep.assert_called_once_with(2)
        self.assertIn("DNS still unavailable", str(raised.exception))
        self.assertIn("external command timed out", str(raised.exception))

    def test_index_timeout_can_recover(self):
        self.fake_clock()
        pipeline = self.pipeline(ssh_timeout=5)
        self.subprocess.side_effect = [subprocess.TimeoutExpired(["private"], 1), completed()]
        pipeline.wait_for_package_index("owned", "apk update")
        self.assertEqual(self.subprocess.call_count, 2)

    def test_success_after_deadline_is_not_accepted(self):
        now, sleep = self.fake_clock()
        pipeline = self.pipeline(ssh_timeout=5)

        def late_success(*args, **kwargs):
            now[0] = 6
            return completed()

        self.subprocess.side_effect = late_success
        with self.assertRaisesRegex(build_pipeline.SetupStepError, "readiness timed out"):
            pipeline.wait_for_package_index("owned", "apk update")
        self.subprocess.assert_called_once()
        sleep.assert_not_called()

    def test_interrupts_are_not_retried(self):
        _, sleep = self.fake_clock()
        for error in (build_pipeline.BuildTerminated("terminated"), KeyboardInterrupt()):
            self.subprocess.reset_mock()
            self.subprocess.side_effect = error
            with self.assertRaises(type(error)):
                self.pipeline().setup_one(self.spec, "owned")
            self.subprocess.assert_called_once()
        sleep.assert_not_called()

    def test_non_index_steps_fail_once_with_labels(self):
        _, sleep = self.fake_clock()
        steps = [
            ("ssh-install", "apk add openssh"), ("ssh-host-keys", "ssh-keygen -A"),
            ("ssh-config", "sshd_config"), ("ssh-enable", "rc-update add sshd"),
            ("ssh-restart", "rc-service sshd restart"), ("chpasswd", "chpasswd"),
        ]
        for label, marker in steps:
            with self.subTest(label=label):
                self.subprocess.reset_mock()

                def fail_step(args, **kwargs):
                    if marker in args[-1]:
                        raise subprocess.CalledProcessError(7, args, stderr="setup failed")
                    return completed()

                self.subprocess.side_effect = fail_step
                with self.assertRaises(build_pipeline.SetupStepError) as raised:
                    self.pipeline().setup_one(spec_for("alpine", "3.21"), "owned")
                self.assertIn(f"[{label}]", str(raised.exception))
                self.assertIn("exit 7", str(raised.exception))
                self.assertEqual(sum(marker in item.args[0][-1] for item in self.subprocess.call_args_list), 1)
                self.assertIn(marker, self.subprocess.call_args.args[0][-1])
        sleep.assert_not_called()

    def test_nonfinite_or_nonpositive_timeout_rejected(self):
        self.native_tools()
        for timeout in (0, -1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.pipeline(ssh_timeout=timeout).preflight()
        self.subprocess.assert_not_called()


class SetupDiagnosticTests(HermeticTestCase):
    def test_diagnostics_require_explicit_cli_opt_in(self):
        args = ["--architecture", "arm64", "--distro", "alpine", "--release", "3.21"]
        self.assertFalse(build_pipeline.parse_args(args).setup_diagnostics)
        self.assertTrue(build_pipeline.parse_args([*args, "--setup-diagnostics"]).setup_diagnostics)
        for enabled in (False, True):
            with patch.object(build_pipeline.Pipeline, "run", autospec=True) as run:
                status = build_pipeline.main(["--manifest", str(self.manifest), *args,
                                             *(["--setup-diagnostics"] if enabled else [])])
                self.assertEqual(status, 0)
                self.assertEqual(run.call_args.args[0].setup_diagnostics, enabled)

    def test_default_setup_diagnostics_remain_opaque(self):
        self.subprocess.side_effect = subprocess.CalledProcessError(2, ["ARGV"], output="STDOUT", stderr="STDERR")
        with self.assertRaises(build_pipeline.SetupStepError) as raised:
            self.pipeline().setup_step("owned", "ssh-install", "apk add openssh")
        self.assertEqual(str(raised.exception), "setup step [ssh-install] failed: external command failed (exit 2)")
        self.assertTrue(raised.exception.__suppress_context__)

    def test_diagnostics_redact_all_credentials_escape_controls_and_prefix_lines(self):
        with patch.dict(os.environ, {"IMAGE_ROOT_PASSWORD": "inherited-image-secret", "SSHPASS": "inherited-ssh-secret"}):
            pipeline = self.pipeline(setup_diagnostics=True)
        raw = (f"DNS unavailable {self.password} inherited-image-secret inherited-ssh-secret\n"
               "::error::injected\r\x1b[2J\x00\x08\x85" + chr(0x2028) + chr(0x202E) + "\n##[error]injected")
        for error in (subprocess.CalledProcessError(1, ["PRIVATE_ARGV"], output="PRIVATE_STDOUT", stderr=raw),
                      subprocess.TimeoutExpired(["PRIVATE_ARGV"], 2, output=b"PRIVATE_STDOUT", stderr=raw.encode())):
            self.subprocess.side_effect = error
            with self.assertRaises(build_pipeline.SetupStepError) as raised:
                pipeline.setup_step("owned", "ssh-install", "PRIVATE_COMMAND")
            message = str(raised.exception)
            for private in (self.password, "inherited-image-secret", "inherited-ssh-secret", "PRIVATE_ARGV", "PRIVATE_STDOUT", "PRIVATE_COMMAND"):
                self.assertNotIn(private, message)
            self.assertIn("DNS unavailable", message)
            self.assertIn("[REDACTED]", message)
            self.assertNotIn("::", message)
            self.assertNotIn("##[", message)
            self.assertTrue(all(line.startswith("  setup stderr | ") for line in message.splitlines()[1:]))
            self.assertTrue(all(c == "\n" or c.isprintable() for c in message))
            for escaped in (r"\r", r"\x1b", r"\x00", r"\x08", r"\x85", ascii(chr(0x2028))[1:-1], ascii(chr(0x202E))[1:-1]):
                self.assertIn(escaped, message)
            self.assertTrue(raised.exception.__suppress_context__)

    def test_redaction_happens_before_clipping_and_output_is_bounded(self):
        pipeline = self.pipeline(setup_diagnostics=True, root_password="UNIQUE-PASSWORD-CROSSING-CLIP")
        text = pipeline.safe_setup_stderr("x" * 2020 + pipeline.root_password + "z" * 10000)
        self.assertNotIn("UNIQUE", text)
        self.assertIn("[truncated]", text)
        self.assertLess(len(text), 2100)
        text = pipeline.safe_setup_stderr("::error::injected\n" * 10000)
        self.assertLessEqual(len(text.splitlines()), 13)
        self.assertTrue(all(line.startswith("  setup stderr | ") for line in text.splitlines()))

    def test_chpasswd_remains_private_even_with_diagnostics_enabled(self):
        pipeline = self.pipeline(setup_diagnostics=True)
        self.subprocess.side_effect = subprocess.CalledProcessError(1, ["ARGV"], output="STDOUT", stderr="PRIVATE_PASSWORD_OUTPUT")
        with self.assertRaises(build_pipeline.SetupStepError) as raised:
            pipeline.setup_step("owned", "chpasswd")
        self.assertEqual(str(raised.exception), "setup step [chpasswd] failed: external command failed (exit 1)")
        self.assertEqual(self.subprocess.call_args.kwargs["input"], f"root:{self.password}\n")
        # Defense in depth: stdin-bearing calls cannot opt in accidentally either.
        with self.assertRaises(build_pipeline.IncusCommandError) as raised:
            pipeline.incus(["exec", "owned", "--", "chpasswd"], input="private", diagnostic_stderr=True)
        self.assertNotIn("PRIVATE_PASSWORD_OUTPUT", str(raised.exception))

    def test_other_incus_calls_remain_opaque_with_setup_diagnostics_enabled(self):
        pipeline = self.pipeline(setup_diagnostics=True)
        self.subprocess.side_effect = subprocess.CalledProcessError(1, ["ARGV"], output="STDOUT", stderr="PRIVATE_STDERR")
        for action in (lambda: pipeline.incus(["version"]),
                       lambda: pipeline.install_common_packages(), lambda: pipeline.cleanup_containers()):
            with self.assertRaises(build_pipeline.IncusCommandError) as raised:
                action()
            self.assertEqual(str(raised.exception), "external command failed (exit 1)")

    def test_safe_setup_error_printed_and_persisted_without_raw_main_errors(self):
        pipeline = self.pipeline(setup_diagnostics=True)
        self.subprocess.side_effect = subprocess.CalledProcessError(9, ["PRIVATE_ARGV"], output="PRIVATE_STDOUT", stderr=f"DNS unavailable {self.password}\n::error::injected")
        log = io.StringIO()
        with patch.object(pipeline, "preflight"), patch.object(pipeline, "download_images"), patch.object(pipeline, "launch_containers"), patch.object(pipeline, "setup_containers", side_effect=lambda: pipeline.setup_step("owned", "ssh-install", "apk add openssh")), patch.object(pipeline, "final_cleanup"), contextlib.redirect_stderr(log):
            with self.assertRaises(RuntimeError) as raised:
                pipeline.run()
        summary = json.loads((self.output / "build-summary.json").read_text())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["error"], str(raised.exception))
        self.assertIn(summary["error"], log.getvalue())
        self.assertIn("DNS unavailable", log.getvalue())
        self.assertIn("[ssh-install]", log.getvalue())
        for private in (self.password, "PRIVATE_ARGV", "PRIVATE_STDOUT"):
            self.assertNotIn(private, log.getvalue() + json.dumps(summary))
        self.assertTrue(raised.exception.__suppress_context__)
        with patch.object(build_pipeline.Pipeline, "run", side_effect=RuntimeError("UNSAFE_MAIN_ERROR")), contextlib.redirect_stderr(log):
            status = build_pipeline.main(["--manifest", str(self.manifest), "--architecture", "arm64", "--distro", "debian", "--release", "bookworm", "--setup-diagnostics"])
        self.assertEqual(status, 1)
        self.assertNotIn("UNSAFE_MAIN_ERROR", log.getvalue())


class OwnershipTests(HermeticTestCase):
    def test_failed_copy_does_not_record_alias(self):
        pipeline = self.pipeline()
        with patch.object(pipeline, "incus", side_effect=RuntimeError("copy failed")):
            with self.assertRaises(RuntimeError):
                pipeline.download_images()
        self.assertEqual(pipeline.created_base_aliases, [])

    def test_successful_copy_records_only_created_alias(self):
        pipeline = self.pipeline()
        with patch.object(pipeline, "incus", return_value=completed()):
            pipeline.download_images()
        self.assertEqual(pipeline.created_base_aliases, [pipeline.base_alias(self.spec)])

    def test_partial_launch_records_only_successful_creation(self):
        pipeline = self.pipeline()
        with patch.object(pipeline, "incus", side_effect=[completed(), RuntimeError("launch failed")]):
            with self.assertRaises(RuntimeError):
                pipeline.launch_containers()
        self.assertEqual(pipeline.created_containers, [pipeline.container_name(self.spec)])

    def test_init_failure_does_not_record_container(self):
        pipeline = self.pipeline()
        with patch.object(pipeline, "incus", side_effect=RuntimeError("init failed")):
            with self.assertRaises(RuntimeError):
                pipeline.launch_containers()
        self.assertEqual(pipeline.created_containers, [])

    def test_launch_creates_and_starts_exactly_one_container(self):
        pipeline = self.pipeline()
        name = pipeline.container_name(self.spec)
        with patch.object(pipeline, "incus", return_value=completed()) as incus, patch.object(pipeline, "wait_for_container") as wait:
            pipeline.launch_containers()
        self.assertEqual(incus.call_args_list, [
            call(["init", pipeline.base_alias(self.spec), name]), call(["start", name]),
        ])
        wait.assert_called_once_with(name)
        self.assertEqual(pipeline.created_containers, [name])

    def test_final_cleanup_only_deletes_recorded_owned_resources(self):
        pipeline = self.pipeline()
        pipeline.created_containers.append("owned-container")
        pipeline.created_base_aliases.append("owned-base-alias")
        pipeline.created_published_aliases.append("owned-published-alias")
        with patch.object(pipeline, "incus", return_value=completed()) as incus:
            pipeline.final_cleanup()
        commands = [item.args[0] for item in incus.call_args_list]
        commands += [item.args[0][1:] for item in self.subprocess.call_args_list]
        self.assertCountEqual(commands, [
            ["delete", "owned-container", "--force"],
            ["image", "alias", "delete", "owned-base-alias"],
            ["image", "alias", "delete", "owned-published-alias"],
        ])
        self.assertFalse(any(command[:2] == ["image", "delete"] for command in commands))

    def test_empty_ownership_lists_never_delete_guessed_names(self):
        pipeline = self.pipeline()
        with patch.object(pipeline, "incus") as incus:
            pipeline.final_cleanup()
        incus.assert_not_called()
        self.subprocess.assert_not_called()

    def test_keep_resources_skips_deletions(self):
        pipeline = self.pipeline(keep_resources=True)
        pipeline.created_containers.append("owned")
        pipeline.created_base_aliases.append("owned-base")
        pipeline.created_published_aliases.append("owned-published")
        with patch.object(pipeline, "incus") as incus:
            pipeline.final_cleanup()
        incus.assert_not_called()
        self.subprocess.assert_not_called()

    def test_cleanup_continues_after_a_delete_failure(self):
        pipeline = self.pipeline()
        pipeline.created_containers.extend(["owned-a", "owned-b"])
        pipeline.created_base_aliases.append("owned-base")
        with patch.object(pipeline, "incus", side_effect=[RuntimeError("gone"), completed(), completed()]) as incus:
            self.subprocess.side_effect = [OSError("gone"), completed(), completed()]
            pipeline.final_cleanup()
        count = incus.call_count + self.subprocess.call_count
        self.assertEqual(count, 3)


class ReadinessAndSshTests(HermeticTestCase):
    def test_extract_ip_ignores_ipv6_and_loopback(self):
        self.assertEqual(build_pipeline.Pipeline.extract_ip("eth0, 127.0.0.1, 10.0.0.12 (global), ::1"), "10.0.0.12")
        self.assertIsNone(build_pipeline.Pipeline.extract_ip("127.0.0.1, ::1, fe80::1"))

    def test_wait_for_container_retries_exec_until_ready(self):
        pipeline = self.pipeline(ssh_timeout=10)
        _, sleep = self.fake_clock()
        with patch.object(pipeline, "incus", side_effect=[RuntimeError("booting"), completed()]) as incus:
            pipeline.wait_for_container("owned")
        self.assertEqual(incus.call_count, 2)
        self.assertTrue(all(item.args[0][:3] == ["exec", "owned", "--"] for item in incus.call_args_list))
        sleep.assert_called()

    def test_wait_for_container_obeys_deadline(self):
        pipeline = self.pipeline(ssh_timeout=2)
        _, sleep = self.fake_clock()
        with patch.object(pipeline, "incus", side_effect=RuntimeError("booting")):
            with self.assertRaises(RuntimeError):
                pipeline.wait_for_container("owned")
        sleep.assert_called()

    def test_launch_waits_for_boot_before_setup(self):
        pipeline = self.pipeline()
        events = []
        with patch.object(pipeline, "incus", return_value=completed()), patch.object(pipeline, "wait_for_container", side_effect=lambda container: events.append(("wait", container))), patch.object(pipeline, "setup_one", side_effect=lambda spec, container: events.append(("setup", container))):
            pipeline.launch_containers()
            pipeline.setup_containers()
        self.assertEqual(events, [("wait", container) for container in pipeline.containers] + [("setup", container) for container in pipeline.containers])

    def test_ssh_retries_missing_ip_and_login_then_succeeds(self):
        pipeline = self.pipeline(ssh_timeout=20)
        _, sleep = self.fake_clock()
        self.subprocess.side_effect = [completed(returncode=255), completed("root\n")]
        with patch.object(pipeline, "container_ip", side_effect=[None, "10.0.0.2", "10.0.0.2"]):
            pipeline.test_ssh()
        self.assertEqual(self.subprocess.call_count, 2)
        self.assertGreaterEqual(sleep.call_count, 2)
        for item in self.subprocess.call_args_list:
            self.assertEqual(item.kwargs["env"]["SSHPASS"], self.password)
            self.assertNotIn(self.password, item.args[0])
            self.assertGreater(item.kwargs["timeout"], 0)
            self.assertIn("-e", item.args[0])
            self.assertNotIn("-p", item.args[0])

    def test_ssh_retries_subprocess_timeout(self):
        pipeline = self.pipeline(ssh_timeout=20)
        self.fake_clock()
        self.subprocess.side_effect = [subprocess.TimeoutExpired(["sshpass"], 1), completed("root\n")]
        with patch.object(pipeline, "container_ip", return_value="10.0.0.2"):
            pipeline.test_ssh()
        self.assertEqual(self.subprocess.call_count, 2)

    def test_ssh_deadline_rejects_missing_ip_or_non_root_login(self):
        for ip, stdout in ((None, ""), ("10.0.0.2", "not-root\n")):
            with self.subTest(ip=ip):
                pipeline = self.pipeline(ssh_timeout=2)
                with patch("build_pipeline.time.monotonic") as monotonic, patch("build_pipeline.time.sleep") as sleep:
                    now = [0.0]
                    monotonic.side_effect = lambda: now[0]
                    sleep.side_effect = lambda seconds: now.__setitem__(0, now[0] + max(seconds, 1))
                    self.subprocess.return_value = completed(stdout)
                    with patch.object(pipeline, "container_ip", return_value=ip):
                        with self.assertRaises(RuntimeError):
                            pipeline.test_ssh()
                self.assertLessEqual(now[0], 20)

    def test_sigterm_is_not_swallowed_by_readiness_retries(self):
        pipeline = self.pipeline(ssh_timeout=2)
        _, sleep = self.fake_clock()
        with patch.object(pipeline, "incus", side_effect=build_pipeline.BuildTerminated("SIGTERM")):
            with self.assertRaises(build_pipeline.BuildTerminated):
                pipeline.wait_for_container("owned")
        sleep.assert_not_called()

    def test_sigterm_is_not_swallowed_by_ssh_retries(self):
        pipeline = self.pipeline(ssh_timeout=2)
        _, sleep = self.fake_clock()
        with patch.object(pipeline, "container_ip", side_effect=build_pipeline.BuildTerminated("SIGTERM")):
            with self.assertRaises(build_pipeline.BuildTerminated):
                pipeline.test_ssh()
        sleep.assert_not_called()

    def test_skip_ssh_does_not_probe_ip_or_execute_commands(self):
        pipeline = self.pipeline(skip_ssh_test=True)
        with patch.object(pipeline, "container_ip") as ip:
            pipeline.test_ssh()
        ip.assert_not_called()
        self.subprocess.assert_not_called()


class SanitizationAndExportTests(HermeticTestCase):
    def prepared_pipeline(self, **kwargs):
        pipeline = self.pipeline(**kwargs)
        self.output.mkdir(exist_ok=True)
        pipeline.output_ready = True
        pipeline.sanitized_containers.update(pipeline.containers)
        return pipeline

    def export_callback(self, mode="single", fail_export=None):
        exports = []

        def invoke(args, **kwargs):
            if args[:2] != ["image", "export"]:
                return completed()
            prefix = Path(args[3])
            self.assertNotEqual(prefix.parent, self.output)
            self.assertIn(self.output, prefix.parents)
            self.assertTrue(prefix.parent.is_dir())
            exports.append(prefix)
            if fail_export == len(exports):
                Path(str(prefix) + ".tar.xz").write_bytes(b"partial")
                raise RuntimeError("export interrupted")
            archive = Path(str(prefix) + ".tar.xz")
            if mode == "single":
                make_archive(archive, ["metadata.yaml", "rootfs/etc/os-release"])
            elif mode == "split":
                make_archive(archive, ["metadata.yaml"])
                make_archive(Path(str(prefix) + ".rootfs.tar.xz"), ["etc/os-release"])
            elif mode == "split-root":
                make_archive(prefix, ["metadata.yaml"])
                make_archive(Path(str(prefix) + ".root"), ["etc/os-release"])
            elif mode == "metadata-only":
                make_archive(prefix, ["metadata.yaml"])
            elif mode == "empty":
                archive.touch()
            elif mode == "symlink":
                target = self.root / "outside.tar.xz"
                make_archive(target, ["metadata.yaml", "rootfs/etc/os-release"])
                archive.symlink_to(target)
            elif mode == "directory":
                archive.mkdir()
            elif mode != "missing":
                raise AssertionError(f"unknown export fixture: {mode}")
            return completed()

        return invoke, exports

    def test_cleanup_locks_root_removes_credentials_and_records_sanitization(self):
        for distro, release in RELEASES:
            with self.subTest(distro=distro, release=release):
                pipeline = self.pipeline(specs=[spec_for(distro, release)])
                with patch.object(pipeline, "exec_container", return_value=completed()) as execute:
                    pipeline.cleanup_containers()
                self.assertEqual(pipeline.sanitized_containers, set(pipeline.containers))
                for item in execute.call_args_list:
                    command = item.args[1]
                    self.assertIn("root:!", command)
                    self.assertIn("chpasswd -e", command)
                    self.assertIn("/etc/shadow-", command)
                    self.assertIn("/root/.ssh", command)
                    self.assertNotIn(self.password, command)

    def test_failed_sanitization_does_not_mark_container_safe(self):
        pipeline = self.pipeline()
        with patch.object(pipeline, "exec_container", side_effect=RuntimeError("cleanup failed")):
            with self.assertRaises(RuntimeError):
                pipeline.cleanup_containers()
        self.assertEqual(pipeline.sanitized_containers, set())

    def test_unsanitized_container_cannot_be_published(self):
        pipeline = self.pipeline()
        with patch.object(pipeline, "incus") as incus:
            with self.assertRaises(RuntimeError):
                pipeline.publish_and_export()
        self.assertFalse(any(item.args[0][0] == "publish" for item in incus.call_args_list))
        self.assertEqual(pipeline.created_published_aliases, [])
        self.assertEqual(pipeline.exported_files, [])

    def test_single_and_split_exports_record_only_actual_verified_files(self):
        for mode, suffixes in (("single", [".tar.xz"]), ("split", [".tar.xz", ".rootfs.tar.xz"]), ("split-root", ["", ".root"])):
            with self.subTest(mode=mode):
                # Give each mode its own empty output directory.
                self.output = self.root / mode
                pipeline = self.prepared_pipeline()
                callback, exports = self.export_callback(mode)
                with patch.object(pipeline, "incus", side_effect=callback), patch.object(pipeline, "write_checksums", wraps=pipeline.write_checksums) as checksums:
                    pipeline.publish_and_export()
                expected = [self.spec.container_name() + suffix for suffix in suffixes]
                self.assertCountEqual(pipeline.exported_files, expected)
                self.assertCountEqual(pipeline.created_published_aliases, pipeline.containers)
                self.assertEqual(checksums.call_count, 1)
                self.assertEqual(len(exports), 1)
                self.assertTrue(all(not prefix.parent.exists() for prefix in exports))
                self.assertCountEqual([path.name for path in self.output.iterdir()], [*expected, "SHA256SUMS"])
                actual_checksums = {}
                for line in (self.output / "SHA256SUMS").read_text().splitlines():
                    digest, filename = line.split(None, 1)
                    actual_checksums[filename.lstrip(" *")] = digest
                self.assertEqual(actual_checksums, {name: hashlib.sha256((self.output / name).read_bytes()).hexdigest() for name in expected})

    def test_invalid_exports_are_not_promoted_or_invented(self):
        for mode in ("missing", "empty", "metadata-only", "symlink", "directory"):
            with self.subTest(mode=mode):
                self.output = self.root / mode
                pipeline = self.prepared_pipeline()
                callback, exports = self.export_callback(mode)
                with patch.object(pipeline, "incus", side_effect=callback):
                    with self.assertRaises((ValueError, RuntimeError)):
                        pipeline.publish_and_export()
                self.assertEqual(pipeline.exported_files, [])
                self.assertEqual(list(self.output.iterdir()), [])
                self.assertTrue(all(not prefix.parent.exists() for prefix in exports))

    def test_failed_export_does_not_promote_partial_files_or_checksums(self):
        pipeline = self.prepared_pipeline()
        callback, exports = self.export_callback(fail_export=1)
        with patch.object(pipeline, "incus", side_effect=callback):
            with self.assertRaises(RuntimeError):
                pipeline.publish_and_export()
        self.assertEqual(pipeline.exported_files, [])
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(len(exports), 1)
        self.assertFalse(exports[0].parent.exists())

    def test_publish_failure_does_not_record_uncreated_alias(self):
        pipeline = self.prepared_pipeline()

        def fail_publish(args, **kwargs):
            if args[0] == "publish":
                raise RuntimeError("publish failed")
            return completed()

        with patch.object(pipeline, "incus", side_effect=fail_publish):
            with self.assertRaises(RuntimeError):
                pipeline.publish_and_export()
        self.assertEqual(pipeline.created_published_aliases, [])
        self.assertEqual(pipeline.exported_files, [])

    def test_summary_contains_actual_architecture_and_verified_exports(self):
        pipeline = self.prepared_pipeline(specs=[spec_for(architecture="amd64")])
        pipeline.exported_files.append("verified.tar.xz")
        (self.output / "verified.tar.xz").write_bytes(b"verified")
        pipeline.write_summary("success")
        summary = json.loads((self.output / "build-summary.json").read_text())
        self.assertEqual(summary["architecture"], "amd64")
        self.assertEqual(summary["exported_files"], ["verified.tar.xz"])
        self.assertEqual(summary["status"], "success")
        self.assertEqual(len(summary["images"]), 1)
        self.assertNotIn("variants", summary)
        self.assertNotIn(self.password, json.dumps(summary))


class RunLifecycleTests(HermeticTestCase):
    STAGES = (
        "download_images", "launch_containers", "setup_containers", "install_common_packages",
        "write_readmes", "test_ssh", "cleanup_containers", "publish_and_export",
    )

    def mock_stages(self, pipeline):
        return {name: self.enterContext(patch.object(pipeline, name)) for name in self.STAGES}

    def test_preflight_failure_runs_no_cleanup_commands_or_summary(self):
        pipeline = self.pipeline()
        stages = self.mock_stages(pipeline)
        with patch.object(pipeline, "preflight", side_effect=RuntimeError("preflight failed")), patch.object(pipeline, "write_summary") as summary:
            with self.assertRaises(RuntimeError):
                pipeline.run()
        summary.assert_not_called()
        for stage in stages.values():
            stage.assert_not_called()
        self.subprocess.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_run_nonempty_output_preserves_existing_files_and_no_summary(self):
        self.native_tools()
        self.output.mkdir()
        sentinel = self.output / "existing"
        sentinel.write_text("keep", encoding="utf-8")
        pipeline = self.pipeline()
        with patch.object(pipeline, "write_summary") as summary:
            with self.assertRaises((ValueError, RuntimeError)):
                pipeline.run()
        summary.assert_not_called()
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertEqual([item.name for item in self.output.iterdir()], ["existing"])
        self.assertFalse(any(item.args[0][:2] == ["incus", "delete"] for item in self.subprocess.call_args_list))

    def test_success_runs_stages_in_order_then_cleanup_and_summary(self):
        pipeline = self.pipeline()
        self.output.mkdir()
        pipeline.output_ready = True
        events = []
        for name in ("preflight", *self.STAGES, "write_summary", "final_cleanup"):
            self.enterContext(patch.object(pipeline, name, side_effect=lambda *args, _name=name, **kwargs: events.append((_name, args))))
        pipeline.run()
        self.assertEqual([name for name, args in events], ["preflight", *self.STAGES, "final_cleanup", "write_summary"])
        self.assertEqual(next(args for name, args in events if name == "write_summary")[0], "success")

    def test_each_stage_failure_writes_failure_summary_and_cleans(self):
        for failed in self.STAGES:
            with self.subTest(stage=failed), contextlib.ExitStack() as stack:
                self.output = self.root / failed
                pipeline = self.pipeline()
                self.output.mkdir()
                pipeline.output_ready = True
                stack.enter_context(patch.object(pipeline, "preflight"))
                stages = {name: stack.enter_context(patch.object(pipeline, name)) for name in self.STAGES}
                stages[failed].side_effect = RuntimeError("controlled stage failure")
                cleanup = stack.enter_context(patch.object(pipeline, "final_cleanup"))
                with self.assertRaises(RuntimeError):
                    pipeline.run()
                cleanup.assert_called_once()
                summary = json.loads((self.output / "build-summary.json").read_text())
                self.assertEqual(summary["status"], "failed")
                self.assertIn("controlled stage failure", summary["error"])
                for name in self.STAGES[self.STAGES.index(failed) + 1:]:
                    stages[name].assert_not_called()

    def test_summary_write_failure_cannot_skip_final_cleanup(self):
        pipeline = self.pipeline()
        pipeline.output_ready = True
        self.mock_stages(pipeline)
        with patch.object(pipeline, "preflight"), patch.object(pipeline, "write_summary", side_effect=OSError("disk full")), patch.object(pipeline, "final_cleanup") as cleanup:
            with self.assertRaises(RuntimeError):
                pipeline.run()
        cleanup.assert_called_once()

    def test_sigterm_handler_raises_build_terminated(self):
        with self.assertRaises(build_pipeline.BuildTerminated):
            build_pipeline.Pipeline._handle_sigterm(signal.SIGTERM, None)

    def test_run_installs_handler_and_restores_previous_handler_after_termination(self):
        pipeline = self.pipeline()
        stages = self.mock_stages(pipeline)
        installed = {}
        previous_handler = object()

        def register(signum, handler):
            self.assertEqual(signum, signal.SIGTERM)
            installed["handler"] = handler
            return previous_handler

        def terminate():
            installed["handler"](signal.SIGTERM, None)

        stages["setup_containers"].side_effect = terminate
        with patch.object(pipeline, "preflight"), patch.object(pipeline, "final_cleanup") as cleanup, patch("build_pipeline.signal.signal", side_effect=register) as register_mock:
            with self.assertRaises(RuntimeError):
                pipeline.run()
        cleanup.assert_called_once()
        self.assertEqual(register_mock.call_args_list, [
            call(signal.SIGTERM, pipeline._handle_sigterm),
            call(signal.SIGTERM, signal.SIG_IGN),
            call(signal.SIGTERM, previous_handler),
        ])
        self.assertTrue(pipeline.output_ready)
        self.assertEqual(json.loads((self.output / "build-summary.json").read_text())["status"], "failed")

    def test_termination_during_stage_still_cleans_owned_resources(self):
        pipeline = self.pipeline()
        self.output.mkdir()
        pipeline.output_ready = True
        stages = self.mock_stages(pipeline)
        stages["setup_containers"].side_effect = build_pipeline.BuildTerminated("terminated")
        with patch.object(pipeline, "preflight"), patch.object(pipeline, "final_cleanup") as cleanup:
            with self.assertRaises(RuntimeError):
                pipeline.run()
        cleanup.assert_called_once()
        stages["publish_and_export"].assert_not_called()
        summary = json.loads((self.output / "build-summary.json").read_text())
        self.assertEqual(summary["status"], "failed")


if __name__ == "__main__":
    unittest.main()
