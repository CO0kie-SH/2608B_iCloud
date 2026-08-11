from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from .config import Settings

log = logging.getLogger("2608b.camoufox")

DEFAULT_CAMOUFOX_REL = "browsers/camoufox"
DEFAULT_PROXY = "http://127.0.0.1:7897"
# 大文件 zip 放这里；探测/launch 绝不调用会 cleanup 的 pkgman.install
_DL_DIRNAME = "_dl"


class CamoufoxRuntimeError(RuntimeError):
    pass


def resolve_camoufox_dir(settings: Settings) -> Path:
    raw = (settings.camoufox_dir or DEFAULT_CAMOUFOX_REL).strip()
    p = Path(raw)
    if not p.is_absolute():
        p = settings.base_dir / p
    return p.resolve()


def resolve_proxy(settings: Settings | None = None) -> str | None:
    for key in (
        "CAMOUFOX_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
    ):
        val = (os.environ.get(key) or "").strip()
        if val:
            if val.lower() in {"none", "off", "0", "false", "disable", "disabled"}:
                return None
            return val
    if settings is not None:
        val = (getattr(settings, "camoufox_proxy", None) or "").strip()
        if val:
            if val.lower() in {"none", "off", "0", "false", "disable", "disabled"}:
                return None
            return val
    return DEFAULT_PROXY


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


def apply_proxy_env(proxy: str | None) -> dict[str, str] | None:
    """仅用于 fetch 下载；会改进程环境变量。"""
    if not proxy:
        log.info("stage=proxy disabled")
        return None
    os.environ["HTTP_PROXY"] = proxy
    os.environ["HTTPS_PROXY"] = proxy
    os.environ["ALL_PROXY"] = proxy
    os.environ["http_proxy"] = proxy
    os.environ["https_proxy"] = proxy
    log.info("stage=proxy set %s", proxy)
    return {"http": proxy, "https": proxy}


def clear_proxy_env() -> None:
    """
    采集/启动浏览器前清除代理环境变量。
    下载用的 127.0.0.1:7897 不能带进 Firefox，否则 icloud.com.cn 易 Connection Error。
    """
    removed: list[str] = []
    for key in _PROXY_ENV_KEYS:
        if key in os.environ:
            os.environ.pop(key, None)
            removed.append(key)
    # 业务配置名也别留给子进程误读
    if "CAMOUFOX_PROXY" in os.environ:
        # 不删 CAMOUFOX_PROXY 本身（resolve 还要用）；只清通用代理键
        pass
    if removed:
        log.info("stage=proxy_cleared keys=%s", ",".join(sorted(set(removed))))
    else:
        log.debug("stage=proxy_cleared none")


def browser_launch_env() -> dict[str, str]:
    """给 Camoufox/Firefox 的 env：去掉代理相关键，避免继承下载代理。"""
    env = {k: v for k, v in os.environ.items() if k not in _PROXY_ENV_KEYS}
    # 明确关掉
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def _patch_requests_proxies(proxies: dict[str, str] | None) -> None:
    if not proxies:
        return
    import requests

    if getattr(requests.get, "_2608b_proxied", False):
        return

    _orig = requests.get

    def _get(url, params=None, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("proxies", proxies)
        kwargs.setdefault("timeout", kwargs.get("timeout") or 60)
        return _orig(url, params=params, **kwargs)

    _get._2608b_proxied = True  # type: ignore[attr-defined]
    requests.get = _get  # type: ignore[assignment]


def configure_install_dir(target: Path) -> Path:
    """
    仅设置 pkgman.INSTALL_DIR / multiversion.BROWSERS_DIR，**绝不**触发 install/cleanup。
    """
    try:
        import camoufox.pkgman as pkgman
    except ImportError as e:
        raise CamoufoxRuntimeError(
            "未安装 camoufox，请先: pip install -U \"camoufox[geoip]\""
        ) from e

    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    pkgman.INSTALL_DIR = target

    try:
        import camoufox.multiversion as multiversion  # type: ignore

        multiversion.BROWSERS_DIR = target / "browsers"
        (target / "browsers").mkdir(parents=True, exist_ok=True)
    except ImportError:
        pass

    log.info("stage=configure_install_dir path=%s", target)
    return target


def _assert_under_project(path: Path, root: Path, what: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as e:
        raise CamoufoxRuntimeError(
            f"{what} 不在项目目录内: {path} (期望位于 {root})"
        ) from e


def _find_exe_under(root: Path) -> Path | None:
    """只扫磁盘，不调用 pkgman（避免 unsupported version → cleanup 删目录）。"""
    names = ("camoufox.exe", "camoufox-bin")
    root = Path(root)
    if not root.exists():
        return None
    for n in names:
        p = root / n
        if p.is_file() and p.stat().st_size > 0:
            return p
    hits: list[Path] = []
    for n in names:
        hits.extend(h for h in root.rglob(n) if h.is_file() and h.stat().st_size > 0)
    if not hits:
        return None
    hits.sort(key=lambda x: len(str(x)))
    return hits[0]


def is_browser_installed(install_dir: Path) -> bool:
    exe = _find_exe_under(Path(install_dir))
    return exe is not None


def _launch_path_strict(install_dir: Path) -> str:
    """
    返回可执行文件路径。
    **禁止**调用 pkgman.launch_path()/camoufox_path()：
    版本不在库约束内时会触发 CamoufoxFetcher.install → cleanup 删光目录。
    """
    install_dir = Path(install_dir).resolve()
    found = _find_exe_under(install_dir)
    if not found:
        raise CamoufoxRuntimeError(
            f"项目内未安装 Camoufox: {install_dir}\n"
            f"请先运行: python main.py camoufox-fetch"
        )
    _assert_under_project(found, install_dir, "camoufox executable")
    return str(found.resolve())


def ensure_browser(settings: Settings, *, fetch_if_missing: bool = False) -> str:
    install_dir = resolve_camoufox_dir(settings)
    # launch 前仍 patch INSTALL_DIR，供 Camoufox 读资源路径；但不调用 launch_path
    try:
        configure_install_dir(install_dir)
    except CamoufoxRuntimeError:
        if not fetch_if_missing:
            raise
    try:
        exe = _launch_path_strict(install_dir)
        log.info("stage=ensure_browser ok exe=%s", exe)
        return exe
    except CamoufoxRuntimeError:
        if not fetch_if_missing:
            raise
        log.info("stage=ensure_browser missing -> fetch")
        fetch_browser(settings)
        return _launch_path_strict(install_dir)


def browser_status(settings: Settings) -> dict[str, object]:
    install_dir = resolve_camoufox_dir(settings)
    # 状态查询：纯文件系统，绝不 import pkgman.launch_path
    installed = is_browser_installed(install_dir)
    exe = ""
    if installed:
        try:
            exe = _launch_path_strict(install_dir)
        except Exception:
            installed = False
            exe = ""
    return {
        "install_dir": str(install_dir),
        "installed": installed,
        "executable": exe,
        "under_project": str(install_dir).startswith(str(settings.base_dir.resolve())),
        "proxy": resolve_proxy(settings) or "",
    }


def _http_get(url: str, proxies: dict[str, str] | None, **kwargs: Any) -> Any:
    import requests

    kw = dict(kwargs)
    if proxies:
        kw.setdefault("proxies", proxies)
    kw.setdefault("timeout", 60)
    headers = dict(kw.pop("headers", None) or {})
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token and "api.github.com" in url and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {token}"
    if headers:
        kw["headers"] = headers
    return requests.get(url, **kw)


def _pick_win_asset_from_html(proxies: dict[str, str] | None) -> tuple[str, str]:
    latest = "https://github.com/daijro/camoufox/releases/latest"
    resp = _http_get(latest, proxies, timeout=45, allow_redirects=True)
    resp.raise_for_status()
    final = str(resp.url)
    m = re.search(r"/releases/tag/([^/?#]+)", final)
    if not m:
        m = re.search(r"/daijro/camoufox/releases/tag/([^\"'?#]+)", resp.text)
    if not m:
        raise CamoufoxRuntimeError(f"无法解析 latest release tag: {final}")
    tag = m.group(1)
    assets_url = f"https://github.com/daijro/camoufox/releases/expanded_assets/{tag}"
    page = _http_get(assets_url, proxies, timeout=45)
    if page.status_code != 200:
        page = _http_get(
            f"https://github.com/daijro/camoufox/releases/tag/{tag}",
            proxies,
            timeout=45,
        )
    page.raise_for_status()
    hrefs = re.findall(r'href="([^"]+)"', page.text)
    candidates: list[str] = []
    for h in hrefs:
        hl = h.lower()
        if not hl.endswith(".zip"):
            continue
        if "win" not in hl:
            continue
        if "mac" in hl or "linux" in hl or "lin." in hl:
            continue
        candidates.append(h)
    if not candidates:
        raise CamoufoxRuntimeError(f"tag {tag} 页面未找到 win zip")
    candidates.sort(
        key=lambda x: (0 if ("x86_64" in x.lower() or "amd64" in x.lower()) else 1, len(x))
    )
    rel = candidates[0]
    zip_url = rel if rel.startswith("http") else urljoin("https://github.com", rel)
    return tag, zip_url


def _pick_win_asset_from_api(proxies: dict[str, str] | None) -> tuple[str, str]:
    repos = ["daijro/camoufox", "camoufox/camoufox"]
    last_err: Exception | None = None
    for repo in repos:
        url = f"https://api.github.com/repos/{repo}/releases"
        try:
            resp = _http_get(url, proxies, timeout=30)
            if resp.status_code == 403:
                last_err = RuntimeError(f"GitHub API 403 rate limit: {repo}")
                continue
            resp.raise_for_status()
            releases = resp.json()
        except Exception as e:
            last_err = e
            continue
        for rel in releases:
            tag = rel.get("tag_name") or ""
            for asset in rel.get("assets") or []:
                name = (asset.get("name") or "").lower()
                if not name.endswith(".zip") or "win" not in name:
                    continue
                if "mac" in name or "lin" in name:
                    continue
                dl = asset.get("browser_download_url") or ""
                if dl:
                    return tag, dl
    if last_err:
        raise last_err
    raise CamoufoxRuntimeError("未在 GitHub releases 找到 Windows camoufox zip")


def _curl_download(url: str, dest: Path, proxy: str | None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 写到临时文件再 rename，避免半截 zip
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    cmd = [
        "curl.exe",
        "-L",
        "--retry",
        "5",
        "--retry-all-errors",
        "--connect-timeout",
        "30",
        "-o",
        str(tmp),
        url,
    ]
    if proxy:
        cmd[1:1] = ["--proxy", proxy]
    log.info("stage=curl_download url=%s out=%s", url.split("?")[0], dest.name)
    # 不捕获 stdout，让进度条打到终端
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0 or not tmp.exists():
        raise CamoufoxRuntimeError(f"curl 下载失败 code={proc.returncode}")
    size = tmp.stat().st_size
    if size < 1_000_000:
        tmp.unlink(missing_ok=True)
        raise CamoufoxRuntimeError(f"下载文件过小 ({size} bytes)，可能不是 zip")
    if dest.exists():
        dest.unlink()
    tmp.replace(dest)
    log.info("stage=curl_done size_mb=%.1f", size / (1024 * 1024))


def _parse_tag_version(tag: str) -> tuple[str, str]:
    """
    v152.0.4-beta.28 → version=152.0.4, build=beta.28
    解析失败则 version=tag, build=manual
    """
    t = (tag or "").strip().lstrip("vV")
    if "-" in t:
        ver, build = t.split("-", 1)
        if ver and build:
            return ver, build
    if t:
        return t, "manual"
    return "0", "manual"


def _write_version_json(dest_root: Path, *, tag: str, zip_name: str) -> None:
    import json

    version, build = _parse_tag_version(tag)
    data = {
        "version": version,
        "build": build,
        "tag": tag,
        "release": build,
        "prerelease": "beta" in build.lower() or "alpha" in build.lower(),
        "source": "curl-zip",
        "zip": zip_name,
    }
    (dest_root / "version.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _set_active_relative(install_dir: Path, rel: str) -> None:
    """写入 camoufox 配置 active_version（相对 INSTALL_DIR）。"""
    try:
        configure_install_dir(install_dir)
        import camoufox.multiversion as multiversion  # type: ignore

        multiversion.set_active(rel.replace("\\", "/"))
        log.info("stage=set_active %s", rel)
    except Exception as e:
        log.warning("set_active failed: %s", e)


def _extract_zip(zip_path: Path, dest_root: Path, *, tag: str) -> Path:
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    log.info("stage=extract zip=%s to=%s", zip_path.name, dest_root)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_root)
    exe = _find_exe_under(dest_root)
    if not exe:
        raise CamoufoxRuntimeError(f"解压后未找到 camoufox 可执行文件: {dest_root}")
    _write_version_json(dest_root, tag=tag, zip_name=zip_path.name)
    log.info("stage=extract_done exe=%s size=%s", exe, exe.stat().st_size)
    return exe


def fetch_browser(settings: Settings, *, logger: logging.Logger | None = None) -> Path:
    """
    下载 Camoufox 到项目 browsers/camoufox。
    大文件优先 curl + 代理；不走会 cleanup 全局/项目目录的 pkgman.install。
    """
    lg = logger or log
    install_dir = resolve_camoufox_dir(settings)
    proxy = resolve_proxy(settings)
    proxies = apply_proxy_env(proxy)
    _patch_requests_proxies(proxies)
    install_dir.mkdir(parents=True, exist_ok=True)
    lg.info("stage=fetch_start dir=%s proxy=%s", install_dir, proxy or "-")

    # 解析 asset
    tag = ""
    zip_url = ""
    try:
        lg.info("stage=fetch_resolve method=html")
        tag, zip_url = _pick_win_asset_from_html(proxies)
    except Exception as e:
        lg.warning("html resolve failed: %s", e)
        lg.info("stage=fetch_resolve method=api")
        tag, zip_url = _pick_win_asset_from_api(proxies)

    lg.info("stage=fetch_asset tag=%s url=%s", tag, zip_url.split("?")[0])
    safe_tag = re.sub(r"[^\w.-]+", "_", tag or "camoufox")
    zip_name = zip_url.rstrip("/").split("/")[-1] or f"camoufox-{safe_tag}-win.zip"
    dl_dir = install_dir / _DL_DIRNAME
    zip_path = dl_dir / zip_name

    if zip_path.exists() and zip_path.stat().st_size > 1_000_000:
        lg.info("stage=reuse_zip size_mb=%.1f", zip_path.stat().st_size / (1024 * 1024))
    else:
        _curl_download(zip_url, zip_path, proxy)

    dest_root = install_dir / "browsers" / "manual" / safe_tag
    exe = _extract_zip(zip_path, dest_root, tag=tag or safe_tag)
    rel_active = f"browsers/manual/{safe_tag}"
    _set_active_relative(install_dir, rel_active)
    (install_dir / "ACTIVE.txt").write_text(str(exe) + "\n", encoding="utf-8")

    try:
        configure_install_dir(install_dir)
    except Exception as e:
        lg.debug("configure after fetch: %s", e)

    checked = _launch_path_strict(install_dir)
    lg.info("stage=fetch_done exe=%s active=%s", checked, rel_active)
    return install_dir


__all__ = [
    "CamoufoxRuntimeError",
    "DEFAULT_CAMOUFOX_REL",
    "DEFAULT_PROXY",
    "apply_proxy_env",
    "browser_status",
    "configure_install_dir",
    "ensure_browser",
    "fetch_browser",
    "is_browser_installed",
    "resolve_camoufox_dir",
    "resolve_proxy",
]
