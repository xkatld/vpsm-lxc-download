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
from build_pipeline import ImageSpec, valid_export_suffixes  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(root: Path, expected: dict) -> int:
    summary = json.loads((root / "build-summary.json").read_text(encoding="utf-8"))
    if summary.get("status") != "success" or summary.get("architecture") != expected["architecture"]:
        raise ValueError("构建未成功或架构不匹配")
    images = summary.get("images", [])
    if len(images) != 1 or any(images[0].get(key) != expected[key] for key in ("distro", "release", "architecture")):
        raise ValueError("摘要与预期镜像不匹配")
    files = summary.get("exported_files", [])
    if not files or len(files) != len(set(files)):
        raise ValueError("导出文件缺失或重复")
    for name in files:
        if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
            raise ValueError("导出文件名不安全")
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"导出文件缺失或为空：{name}")
    spec = ImageSpec(expected["distro"], expected["release"], expected["architecture"], "default", "snapshot")
    prefix = spec.container_name()
    if any(not (name == prefix or name.startswith(prefix + ".")) for name in files):
        raise ValueError("镜像导出文件不符合预期")
    if not valid_export_suffixes({name[len(prefix):] for name in files}):
        raise ValueError("导出文件组合不符合预期")
    checksums = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if not match or match[2] in checksums:
            raise ValueError("校验和条目无效或重复")
        checksums[match[2]] = match[1]
    if set(checksums) != set(files):
        raise ValueError("校验和列表与导出文件不匹配")
    actual = {path.name for path in root.iterdir()}
    if actual != set(files) | {"build-summary.json", "SHA256SUMS"}:
        raise ValueError("产物文件多出或缺失")
    for name, digest in checksums.items():
        if sha256(root / name) != digest:
            raise ValueError(f"校验和不匹配：{name}")
    return len(files)


def release_configured(repo: str | None, tag: str | None, token: str) -> bool:
    return bool(repo and tag and token)


def ensure_release(repo: str, tag: str, token: str) -> None:
    env = dict(os.environ, GH_TOKEN=token)
    view = subprocess.run(["gh", "release", "view", tag, "--repo", repo],
                          capture_output=True, text=True, env=env, timeout=120)
    if view.returncode != 0:
        subprocess.run(["gh", "release", "create", tag, "--repo", repo,
                        "--title", tag, "--notes", ""], check=True, env=env, timeout=120)


def upload_assets(repo: str, tag: str, token: str, files: list[Path]) -> None:
    env = dict(os.environ, GH_TOKEN=token)
    subprocess.run(["gh", "release", "upload", tag, *(str(path) for path in files),
                    "--repo", repo, "--clobber"], check=True, env=env, timeout=1800)


def main() -> int:
    parser = argparse.ArgumentParser(description="逐个校验本次运行的镜像产物，限制磁盘占用。", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    parser.add_argument("--matrix", type=Path, required=True, help="预期构建矩阵文件路径")
    parser.add_argument("--run-id", required=True, help="待校验产物所属的工作流运行编号")
    parser.add_argument("--release-repo", help="发布目标仓库 owner/name")
    parser.add_argument("--release-tag", help="发布目标标签")
    args = parser.parse_args()
    rows = json.loads(args.matrix.read_text(encoding="utf-8"))["include"]
    if not rows:
        raise ValueError("预期构建矩阵为空")
    names = [f"images-{r['distro']}-{r['release']}-{r['architecture']}" for r in rows]
    if len(names) != len(set(names)):
        raise ValueError("预期产物名称重复")
    token = os.environ.get("RELEASE_TOKEN", "")
    publish = release_configured(args.release_repo, args.release_tag, token)
    if publish:
        ensure_release(args.release_repo, args.release_tag, token)
    checksum_lines: list[str] = []
    total = 0
    for row, name in zip(rows, names):
        with tempfile.TemporaryDirectory(prefix="vpsm-verify-") as directory:
            root = Path(directory)
            subprocess.run(
                ["gh", "run", "download", args.run_id, "--name", name, "--dir", directory],
                check=True, timeout=1200,
            )
            count = verify_artifact(root, row)
            total += count
            if publish:
                exported = json.loads((root / "build-summary.json").read_text(encoding="utf-8"))["exported_files"]
                upload_assets(args.release_repo, args.release_tag, token, [root / item for item in exported])
                checksum_lines.extend((root / "SHA256SUMS").read_text(encoding="utf-8").splitlines(keepends=True))
            print(f"已校验 {name}：{count} 个文件")
    if publish:
        with tempfile.TemporaryDirectory(prefix="vpsm-release-") as directory:
            combined = Path(directory) / "SHA256SUMS"
            combined.write_text("".join(checksum_lines), encoding="utf-8")
            upload_assets(args.release_repo, args.release_tag, token, [combined])
    else:
        print("[注意] 未配置发布目标或 RELEASE_TOKEN，跳过发布")
    report = f"已校验 {len(rows)} 个架构与版本构建任务、{len(rows)} 个镜像、{total} 个导出文件。"
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(f"## 镜像构建完成\n\n{report}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
