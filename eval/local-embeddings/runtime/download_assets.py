"""Fetch pinned, free public files. No credentials, inference client or remote code.

Only successful hash-verified downloads are published. Existing differing files
are preserved, and symlinks are rejected. Choose a new output root for recovery.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

MANIFEST = Path(__file__).with_name("assets.json")


def asset_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(part in {"..", "."} for part in path.parts):
        raise ValueError("Asset paths must be contained, relative paths")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("The asset root must not be a symlink")
    current = root
    for part in path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("Asset directories must not be symlinks")
        current.mkdir(exist_ok=True)
    target = root.joinpath(*path.parts)
    if target.is_symlink():
        raise ValueError("Asset files must not be symlinks")
    return target


def verified(path: Path, expected: dict) -> bool:
    if not path.exists():
        return False
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Asset files must be regular files")
    if path.stat().st_size != expected["bytes"]:
        raise ValueError(f"Existing asset has a different size; preserved: {expected['path']}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
        raise ValueError(f"Existing asset has a different hash; preserved: {expected['path']}")
    return True


def download(root: Path, expected: dict) -> None:
    target = asset_path(root, expected["path"])
    if verified(target, expected):
        return
    url = expected["url"]
    if not url.startswith(
        (
            "https://huggingface.co/",
            "https://raw.githubusercontent.com/FlagOpen/FlagEmbedding/",
            "https://www.apache.org/licenses/",
        )
    ):
        raise ValueError("The manifest must use a supported public asset publisher")
    digest = hashlib.sha256()
    temporary = None
    try:
        with (
            urllib.request.urlopen(url, timeout=60) as response,
            tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=".download-", delete=False
            ) as out,
        ):
            temporary = Path(out.name)
            total = 0
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > expected["bytes"]:
                    raise ValueError("Downloaded asset exceeds its pinned size")
                out.write(block)
                digest.update(block)
            out.flush()
            os.fsync(out.fileno())
        if total != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
            raise ValueError("Downloaded asset failed pinned size or SHA256 verification")
        # Exclusive publication: do not overwrite a file created while downloading.
        os.link(temporary, target)
        target.chmod(0o644)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-root", type=Path, required=True)
    parser.add_argument("--candidate", choices=["all", "bge", "minilm"], default="all")
    args = parser.parse_args()
    for candidate in json.loads(MANIFEST.read_text()):
        if args.candidate not in {"all", candidate["key"]}:
            continue
        for expected in candidate["files"]:
            download(args.assets_root.absolute(), expected)
        print(f"Verified pinned public assets: {candidate['key']}")


if __name__ == "__main__":
    main()
