from __future__ import annotations

import hashlib
import io
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests

from tools import duckduckgo_extension as ddg


def manifest(version="2026.8.24", minimum="128.0"):
    return {
        "manifest_version": 3,
        "name": "Fixture extension",
        "version": version,
        "minimum_chrome_version": minimum,
        "background": {"service_worker": "background.js"},
    }


def make_release(root: Path, version="2026.8.24", minimum="128.0"):
    archive = root / f"{version}.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest(version, minimum)))
        bundle.writestr("background.js", "self.fixture = true;")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    release = ddg.Release(version, f"{ddg.WEB_URL}/releases/download/{version}/chrome-release-fixture.zip", digest)
    return release, archive


def response(*, data=None, url="", text="", content=b"", status=200):
    result = MagicMock()
    result.__enter__.return_value = result
    result.url = url
    result.status_code = status
    result.text = text
    result.content = content
    result.json.return_value = data
    result.iter_content.return_value = [content]
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(str(status))
    return result


class ReleaseTests(unittest.TestCase):
    def test_numeric_version_comparison_and_invalid_versions(self):
        self.assertGreater(ddg.version_key("2026.10.1"), ddg.version_key("2026.9.30"))
        self.assertEqual(ddg.version_key("128"), ddg.version_key("128.0"))
        for value in ("../2026", "2026.8.24-beta", "", None, "1.2.3.4.5"):
            with self.subTest(value=value), self.assertRaises(ddg.ExtensionError):
                ddg.version_key(value)

    def test_official_asset_url_and_digest_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            release, _ = make_release(Path(temporary))
        for url in (
            release.asset_url.replace("github.com", "example.com"),
            release.asset_url.replace("chrome-release-", "firefox-release-"),
            release.asset_url.replace("2026.8.24/", "2026.8.25/"),
            release.asset_url + "?other=1",
        ):
            with self.subTest(url=url), self.assertRaises(ddg.ExtensionError):
                ddg.Release(release.version, url, release.sha256)
        with self.assertRaises(ddg.ExtensionError):
            ddg.Release(release.version, release.asset_url, "")

    def test_release_html_associates_digest_with_correct_asset(self):
        parser = ddg.ReleaseHTML()
        parser.feed(f"""<ul>
          <li><a href="/{ddg.REPOSITORY}/releases/download/2026.8.24/firefox.zip">Firefox</a>
          <span>sha256:{'f' * 64}</span></li>
          <li><a href="/{ddg.REPOSITORY}/releases/download/2026.8.24/chrome-release-fixture.zip">Chrome</a>
          <clipboard-copy value="sha256:{'a' * 64}"></clipboard-copy></li>
        </ul>""")
        self.assertEqual(parser.release("2026.8.24").sha256, "a" * 64)

    def test_release_html_rejects_missing_or_ambiguous_digest(self):
        for extra in ("", f"<span>sha256:{'a' * 64} sha256:{'b' * 64}</span>"):
            parser = ddg.ReleaseHTML()
            parser.feed(f'<li><a href="{ddg.WEB_URL}/releases/download/2026.8.24/chrome-release-fixture.zip">Chrome</a>{extra}</li>')
            with self.assertRaises(ddg.ExtensionError):
                parser.release("2026.8.24")

    def test_api_selects_chrome_and_rejects_prerelease(self):
        client = ddg.ReleaseClient()
        self.addCleanup(client.close)
        data = {
            "tag_name": "2026.8.24", "prerelease": False, "draft": False,
            "assets": [
                {"name": "firefox.zip"},
                {"name": "chrome-release-fixture.zip", "digest": "sha256:" + "a" * 64,
                 "browser_download_url": f"{ddg.WEB_URL}/releases/download/2026.8.24/chrome-release-fixture.zip"},
            ],
        }
        with patch.object(client, "get", return_value=response(data=data)):
            self.assertEqual(client.release().version, "2026.8.24")
        data["prerelease"] = True
        with patch.object(client, "get", return_value=response(data=data)) as get:
            with self.assertRaises(ddg.ExtensionError):
                client.release()
            self.assertEqual(get.call_count, 1)

    def test_api_limit_uses_latest_page_and_expanded_assets(self):
        client = ddg.ReleaseClient()
        self.addCleanup(client.close)
        html = f'<li><a href="{ddg.WEB_URL}/releases/download/2026.8.24/chrome-release-fixture.zip">Chrome</a><span>sha256:{"a" * 64}</span></li>'
        with patch.object(client, "get", side_effect=[
            response(status=403),
            response(url=f"{ddg.WEB_URL}/releases/tag/2026.8.24"),
            response(text=html),
        ]) as get:
            self.assertEqual(client.release().sha256, "a" * 64)
            self.assertTrue(get.call_args_list[1].args[0].endswith("/releases/latest"))

    def test_fallback_rejects_prerelease_page(self):
        client = ddg.ReleaseClient()
        self.addCleanup(client.close)
        with patch.object(client, "get", side_effect=[
            response(status=403),
            response(url=f"{ddg.WEB_URL}/releases/tag/2026.8.24", text="<span>Pre-release</span>"),
        ]):
            with self.assertRaises(ddg.ExtensionError):
                client.release("2026.8.24")

    def test_download_verifies_hash_and_reuses_valid_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release, archive = make_release(root)
            client = ddg.ReleaseClient()
            self.addCleanup(client.close)
            with patch.object(client, "get", return_value=response(content=archive.read_bytes())) as get:
                target = client.download(release, root / "downloads", Mock())
                self.assertEqual(target.read_bytes(), archive.read_bytes())
                client.download(release, root / "downloads", Mock())
                self.assertEqual(get.call_count, 1)

    def test_bad_hash_or_network_failure_never_publishes_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release, _ = make_release(root)
            client = ddg.ReleaseClient()
            self.addCleanup(client.close)
            for result in (response(content=b"wrong bytes"), requests.Timeout("fixture")):
                with self.subTest(result=type(result).__name__):
                    with patch.object(client, "get", side_effect=[result]):
                        with self.assertRaises((ddg.ExtensionError, requests.Timeout)):
                            client.download(release, root / "downloads", Mock())
                    self.assertEqual(list((root / "downloads").iterdir()), [])

    def test_license_and_optional_notices(self):
        client = ddg.ReleaseClient()
        self.addCleanup(client.close)
        with patch.object(client, "get", side_effect=[
            response(content=b"Apache License\nfixture"),
            response(status=404),
            response(content=b"Copyright fixture"),
        ]):
            self.assertEqual(set(client.license_files("2026.8.24")), {"LICENSE.md", "NOTICE.md"})


class ArchiveTests(unittest.TestCase):
    def test_valid_bundle_and_minimum_browser_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, archive = make_release(root)
            ddg.extract_release(archive, root / "out")
            self.assertEqual(ddg.validate_manifest(root / "out", "151.0")["version"], "2026.8.24")
            with self.assertRaises(ddg.ExtensionError):
                ddg.validate_manifest(root / "out", "127.0")

    def test_rejects_unsafe_paths_before_extracting_any_file(self):
        for name in ("../outside", "/outside", "C:/outside", "file:stream", "NUL", "dir/file. "):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with zipfile.ZipFile(root / "bad.zip", "w") as bundle:
                    bundle.writestr("good.txt", "fixture")
                    bundle.writestr(name, "fixture")
                with self.assertRaises(ddg.ExtensionError):
                    ddg.extract_release(root / "bad.zip", root / "out")
                self.assertFalse((root / "out/good.txt").exists())

    def test_rejects_symlinks_and_case_insensitive_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            info = zipfile.ZipInfo("link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(root / "link.zip", "w") as bundle:
                bundle.writestr(info, "../outside")
            with self.assertRaises(ddg.ExtensionError):
                ddg.extract_release(root / "link.zip", root / "out")
            with zipfile.ZipFile(root / "duplicate.zip", "w") as bundle:
                bundle.writestr("file.js", "fixture")
                bundle.writestr("FILE.js", "fixture")
            with self.assertRaises(ddg.ExtensionError):
                ddg.extract_release(root / "duplicate.zip", root / "out")

    def test_missing_worker_and_invalid_manifest_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ddg.ExtensionError):
                ddg.validate_manifest(root)
            (root / "manifest.json").write_text(json.dumps(manifest()), encoding="utf-8")
            with self.assertRaises(ddg.ExtensionError):
                ddg.validate_manifest(root)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = Mock()
        self.client.license_files.return_value = {"LICENSE.md": b"Apache License", "NOTICE": b"Fixture notice"}
        self.log = Mock()
        self.manager = ddg.ExtensionManager(self.root, client=self.client, log=self.log)
        patcher = patch.object(ddg, "browser_version", return_value="151.0.7922.34")
        patcher.start()
        self.addCleanup(patcher.stop)

    def install_fixture(self, version="2026.8.24", minimum="128.0"):
        release, archive = make_release(self.root, version, minimum)
        self.client.download.return_value = archive
        self.manager.install(release)
        return release

    def test_initial_install_and_same_version_no_download(self):
        release = self.install_fixture()
        self.assertEqual(self.manager.installed_version(), release.version)
        self.assertEqual(json.loads(self.manager.record_path.read_text()), release.record())
        self.assertTrue((self.manager.active / "LICENSE.md").is_file())
        self.assertTrue((self.manager.active / "NOTICE").is_file())
        self.assertTrue((self.manager.home / "versions" / release.version / "manifest.json").is_file())
        self.client.download.reset_mock()
        self.assertFalse(self.manager.install(release))
        self.client.download.assert_not_called()

    def test_update_retains_previous_at_fixed_active_path(self):
        self.install_fixture()
        active = self.manager.active
        self.install_fixture("2026.9.1")
        self.assertEqual(self.manager.active, active)
        self.assertEqual(self.manager.installed_version(), "2026.9.1")
        self.assertEqual(ddg.validate_manifest(self.manager.previous)["version"], "2026.8.24")

    def test_update_requires_confirmation_and_defaults_to_cancel(self):
        self.install_fixture()
        release, archive = make_release(self.root, "2026.9.1")
        self.client.release.return_value = release
        self.client.download.return_value = archive
        self.client.download.reset_mock()
        for answer in ("", "n", "no"):
            self.assertFalse(self.manager.update(confirm=Mock(return_value=answer)))
        self.assertFalse(self.manager.update(confirm=Mock(side_effect=EOFError)))
        self.client.download.assert_not_called()
        self.assertTrue(self.manager.update(confirm=Mock(return_value="y")))

    def test_latest_version_check_does_not_prompt_or_download(self):
        release = self.install_fixture()
        self.client.release.return_value = release
        self.client.download.reset_mock()
        confirm = Mock()
        self.assertFalse(self.manager.update(confirm=confirm))
        self.client.download.assert_not_called()
        confirm.assert_not_called()

    def test_busy_browser_blocks_update_and_releases_lock(self):
        release, archive = make_release(self.root)
        self.client.download.return_value = archive
        with ddg.ExtensionLock(self.manager.active):
            with self.assertRaises(ddg.ExtensionError):
                self.manager.install(release)
        self.client.download.assert_not_called()
        self.assertTrue(self.manager.install(release))

    def test_network_or_incompatible_release_preserves_current_version(self):
        self.install_fixture()
        record = self.manager.record_path.read_bytes()
        newer, archive = make_release(self.root, "2026.9.1", minimum="999.0")
        self.client.download.side_effect = requests.Timeout("fixture")
        with self.assertRaises(requests.Timeout):
            self.manager.install(newer)
        self.client.download.side_effect = None
        self.client.download.return_value = archive
        with self.assertRaises(ddg.ExtensionError):
            self.manager.install(newer)
        self.assertEqual(self.manager.installed_version(), "2026.8.24")
        self.assertEqual(self.manager.record_path.read_bytes(), record)

    def test_activation_failure_restores_active_and_metadata(self):
        self.install_fixture()
        record = self.manager.record_path.read_bytes()
        newer, archive = make_release(self.root, "2026.9.1")
        payload = self.manager.home / "fixture-staging"
        ddg.extract_release(archive, payload)
        with patch.object(ddg, "atomic_json", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                self.manager._activate(payload, newer.record())
        self.assertEqual(self.manager.installed_version(), "2026.8.24")
        self.assertEqual(self.manager.record_path.read_bytes(), record)

    def test_interrupted_rename_is_recovered(self):
        self.install_fixture()
        self.manager.active.rename(self.manager.previous)
        with ddg.ExtensionLock(self.manager.active):
            self.manager.recover()
        self.assertEqual(self.manager.installed_version(), "2026.8.24")

    def test_cleanup_rejects_paths_outside_owned_storage(self):
        with self.assertRaises(ddg.ExtensionError):
            self.manager._remove_tree(self.root)

    def test_cli_reports_failure_and_closes_network_session(self):
        self.client.release.side_effect = requests.Timeout("fixture")
        with patch.object(ddg, "ExtensionManager", return_value=self.manager):
            with patch("sys.stderr", new_callable=io.StringIO) as errors:
                self.assertEqual(ddg.main(["install"]), 1)
        self.assertIn("Network request failed", errors.getvalue())
        self.client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
