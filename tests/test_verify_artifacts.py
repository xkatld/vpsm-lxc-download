import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.verify_artifacts import main, release_configured, verify_artifact


class ArtifactTests(unittest.TestCase):
    row = {"distro": "alpine", "release": "3.24", "architecture": "amd64"}
    prefix = "alpine324-amd64-lxc"

    def fixture(self, root, files=None, row=None):
        if files is None:
            files = [self.prefix + ".tar.gz"]
        row = self.row if row is None else row
        for name in files:
            (root / name).write_bytes(b"test image")
        summary = {"status": "success", "architecture": row["architecture"], "images": [row], "exported_files": files}
        (root / "build-summary.json").write_text(json.dumps(summary))
        digest = hashlib.sha256(b"test image").hexdigest()
        (root / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name in files))
        return files

    def test_success_with_exact_expected_names(self):
        cases = [
            (self.row, "alpine324-amd64-lxc.tar.gz"),
            ({"distro": "debian", "release": "bookworm", "architecture": "arm64"}, "debian12-arm64-lxc.tar.gz"),
            ({"distro": "centos", "release": "9-Stream", "architecture": "amd64"}, "centos9-stream-amd64-lxc.tar.gz"),
        ]
        for row, filename in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.fixture(root, [filename], row)
                self.assertEqual(verify_artifact(root, row), 1)

    def test_supported_single_image_export_sets(self):
        suffixes = [".tar", ".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst"]
        cases = [[suffix] for suffix in suffixes] + [["", ".root"]]
        cases += [[metadata, ".rootfs" + rootfs] for metadata in suffixes for rootfs in suffixes]
        for extensions in cases:
            with self.subTest(extensions=extensions), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                files = [self.prefix + suffix for suffix in extensions]
                self.fixture(root, files)
                self.assertEqual(verify_artifact(root, self.row), len(files))

    def test_incomplete_or_multiple_image_export_sets_fail(self):
        for suffixes in ([""], [".root"], [".rootfs.tar.xz"], [".tar.gz", ".tar.xz"],
                         [".tar.gz", ".root"], [".tar.gz", ".rootfs.tar.xz", ".root"], [".unexpected"]):
            with self.subTest(suffixes=suffixes), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.fixture(root, [self.prefix + suffix for suffix in suffixes])
                with self.assertRaisesRegex(ValueError, "导出文件组合不符合预期"):
                    verify_artifact(root, self.row)

    def test_old_flavors_and_inexact_prefixes_fail(self):
        for filename in ("alpine324-all-amd64-lxc.tar.gz", "alpine324-lite-amd64-lxc.tar.gz",
                         "alpine324-arm64-lxc.tar.gz", "alpine323-amd64-lxc.tar.gz",
                         "alpine324-amd64-lxc-extra.tar.gz", "alpine324-amd64-lxc2.tar.gz"):
            for include_expected in (False, True):
                with self.subTest(filename=filename, include_expected=include_expected), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    files = [filename] + ([self.prefix + ".tar.gz"] if include_expected else [])
                    self.fixture(root, files)
                    with self.assertRaisesRegex(ValueError, "镜像导出文件不符合预期"):
                        verify_artifact(root, self.row)

    def test_corrupt_export_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = self.fixture(root)
            (root / files[0]).write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "校验和不匹配"):
                verify_artifact(root, self.row)

    def test_missing_export_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = self.fixture(root)
            (root / files[0]).unlink()
            with self.assertRaisesRegex(ValueError, "导出文件缺失或为空"):
                verify_artifact(root, self.row)

    def test_empty_or_duplicate_export_list_fails(self):
        for files in ([], [self.prefix + ".tar.gz"] * 2):
            with self.subTest(files=files), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.fixture(root, files)
                with self.assertRaisesRegex(ValueError, "导出文件缺失或重复"):
                    verify_artifact(root, self.row)

    def test_checksum_set_must_exactly_match_exported_files(self):
        for change in ("missing", "extra", "renamed", "duplicate", "malformed"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                files = self.fixture(root, [self.prefix, self.prefix + ".root"])
                path = root / "SHA256SUMS"
                lines = path.read_text().splitlines(keepends=True)
                if change == "missing":
                    lines.pop()
                elif change == "extra":
                    lines.append(lines[0].replace(files[0], "stale.tar.gz"))
                elif change == "renamed":
                    lines[0] = lines[0].replace(files[0], "stale.tar.gz")
                elif change == "duplicate":
                    lines.append(lines[0])
                else:
                    lines[0] = "invalid checksum entry\n"
                path.write_text("".join(lines))
                with self.assertRaisesRegex(ValueError, "校验和"):
                    verify_artifact(root, self.row)

    def test_extra_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / "stale.tar.xz").write_bytes(b"old")
            with self.assertRaisesRegex(ValueError, "产物文件多出或缺失"):
                verify_artifact(root, self.row)

    def test_unsafe_filename_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            path = root / "build-summary.json"
            summary = json.loads(path.read_text())
            summary["exported_files"] = ["../outside"]
            path.write_text(json.dumps(summary))
            with self.assertRaisesRegex(ValueError, "导出文件名不安全"):
                verify_artifact(root, self.row)

    def test_wrong_architecture_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            with self.assertRaisesRegex(ValueError, "架构不匹配"):
                verify_artifact(root, {**self.row, "architecture": "arm64"})

    def test_downloads_each_artifact_then_removes_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            matrix = Path(directory) / "matrix.json"
            matrix.write_text(json.dumps({"include": [self.row]}))
            downloaded = []
            output = io.StringIO()

            def download(command, **kwargs):
                self.assertIn("--name", command)
                root = Path(command[command.index("--dir") + 1])
                self.assertEqual(list(root.iterdir()), [])
                downloaded.append(root)
                self.fixture(root)

            with patch("sys.argv", ["verify", "--matrix", str(matrix), "--run-id", "123"]), \
                 patch("scripts.verify_artifacts.subprocess.run", side_effect=download), \
                 patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(output):
                self.assertEqual(main(), 0)
            self.assertIn("已校验 1 个架构与版本构建任务、1 个镜像、1 个导出文件。", output.getvalue())
            self.assertFalse(downloaded[0].exists())

    def test_release_configured_requires_repo_tag_and_token(self):
        self.assertTrue(release_configured("xkatld/vpsm", "lxc-images", "token"))
        for repo, tag, token in (("", "lxc-images", "token"), ("xkatld/vpsm", "", "token"),
                                 ("xkatld/vpsm", "lxc-images", "")):
            with self.subTest(repo=repo, tag=tag, token=token):
                self.assertFalse(release_configured(repo, tag, token))

    def run_main(self, matrix, extra_args, environment, release_exists=True):
        uploads, created, commands = [], [], []

        def run(command, **kwargs):
            commands.append(list(command))
            if command[:3] == ["gh", "run", "download"]:
                self.fixture(Path(command[command.index("--dir") + 1]))
            elif command[:3] == ["gh", "release", "view"]:
                return subprocess.CompletedProcess(command, 0 if release_exists else 1, "", "")
            elif command[:3] == ["gh", "release", "create"]:
                created.append(list(command))
            elif command[:3] == ["gh", "release", "upload"]:
                self.assertEqual(kwargs.get("env", {}).get("GH_TOKEN"), environment.get("RELEASE_TOKEN"))
                uploads.append(list(command))
            else:
                self.fail("unexpected command: " + " ".join(command))
            return subprocess.CompletedProcess(command, 0, "", "")

        output = io.StringIO()
        argv = ["verify", "--matrix", str(matrix), "--run-id", "123", *extra_args]
        with patch("sys.argv", argv), \
             patch("scripts.verify_artifacts.subprocess.run", side_effect=run), \
             patch.dict("os.environ", environment, clear=True), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        return uploads, created, commands, output.getvalue()

    def test_publishes_each_artifact_and_combined_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            matrix = Path(directory) / "matrix.json"
            matrix.write_text(json.dumps({"include": [self.row]}))
            uploads, created, commands, _ = self.run_main(
                matrix, ["--release-repo", "xkatld/vpsm", "--release-tag", "lxc-images"],
                {"RELEASE_TOKEN": "secret-token"},
            )
        self.assertEqual(created, [])
        self.assertEqual(commands.count(["gh", "release", "view", "lxc-images", "--repo", "xkatld/vpsm"]), 1)
        self.assertEqual(len(uploads), 2)
        for upload in uploads:
            self.assertEqual(upload[3], "lxc-images")
            self.assertEqual(upload[-3:], ["--repo", "xkatld/vpsm", "--clobber"])
        self.assertTrue(uploads[0][4].endswith(self.prefix + ".tar.gz"))
        self.assertTrue(uploads[1][4].endswith("SHA256SUMS"))

    def test_release_is_created_when_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            matrix = Path(directory) / "matrix.json"
            matrix.write_text(json.dumps({"include": [self.row]}))
            _, created, _, _ = self.run_main(
                matrix, ["--release-repo", "xkatld/vpsm", "--release-tag", "lxc-images"],
                {"RELEASE_TOKEN": "secret-token"}, release_exists=False,
            )
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][:3], ["gh", "release", "create"])
        self.assertIn("--notes", created[0])

    def test_release_skipped_without_token(self):
        with tempfile.TemporaryDirectory() as directory:
            matrix = Path(directory) / "matrix.json"
            matrix.write_text(json.dumps({"include": [self.row]}))
            uploads, created, commands, output = self.run_main(
                matrix, ["--release-repo", "xkatld/vpsm", "--release-tag", "lxc-images"], {},
            )
        self.assertEqual(uploads, [])
        self.assertEqual(created, [])
        self.assertFalse(any(command[:2] == ["gh", "release"] for command in commands))
        self.assertIn("跳过发布", output)
