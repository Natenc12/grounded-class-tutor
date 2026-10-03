"""Reproduce supplemental wheel notices from pinned, free public source archives.

No source is executed or extracted onto the filesystem. The checked-in manifest
selects exact license files and leading copyright comments by byte range/hash.
This collector is a maintenance tool; normal packaging verifies committed text.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

MANIFEST = Path(__file__).resolve().parents[1] / "licenses/python-wheels/manifest.json"


def verified(content: bytes, expected: dict, size_key="bytes", hash_key="sha256") -> bytes:
    if (
        len(content) != expected[size_key]
        or hashlib.sha256(content).hexdigest() != expected[hash_key]
    ):
        raise ValueError("Pinned notice source failed size or SHA256 verification")
    return content


def download(source: dict, cache: Path) -> bytes:
    url = source["url"]
    if not url.startswith(
        (
            "https://static.crates.io/crates/",
            "https://codeload.github.com/huggingface/tokenizers/tar.gz/",
            "https://codeload.github.com/google/flatbuffers/tar.gz/",
            "https://raw.githubusercontent.com/knurling-rs/defmt/",
        )
    ):
        raise ValueError("Unsupported public notice source")
    size = source["archive_bytes"]
    if (
        type(size) is not int
        or not 0 < size <= 40 * 1024 * 1024
        or not isinstance(source["archive_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", source["archive_sha256"]) is None
    ):
        raise ValueError("Invalid bounded source size or SHA256")
    cache.mkdir(parents=True, exist_ok=True)
    if cache.is_symlink():
        raise ValueError("Notice cache must be a real directory")
    target = cache / source["archive_sha256"]
    if target.is_symlink():
        raise ValueError("Notice cache entries must be regular files")
    if target.exists():
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(target, flags), "rb") as cached:
            info = os.fstat(cached.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise ValueError("Notice cache must contain a regular file of the pinned size")
            content = cached.read(size + 1)
        return verified(content, source, "archive_bytes", "archive_sha256")
    request = urllib.request.Request(url, headers={"User-Agent": "GCT-public-license-audit"})
    with urllib.request.urlopen(request, timeout=60) as response:
        content = verified(response.read(size + 1), source, "archive_bytes", "archive_sha256")
    with target.open("xb") as output:
        output.write(content)
    return content


def extract_notice(archive: bytes, source: dict, notice: dict) -> bytes:
    if source["format"] == "text":
        return verified(archive, notice)
    path = PurePosixPath(notice["path"])
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Notice path must remain inside its source archive")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        matches = [
            member
            for member in package.getmembers()
            if "/".join(member.name.split("/")[1:]) == str(path)
        ]
        if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 2 * 1024 * 1024:
            raise ValueError("Notice must identify one bounded regular archive member")
        content = package.extractfile(matches[0]).read()
    if "start_byte" in notice:
        start = notice["start_byte"]
        content = content[start : start + notice["bytes"]]
    return verified(content, notice)


def render(manifest: dict, fetch) -> bytes:
    output = [
        b"GCT supplemental Python-wheel and compiled dependency notices\n\n",
        manifest["description"].encode() + b"\n\n",
        b"Original texts below are reproduced without modification. Section headings are GCT's.\n",
        b"The accompanying manifest records source URLs, versions, hashes and byte ranges.\n",
    ]
    for source in manifest["sources"]:
        title = f"\n{'=' * 72}\n{source['name']} {source['version']}\n"
        output.append(title.encode())
        output.append(f"Declared license: {source['license']}\nSource: {source['url']}\n".encode())
        if not source["notices"]:
            output.append(("Notices: " + source["notice_reference"] + "\n").encode())
            continue
        archive = fetch(source)
        for notice in source["notices"]:
            kind = (
                "original leading copyright/license comment"
                if "start_byte" in notice
                else "original file"
            )
            output.append(f"\n--- {notice['path']} ({kind}) ---\n".encode())
            output.append(extract_notice(archive, source, notice))
            output.append(b"\n")
    return b"".join(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    manifest = json.loads(options.manifest.read_text())
    content = render(manifest, lambda source: download(source, options.cache))
    verified(content, manifest["notice_file"])
    options.output.write_bytes(content)
    print(f"Verified original notices for {len(manifest['sources'])} pinned source archives/files.")


if __name__ == "__main__":
    main()
