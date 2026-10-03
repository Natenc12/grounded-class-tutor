"""No models or network needed to exercise the asset admission boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import io
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "download_assets", Path(__file__).with_name("download_assets.py")
)
downloader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(downloader)


def asset(data=b"synthetic model"):
    return {
        "path": "model/model.onnx",
        "url": "https://huggingface.co/example/model/resolve/pinned/model.onnx",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def test_complete_verified_asset_is_reused_without_network(tmp_path, monkeypatch):
    expected = asset()
    monkeypatch.setattr(
        downloader.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"synthetic model")
    )
    downloader.download(tmp_path, expected)
    path = tmp_path / expected["path"]
    assert path.read_bytes() == b"synthetic model"
    monkeypatch.setattr(
        downloader.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("Verified assets must not be downloaded again"),
    )
    downloader.download(tmp_path, expected)


@pytest.mark.parametrize(
    "data", [b"truncated", b"wrong same size", b"too much data for this model"]
)
def test_invalid_download_does_not_publish_or_leave_partial_files(tmp_path, monkeypatch, data):
    monkeypatch.setattr(
        downloader.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(data)
    )
    with pytest.raises(ValueError):
        downloader.download(tmp_path, asset())
    assert list((tmp_path / "model").iterdir()) == []


def test_existing_changed_asset_is_preserved(tmp_path, monkeypatch):
    path = downloader.asset_path(tmp_path, "model/model.onnx")
    path.write_bytes(b"user data")
    monkeypatch.setattr(
        downloader.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("No overwrite")
    )
    with pytest.raises(ValueError, match="preserved"):
        downloader.download(tmp_path, asset())
    assert path.read_bytes() == b"user data"


def test_escape_and_symlink_paths_are_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "assets"
    root.mkdir()
    for path in ["../outside/file", "/absolute/file"]:
        with pytest.raises(ValueError):
            downloader.asset_path(root, path)
    (root / "model").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        downloader.download(root, asset())
    assert list(outside.iterdir()) == []
