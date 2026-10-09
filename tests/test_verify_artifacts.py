import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.verify_artifacts import main, verify_artifact


class ArtifactTests(unittest.TestCase):
    row = {"distro": "alpine", "release": "3.24", "architecture": "amd64"}

    def fixture(self, root):
        files = [f"alpine324-{flavor}-amd64-lxc.tar.xz" for flavor in ("all", "lite")]
        for name in files:
            (root / name).write_bytes(b"test image")
        summary = {"status": "success", "architecture": "amd64", "images": [self.row], "exported_files": files}
        (root / "build-summary.json").write_text(json.dumps(summary))
        digest = hashlib.sha256(b"test image").hexdigest()
        (root / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name in files))
        return files

    def test_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.assertEqual(verify_artifact(root, self.row), 2)

    def test_incus_extensionless_split_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = [f"alpine324-{flavor}-amd64-lxc{suffix}"
                     for flavor in ("all", "lite") for suffix in ("", ".root")]
            for name in files:
                (root / name).write_bytes(b"test image")
            summary = {"status": "success", "architecture": "amd64", "images": [self.row], "exported_files": files}
            (root / "build-summary.json").write_text(json.dumps(summary))
            digest = hashlib.sha256(b"test image").hexdigest()
            (root / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name in files))
            self.assertEqual(verify_artifact(root, self.row), 4)

    def test_corrupt_export_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = self.fixture(root)
            (root / files[0]).write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                verify_artifact(root, self.row)

    def test_missing_flavor_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = self.fixture(root)
            (root / files[0]).unlink()
            with self.assertRaises(ValueError):
                verify_artifact(root, self.row)

    def test_extra_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / "stale.tar.xz").write_bytes(b"old")
            with self.assertRaisesRegex(ValueError, "unexpected or missing"):
                verify_artifact(root, self.row)

    def test_unsafe_filename_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            path = root / "build-summary.json"
            summary = json.loads(path.read_text())
            summary["exported_files"] = ["../outside"]
            path.write_text(json.dumps(summary))
            with self.assertRaisesRegex(ValueError, "unsafe"):
                verify_artifact(root, self.row)

    def test_wrong_architecture_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            with self.assertRaisesRegex(ValueError, "wrong architecture"):
                verify_artifact(root, {**self.row, "architecture": "arm64"})

    def test_downloads_each_artifact_then_removes_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            matrix = Path(directory) / "matrix.json"
            matrix.write_text(json.dumps({"include": [self.row]}))
            downloaded = []

            def download(command, **kwargs):
                self.assertIn("--name", command)
                root = Path(command[command.index("--dir") + 1])
                downloaded.append(root)
                self.fixture(root)

            with patch("sys.argv", ["verify", "--matrix", str(matrix), "--run-id", "123"]), \
                 patch("scripts.verify_artifacts.subprocess.run", side_effect=download), \
                 patch.dict("os.environ", {}, clear=True):
                self.assertEqual(main(), 0)
            self.assertFalse(downloaded[0].exists())
