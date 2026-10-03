"""No-network collector admission regressions, run by the desktop package tests."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "notice_collector", Path(__file__).resolve().parents[1] / "scripts/collect-python-notices.py"
)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class CacheAdmission(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.cache = Path(self.folder.name)
        self.data = b"synthetic public source"
        self.source = {
            "url": "https://static.crates.io/crates/synthetic/synthetic-1.crate",
            "archive_sha256": hashlib.sha256(self.data).hexdigest(),
            "archive_bytes": len(self.data),
        }
        self.target = self.cache / self.source["archive_sha256"]
        self.network = patch.object(
            collector.urllib.request, "urlopen", side_effect=AssertionError("Network forbidden")
        )
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_valid_cache_is_offline_and_corrupt_size_is_rejected(self):
        self.target.write_bytes(self.data)
        self.assertEqual(collector.download(self.source, self.cache), self.data)
        self.target.write_bytes(self.data + b"unexpected excess")
        with self.assertRaisesRegex(ValueError, "pinned size"):
            collector.download(self.source, self.cache)
        with self.assertRaisesRegex(ValueError, "bounded source"):
            collector.download({**self.source, "archive_bytes": 100 * 1024 * 1024}, self.cache)
        with self.assertRaisesRegex(ValueError, "SHA256"):
            collector.download({**self.source, "archive_sha256": "../redirect"}, self.cache)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO and symlink admission")
    def test_fifo_and_symlink_cannot_be_read_as_cached_archives(self):
        os.mkfifo(self.target)
        with self.assertRaisesRegex(ValueError, "regular file"):
            collector.download(self.source, self.cache)
        self.target.unlink()
        original = self.cache / "original"
        original.write_bytes(self.data)
        self.target.symlink_to(original)
        with self.assertRaisesRegex(ValueError, "regular files"):
            collector.download(self.source, self.cache)
        self.assertEqual(original.read_bytes(), self.data)


if __name__ == "__main__":
    unittest.main()
