"""GitHub Releases 업데이터 단위 테스트."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestUpdaterVersion(unittest.TestCase):
    def test_parse_and_compare(self) -> None:
        from startofwork.updater import is_newer, parse_version

        self.assertEqual(parse_version("v1.2.0"), (1, 2, 0))
        self.assertEqual(parse_version("1.10.3"), (1, 10, 3))
        self.assertTrue(is_newer("1.2.1", "1.2.0"))
        self.assertFalse(is_newer("1.2.0", "1.2.0"))
        self.assertFalse(is_newer("1.1.9", "1.2.0"))


class TestUpdaterReleaseParsing(unittest.TestCase):
    def test_pick_asset_and_sha(self) -> None:
        from startofwork.updater import (
            parse_setup_sha256,
            pick_setup_asset,
            release_info_from_json,
        )

        assets = [
            {"name": "StartOfWork-1.2.1.exe", "browser_download_url": "https://x/p.exe"},
            {
                "name": "StartOfWorkSetup-1.2.1.exe",
                "browser_download_url": "https://x/setup.exe",
            },
        ]
        picked = pick_setup_asset(assets, "1.2.1")
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["name"], "StartOfWorkSetup-1.2.1.exe")

        body = (
            "## SHA256\n"
            "- `StartOfWorkSetup-1.2.1.exe`: `"
            + ("A" * 64)
            + "`"
        )
        self.assertEqual(
            parse_setup_sha256(body, "StartOfWorkSetup-1.2.1.exe"),
            "A" * 64,
        )

        payload = {
            "tag_name": "v1.2.1",
            "html_url": "https://github.com/x/y/releases/tag/v1.2.1",
            "draft": False,
            "prerelease": False,
            "body": body,
            "assets": assets,
        }
        info = release_info_from_json(payload)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.version, "1.2.1")
        self.assertEqual(info.asset_name, "StartOfWorkSetup-1.2.1.exe")

    def test_prerelease_ignored(self) -> None:
        from startofwork.updater import release_info_from_json

        payload = {
            "tag_name": "v1.3.0-beta",
            "prerelease": True,
            "assets": [{"name": "StartOfWorkSetup-1.3.0.exe", "browser_download_url": "u"}],
        }
        self.assertIsNone(release_info_from_json(payload))


class TestUpdaterNetwork(unittest.TestCase):
    def test_check_for_update_mock(self) -> None:
        from startofwork.updater import ReleaseInfo, check_for_update

        release = ReleaseInfo(
            version="9.9.9",
            tag_name="v9.9.9",
            html_url="https://example.com",
            asset_name="StartOfWorkSetup-9.9.9.exe",
            download_url="https://example.com/setup.exe",
            body="",
        )
        with mock.patch(
            "startofwork.updater.fetch_latest_release", return_value=release
        ):
            found, message = check_for_update(local_version="1.2.0")
        self.assertIsNotNone(found)
        self.assertIn("9.9.9", message)

        with mock.patch(
            "startofwork.updater.fetch_latest_release", return_value=release
        ):
            found, message = check_for_update(local_version="9.9.9")
        self.assertIsNone(found)
        self.assertIn("최신", message)

    def test_download_and_verify(self) -> None:
        import hashlib

        from startofwork.updater import (
            ReleaseInfo,
            download_release_asset,
            verify_download,
        )

        payload = b"setup-binary"
        digest = hashlib.sha256(payload).hexdigest().upper()
        release = ReleaseInfo(
            version="1.2.1",
            tag_name="v1.2.1",
            html_url="https://example.com",
            asset_name="StartOfWorkSetup-1.2.1.exe",
            download_url="https://example.com/setup.exe",
            body="",
            expected_sha256=digest,
        )

        class _Resp:
            headers = {"Content-Length": str(len(payload))}

            def __init__(self) -> None:
                self._data = payload
                self._pos = 0

            def read(self, size: int = -1) -> bytes:
                if self._pos >= len(self._data):
                    return b""
                if size < 0:
                    chunk = self._data[self._pos :]
                    self._pos = len(self._data)
                    return chunk
                chunk = self._data[self._pos : self._pos + size]
                self._pos += len(chunk)
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            seen: list[tuple[int, int]] = []

            def _progress(downloaded: int, total: int) -> None:
                seen.append((downloaded, total))

            with mock.patch("startofwork.updater.urlopen", return_value=_Resp()):
                path = download_release_asset(
                    release, dest_dir=dest, progress_callback=_progress
                )
            self.assertTrue(path.is_file())
            self.assertTrue(seen)
            self.assertEqual(seen[-1][0], len(payload))
            verify_download(path, digest)
            from startofwork.updater import UpdateError

            with self.assertRaises(UpdateError):
                verify_download(path, "0" * 64)


class TestUpdateConfig(unittest.TestCase):
    def test_update_check_enabled_default(self) -> None:
        from startofwork import config

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            with mock.patch.object(config, "CONFIG_FILE", cfg):
                config.clear_config_cache()
                data = config.ensure_app_config()
                self.assertTrue(data.get("update_check_enabled", False))
                self.assertTrue(config.load_update_check_enabled())
                config.save_update_check_enabled(False)
                self.assertFalse(config.load_update_check_enabled())


if __name__ == "__main__":
    unittest.main()
