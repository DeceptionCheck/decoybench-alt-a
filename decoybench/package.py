"""Build a reproducible source archive and a provenance manifest.

    python -m decoybench package --out dist [--root .]

The archive holds the files under the project root that are tracked by git (or,
without git, every file except caches and earlier build output), in sorted order,
with a fixed timestamp, owner 0 and normalised permissions, so two builds of the
same tree give identical bytes. The manifest lists every file's SHA-256, the
archive's SHA-256, the git commit when available, and the tool version. Building
never publishes, uploads or pushes anything.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

from . import __version__

FIXED_MTIME = 1793664000  # 2026-11-03T00:00:00Z
EXCLUDE_PARTS = {"__pycache__", ".git", "dist", "build", ".pytest_cache"}


def tracked_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
        names = [n for n in out.stdout.decode("utf-8").split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file()]
    return sorted(n for n in names if not (set(Path(n).parts) & EXCLUDE_PARTS) and not n.endswith(".pyc"))


def commit(root: Path):
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, check=True, text=True).stdout.strip()
        return head, bool(dirty)
    except (OSError, subprocess.CalledProcessError):
        return None, None


def build(root: Path, out: Path) -> dict:
    root, out = root.resolve(), out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    prefix = f"decoybench-{__version__}"
    names = tracked_files(root)
    files = {}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in names:
            data = (root / name).read_bytes()
            files[name] = hashlib.sha256(data).hexdigest()
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size, info.mtime, info.uid, info.gid, info.uname, info.gname = len(data), FIXED_MTIME, 0, 0, "", ""
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    archive = out / f"{prefix}.tar.gz"
    if archive.exists():
        raise FileExistsError(f"{archive} exists; use a new --out folder")
    with open(archive, "wb") as fh, gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    head, dirty = commit(root)
    manifest = {"tool": "decoybench", "version": __version__, "archive": archive.name,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "git_commit": head,
                "working_tree_dirty": dirty, "file_count": len(files), "files": files,
                "reproducible": "sorted entries, fixed mtime, owner 0, mode 0644, gzip mtime 0",
                "published": False}
    (out / f"{prefix}.MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(prog="decoybench package", description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)
    manifest = build(Path(args.root), Path(args.out))
    print(json.dumps({k: manifest[k] for k in ("archive", "archive_sha256", "git_commit", "working_tree_dirty", "file_count")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
