"""Manage the official DuckDuckGo Chrome extension without changing its source."""

from __future__ import annotations

import argparse
import hashlib
import importlib.resources
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "duckduckgo/duckduckgo-privacy-extension"
WEB_URL = f"https://github.com/{REPOSITORY}"
API_URL = f"https://api.github.com/repos/{REPOSITORY}"
INITIAL_VERSION = "2026.8.24"
MAX_DOWNLOAD = 50 * 1024 * 1024
MAX_UNPACKED = 200 * 1024 * 1024


class ExtensionError(RuntimeError):
    pass


def version_key(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+){0,3}", value):
        raise ExtensionError(f"Invalid stable version: {value!r}")
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * (4 - len(parts))


def browser_version() -> str:
    manifest = importlib.resources.files("patchright") / "driver/package/browsers.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    return next(item["browserVersion"] for item in data["browsers"] if item["name"] == "chromium")


def contained_path(root: Path, relative: str) -> Path:
    # ZIP and manifest paths also need Windows drive, ADS, and device-name checks.
    path = PurePosixPath(relative)
    if not relative or "\\" in relative or ":" in relative or path.is_absolute():
        raise ExtensionError(f"Invalid extension path: {relative!r}")
    for part in path.parts:
        if part in {".", ".."} or part.endswith((".", " ")):
            raise ExtensionError(f"Invalid extension path: {relative!r}")
        if re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part):
            raise ExtensionError(f"Reserved extension path: {relative!r}")
    target = (root / path).resolve()
    if target == root.resolve() or not target.is_relative_to(root.resolve()):
        raise ExtensionError(f"Extension path leaves its directory: {relative!r}")
    return target


def validate_manifest(directory: Path, chromium_version: str | None = None) -> dict:
    try:
        data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExtensionError("Extension manifest is missing or invalid. Run the extension updater first.") from exc
    if not isinstance(data, dict) or data.get("manifest_version") != 3:
        raise ExtensionError("A Manifest V3 extension is required.")
    version_key(data.get("version"))
    minimum = data.get("minimum_chrome_version")
    if minimum and chromium_version and version_key(chromium_version) < version_key(minimum):
        raise ExtensionError(f"Extension requires Chromium {minimum}; installed: {chromium_version}.")
    background = data.get("background")
    worker = background.get("service_worker") if isinstance(background, dict) else None
    if not isinstance(worker, str) or not contained_path(directory, worker).is_file():
        raise ExtensionError("Extension background service worker is missing.")
    return data


class ExtensionLock:
    """One OS-owned lock shared by the launcher and updater; never unlink it."""

    def __init__(self, directory: Path):
        self.path = directory.resolve().parent / f".{directory.name}.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if self.file.seek(0, os.SEEK_END) == 0:
                self.file.write(b"\0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise ExtensionError("Extension is in use. Close its browser or wait for the other updater.") from exc
        return self

    def __exit__(self, *_args):
        if self.file is not None:
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()
                self.file = None


@dataclass(frozen=True)
class Release:
    version: str
    asset_url: str
    sha256: str

    def __post_init__(self):
        version_key(self.version)
        url = urlparse(self.asset_url)
        prefix = f"/{REPOSITORY}/releases/download/{self.version}/"
        filename = url.path.removeprefix(prefix)
        if (url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment
                or not url.path.startswith(prefix)
                or not re.fullmatch(r"chrome-release-[A-Za-z0-9_.-]+\.zip", filename)):
            raise ExtensionError("Expected an official Chrome release asset URL.")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ExtensionError("Release is missing its official SHA-256 digest.")

    def record(self) -> dict:
        return {
            "repository": WEB_URL,
            "version": self.version,
            "release_url": f"{WEB_URL}/releases/tag/{self.version}",
            "asset_url": self.asset_url,
            "sha256": self.sha256,
            "license": "Apache-2.0",
            "license_url": f"https://raw.githubusercontent.com/{REPOSITORY}/{self.version}/LICENSE.md",
        }


class ReleaseHTML(HTMLParser):
    """Associate each asset URL with the digest in the same GitHub asset row."""

    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self.row: dict | None = None
        self.depth = 0
        self.unstable = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "li":
            if self.depth == 0:
                self.row = {"urls": [], "digests": []}
            self.depth += 1
        if self.row is not None:
            if tag == "a":
                self.row["urls"].append(urljoin(WEB_URL, attrs.get("href", "")))
            if tag == "clipboard-copy":
                self.row["digests"].append(attrs.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "li" and self.depth:
            self.depth -= 1
            if self.depth == 0:
                self.rows.append(self.row)
                self.row = None

    def handle_data(self, data):
        if data.strip().lower() in {"pre-release", "draft"}:
            self.unstable = True
        if self.row is not None:
            self.row["digests"].extend(re.findall(r"sha256:[0-9a-f]{64}", data))

    def release(self, version: str) -> Release:
        candidates = []
        for row in self.rows:
            for url in row["urls"]:
                if PurePosixPath(urlparse(url).path).name.startswith("chrome-release-"):
                    digests = {value[7:] for value in row["digests"]
                               if re.fullmatch(r"sha256:[0-9a-f]{64}", value)}
                    if len(digests) != 1:
                        raise ExtensionError("Chrome asset has no unambiguous SHA-256 digest.")
                    candidates.append(Release(version, url, next(iter(digests))))
        if len(candidates) != 1:
            raise ExtensionError("Expected exactly one Chrome release ZIP.")
        return candidates[0]


class ReleaseClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "2608B-iCloud-extension-updater"
        retries = Retry(total=2, backoff_factor=0.5, status_forcelist=(502, 503, 504))
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def close(self):
        self.session.close()

    def get(self, url: str, *, stream: bool = False):
        return self.session.get(url, timeout=(10, 60), stream=stream)

    def release(self, version: str | None = None) -> Release:
        if version:
            version_key(version)
        suffix = f"tags/{version}" if version else "latest"
        data = None
        try:
            with self.get(f"{API_URL}/releases/{suffix}") as response:
                response.raise_for_status()
                data = response.json()
        except (requests.RequestException, ValueError):
            pass
        if isinstance(data, dict):
            if data.get("prerelease") or data.get("draft"):
                raise ExtensionError("Only published stable releases are accepted.")
            try:
                tag = data["tag_name"]
                if version and version != tag:
                    raise ExtensionError("Release tag mismatch.")
                assets = [item for item in data["assets"]
                          if item.get("name", "").startswith("chrome-release-")
                          and item.get("name", "").endswith(".zip")]
                if len(assets) == 1:
                    digest = assets[0].get("digest") or ""
                    return Release(tag, assets[0]["browser_download_url"], digest.removeprefix("sha256:"))
            except (KeyError, TypeError, ExtensionError):
                pass
        page_url = f"{WEB_URL}/releases/tag/{version}" if version else f"{WEB_URL}/releases/latest"
        with self.get(page_url) as response:
            response.raise_for_status()
            parsed = urlparse(response.url)
            prefix = f"/{REPOSITORY}/releases/tag/"
            if parsed.netloc != "github.com" or not parsed.path.startswith(prefix):
                raise ExtensionError("GitHub did not return a stable release page.")
            tag = parsed.path.removeprefix(prefix)
            version_key(tag)
            if version and tag != version:
                raise ExtensionError("Release tag mismatch.")
            page = ReleaseHTML()
            page.feed(response.text)
            if page.unstable:
                raise ExtensionError("Only published stable releases are accepted.")
        with self.get(f"{WEB_URL}/releases/expanded_assets/{tag}") as response:
            response.raise_for_status()
            assets_page = ReleaseHTML()
            assets_page.feed(response.text)
        return assets_page.release(tag)

    def download(self, release: Release, directory: Path, log: Callable[[str], None]) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / PurePosixPath(urlparse(release.asset_url).path).name
        if target.is_file():
            with target.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() == release.sha256:
                    log("Using verified cached release ZIP.")
                    return target
        partial = target.with_suffix(".zip.part")
        try:
            with self.get(release.asset_url, stream=True) as response:
                response.raise_for_status()
                digest = hashlib.sha256()
                total = 0
                reported = 0
                with partial.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD:
                            raise ExtensionError("Release ZIP exceeds the download size limit.")
                        output.write(chunk)
                        digest.update(chunk)
                        if total - reported >= 1024 * 1024:
                            log(f"Downloaded {total // (1024 * 1024)} MiB...")
                            reported = total
                if digest.hexdigest() != release.sha256:
                    raise ExtensionError("Release ZIP SHA-256 mismatch; current extension is unchanged.")
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        return target

    def license_files(self, version: str) -> dict[str, bytes]:
        files = {}
        for name in ("LICENSE.md", "NOTICE", "NOTICE.md"):
            url = f"https://raw.githubusercontent.com/{REPOSITORY}/{version}/{name}"
            with self.get(url) as response:
                if name != "LICENSE.md" and response.status_code == 404:
                    continue
                response.raise_for_status()
                if len(response.content) > 1024 * 1024:
                    raise ExtensionError("Unexpected license file size.")
                if name == "LICENSE.md" and b"Apache License" not in response.content:
                    raise ExtensionError("Expected the official Apache license text.")
                files[name] = response.content
        return files


def extract_release(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > 10000 or sum(item.file_size for item in members) > MAX_UNPACKED:
            raise ExtensionError("Extension archive exceeds extraction limits.")
        seen = set()
        for item in members:
            path = contained_path(destination, item.filename)
            key = str(path).casefold()
            mode = stat.S_IFMT(item.external_attr >> 16)
            if key in seen or mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ExtensionError("Archive contains duplicate paths or special files.")
            seen.add(key)
        for item in members:
            target = contained_path(destination, item.filename)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(data, output, ensure_ascii=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ExtensionManager:
    def __init__(self, root: Path = PROJECT_ROOT, client=None, log: Callable[[str], None] = print):
        self.root = root.resolve()
        self.home = self.root / "browsers/extensions/duckduckgo"
        self.active = self.home / "active"
        self.previous = self.home / "previous"
        self.record_path = self.root / "third_party/duckduckgo-extension.json"
        self.client = client if client is not None else ReleaseClient()
        self.log = log

    def installed_version(self) -> str | None:
        if not self.active.exists():
            return None
        return validate_manifest(self.active)["version"]

    def _remove_tree(self, path: Path) -> None:
        if path.resolve() == self.home.resolve() or not path.resolve().is_relative_to(self.home.resolve()):
            raise ExtensionError("Cleanup path leaves extension storage.")
        if path.exists():
            shutil.rmtree(path)

    def recover(self) -> None:
        # A process exit between the two renames must not strand a usable old version.
        if not self.active.exists() and self.previous.exists():
            validate_manifest(self.previous)
            self.previous.rename(self.active)
            self.log("Restored previous extension after an interrupted update.")
        if self.active.exists():
            local_record = self.active / "installation.json"
            if local_record.is_file():
                atomic_json(self.record_path, json.loads(local_record.read_text(encoding="utf-8")))

    def install(self, release: Release) -> bool:
        with ExtensionLock(self.active):
            self.recover()
            installed = self.installed_version()
            if installed and version_key(installed) >= version_key(release.version):
                validate_manifest(self.active, browser_version())
                self.log(f"Already installed: {installed}. No download needed.")
                return False
            archive = self.client.download(release, self.home / "downloads", self.log)
            with tempfile.TemporaryDirectory(prefix=".install-", dir=self.home) as temporary:
                payload = Path(temporary) / "payload"
                payload.mkdir()
                extract_release(archive, payload)
                manifest = validate_manifest(payload, browser_version())
                if manifest["version"] != release.version:
                    raise ExtensionError("Release tag and extension manifest version differ.")
                for name, content in self.client.license_files(release.version).items():
                    (payload / name).write_bytes(content)
                record = release.record()
                atomic_json(payload / "installation.json", record)
                archive_dir = self.home / "versions" / release.version
                if not archive_dir.exists():
                    shutil.copytree(payload, archive_dir)
                self._activate(payload, record)
            self.log(f"Installed DuckDuckGo {release.version}. Restart its browser to use this version.")
            return True

    def _activate(self, payload: Path, record: dict) -> None:
        self._remove_tree(self.previous)
        moved_old = False
        moved_new = False
        try:
            if self.active.exists():
                self.active.rename(self.previous)
                moved_old = True
            payload.rename(self.active)
            moved_new = True
            atomic_json(self.record_path, record)
        except BaseException:
            if moved_new:
                self._remove_tree(self.active)
            if moved_old:
                self.previous.rename(self.active)
            raise

    def check(self) -> Release:
        installed = self.installed_version()
        self.log(f"Installed version: {installed or 'not installed'}")
        latest = self.client.release()
        self.log(f"Latest stable version: {latest.version}")
        self.log(f"Release: {latest.record()['release_url']}")
        if installed and version_key(installed) >= version_key(latest.version):
            self.log("No update needed.")
        return latest

    def update(self, confirm: Callable[[str], str] = input) -> bool:
        release = self.check()
        installed = self.installed_version()
        if installed and version_key(installed) >= version_key(release.version):
            return False
        try:
            answer = confirm(f"Install DuckDuckGo {release.version}? [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            self.log("Update cancelled; installed files are unchanged.")
            return False
        return self.install(release)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Show installed and latest stable versions")
    commands.add_parser("update", help="Ask before installing the latest stable release")
    install = commands.add_parser("install", help="Install a specific stable version")
    install.add_argument("--version", default=INITIAL_VERSION)
    args = parser.parse_args(argv)
    manager = ExtensionManager(log=lambda message: print(message, flush=True))
    try:
        if args.command == "check":
            manager.check()
        elif args.command == "update":
            manager.update()
        else:
            manager.install(manager.client.release(args.version))
        return 0
    except requests.RequestException as exc:
        print(f"ERROR: Network request failed ({type(exc).__name__}). Check the download proxy/network.", file=sys.stderr)
        return 1
    except (ExtensionError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.")
        return 1
    finally:
        manager.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
