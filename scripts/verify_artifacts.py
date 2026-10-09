#!/usr/bin/env python3
"""Verify this run's image artifacts one at a time to bound disk usage."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_pipeline import ImageSpec  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(root: Path, expected: dict) -> int:
    summary = json.loads((root / "build-summary.json").read_text(encoding="utf-8"))
    if summary.get("status") != "success" or summary.get("architecture") != expected["architecture"]:
        raise ValueError("unsuccessful build or wrong architecture")
    images = summary.get("images", [])
    if len(images) != 1 or any(images[0].get(key) != expected[key] for key in ("distro", "release", "architecture")):
        raise ValueError("summary does not match expected image")
    files = summary.get("exported_files", [])
    if not files or len(files) != len(set(files)):
        raise ValueError("missing or duplicate exported files")
    for name in files:
        if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
            raise ValueError("unsafe export filename")
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"missing or empty export: {name}")
    spec = ImageSpec(expected["distro"], expected["release"], expected["architecture"], "default", "snapshot")
    prefixes = [spec.container_name(flavor) for flavor in ("all", "lite")]

    def belongs(name, prefix):
        return name == prefix or name.startswith(prefix + ".")

    if not all(any(belongs(name, prefix) for name in files) for prefix in prefixes):
        raise ValueError("both all and lite exports are required")
    if any(not any(belongs(name, prefix) for prefix in prefixes) for name in files):
        raise ValueError("unexpected image export")
    checksums = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if not match or match[2] in checksums:
            raise ValueError("invalid or duplicate checksum entry")
        checksums[match[2]] = match[1]
    if set(checksums) != set(files):
        raise ValueError("checksum list does not match exported files")
    actual = {path.name for path in root.iterdir()}
    if actual != set(files) | {"build-summary.json", "SHA256SUMS"}:
        raise ValueError("unexpected or missing artifact files")
    for name, digest in checksums.items():
        if sha256(root / name) != digest:
            raise ValueError(f"checksum mismatch: {name}")
    return len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    rows = json.loads(args.matrix.read_text(encoding="utf-8"))["include"]
    if not rows:
        raise ValueError("empty expected matrix")
    names = [f"images-{r['distro']}-{r['release']}-{r['architecture']}" for r in rows]
    if len(names) != len(set(names)):
        raise ValueError("duplicate expected artifacts")
    total = 0
    for row, name in zip(rows, names):
        with tempfile.TemporaryDirectory(prefix="vpsm-verify-") as directory:
            subprocess.run(
                ["gh", "run", "download", args.run_id, "--name", name, "--dir", directory],
                check=True, timeout=1200,
            )
            count = verify_artifact(Path(directory), row)
            total += count
            print(f"Verified {name}: {count} files")
    report = f"Verified {len(rows)} architecture/version jobs, {len(rows) * 2} images, {total} export files."
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(f"## Complete image build\n\n{report}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
