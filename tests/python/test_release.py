"""Real synthetic release archives; no private source or recursive cleanup."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("wechat_release", ROOT / "tools/release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def fixture(self):
        root = Path(tempfile.mkdtemp(prefix="dsh-release-synthetic-"))
        (root / "package.json").write_text(json.dumps({"version": "1.2.3", "files": ["README.md"]}), encoding="utf-8")
        (root / "README.md").write_text("Synthetic release text.\n", encoding="utf-8")
        return root

    def test_reproducible_bytes_and_exact_manifest(self):
        root = self.fixture()
        first = release.build(root)
        data = (root / "dist" / first["archive"]).read_bytes()
        os.utime(root / "README.md", (1500000000, 1500000000))
        self.assertEqual(release.build(root), first)
        self.assertEqual((root / "dist" / first["archive"]).read_bytes(), data)
        with zipfile.ZipFile(root / "dist" / first["archive"]) as archive:
            self.assertEqual(set(archive.namelist()), {"dsh-better-wechat/README.md", "dsh-better-wechat/package.json"})
            self.assertTrue(all(row.date_time == (2020, 1, 1, 0, 0, 0) for row in archive.infolist()))
        self.assertIn(first["sha256"], (root / "dist/SHA256SUMS").read_text())

    def test_unlisted_private_data_is_not_in_archive(self):
        root = self.fixture()
        (root / "private-not-for-release.txt").write_text("wxid_" + "unmarked_private_canary")
        report = release.build(root)
        self.assertEqual(report["file_count"], 2)

    def test_privacy_failure_does_not_publish_or_replace_success(self):
        root = self.fixture()
        first = release.build(root)
        archive = root / "dist" / first["archive"]
        previous = archive.read_bytes()
        (root / "README.md").write_text("wxid_" + "unmarked_private_canary")
        with self.assertRaises(release.ReleaseError):
            release.build(root)
        self.assertEqual(archive.read_bytes(), previous)
        self.assertEqual(list((root / "dist").glob(".release-*.tmp")), [])

    def test_missing_duplicate_and_traversing_manifest_fail_before_zip(self):
        for files in [["missing.txt"], ["README.md", "README.md"], ["../outside"], ["a/./b"], ["README.md", "readme.md"], ["*"]]:
            root = self.fixture()
            (root / "package.json").write_text(json.dumps({"version": "1.2.3", "files": files}))
            with self.assertRaises(release.ReleaseError):
                release.build(root)
            self.assertFalse((root / "dist").exists())

    def test_archive_scanner_rejects_missing_declared_files(self):
        root = self.fixture()
        archive = root / "incomplete.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.write(root / "package.json", "package.json")
        result = release.scanner()(archive)
        self.assertFalse(result["ok"])
        self.assertIn("DECLARED_FILE_MISSING", [f["rule"] for f in result["findings"]])

    def test_symlinked_dist_and_source_are_rejected(self):
        root = self.fixture()
        other = Path(tempfile.mkdtemp(prefix="dsh-release-target-synthetic-"))
        try:
            (root / "dist").symlink_to(other, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symbolic links is not available")
        with self.assertRaises(release.ReleaseError):
            release.build(root)
        self.assertEqual(list(other.iterdir()), [])

    def test_version_cannot_change_output_directory(self):
        root = self.fixture()
        (root / "package.json").write_text(json.dumps({"version": "../synthetic", "files": ["README.md"]}))
        with self.assertRaises(release.ReleaseError):
            release.build(root)


if __name__ == "__main__":
    unittest.main()
