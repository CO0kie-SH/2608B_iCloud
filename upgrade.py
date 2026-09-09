"""Update the project from a local release zip or GitHub."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent

# PROJECT-SPECIFIC: replace with your GitHub owner/repo
_REPO = "CO0kie-SH/2608B_iCloud"
_REMOTE_URL = f"https://github.com/{_REPO}.git"

# PROJECT-SPECIFIC: zip filename prefix
_ZIP_PREFIX = "2608B_iCloud"


def _read_version(root: Path) -> str:
    version_file = root / "VERSION"
    if version_file.exists():
        return version_file.read_text(encoding="utf-8").strip()
    return "0.0.0"


def _show_version(root: Path, label: str = "当前版本") -> None:
    print(f"{label}: v{_read_version(root)}")


def run_git(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        sys.exit("错误: 系统中未找到 git 命令，请确认 git 已安装并在 PATH 中")
    except subprocess.TimeoutExpired:
        sys.exit(f"错误: Git 操作超时 ({timeout}s): {' '.join(cmd)}")


def resolve_branch() -> str:
    code, stdout, _ = run_git(["git", "branch", "--show-current"])
    branch = stdout.strip() if code == 0 else ""
    if branch:
        return branch
    for name in ("main", "master"):
        code2, stdout2, _ = run_git(["git", "branch", "--list", name])
        if code2 == 0 and stdout2.strip():
            return name
    return "main"


_ZIP_NAME_RE = re.compile(
    rf"^{re.escape(_ZIP_PREFIX)}_v(\d+)\.(\d+)\.(\d+)_(\d{{8}}-\d{{6}})\.zip$"
)


def _zip_sort_key(path: Path) -> Optional[tuple[int, int, int, str]]:
    match = _ZIP_NAME_RE.match(path.name)
    if match is None:
        return None
    return (int(match[1]), int(match[2]), int(match[3]), match[4])


def _find_latest_zip(root: Path) -> Optional[Path]:
    release_dir = root / "release"
    if not release_dir.is_dir():
        return None
    candidates = []
    for path in release_dir.glob(f"{_ZIP_PREFIX}_*.zip"):
        key = _zip_sort_key(path)
        if key is not None:
            candidates.append((key, path))
    if not candidates:
        return None
    return max(candidates)[1]


def _is_safe_member(root: Path, member_name: str) -> bool:
    member_path = Path(member_name)
    if member_path.is_absolute():
        return False
    target = (root / member_path).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _validate_zip(root: Path, zf: zipfile.ZipFile) -> None:
    for member in zf.infolist():
        if member.is_dir():
            continue
        if not _is_safe_member(root, member.filename):
            sys.exit(f"错误: 更新包包含不安全路径: {member.filename}")


def _read_zip_manifest(zf: zipfile.ZipFile) -> dict:
    try:
        with zf.open("RELEASE.json") as fp:
            return json.loads(fp.read().decode("utf-8"))
    except KeyError:
        return {}
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        sys.exit(f"错误: RELEASE.json 无法解析: {e}")


def _print_manifest(zip_path: Path, manifest: dict) -> None:
    print()
    print("离线更新包信息")
    print(f"  文件: {zip_path.name}")
    if not manifest:
        print("  RELEASE.json: 未找到，将按传统 zip 更新")
        return
    print(f"  项目: {manifest.get('project', '-')}")
    print(f"  更新包版本: v{manifest.get('version', '-')}")
    print(f"  tag: {manifest.get('tag') or '无'}")
    print(f"  commit: {manifest.get('commit', '-')}")
    print(f"  created_at: {manifest.get('created_at', '-')}")


def _member_files(zf: zipfile.ZipFile) -> list[str]:
    """Return member file paths with a single wrapping directory stripped."""
    names = [m.filename for m in zf.infolist() if not m.is_dir()]
    top_levels = {name.split("/", 1)[0] for name in names}
    if len(top_levels) == 1:
        prefix = next(iter(top_levels)) + "/"
        if f"{prefix}VERSION" in names:
            return [name[len(prefix):] for name in names if name.startswith(prefix)]
    return names


def _backup_overwritten(root: Path, source_dir: Path) -> None:
    """Zip up files that the incoming package will overwrite."""
    to_backup = []
    for item in source_dir.rglob("*"):
        if not item.is_file() or item.name == "RELEASE.json":
            continue
        rel = item.relative_to(source_dir).as_posix()
        if (root / rel).exists():
            to_backup.append(rel)
    if not to_backup:
        print("没有将被覆盖的既有文件，跳过备份")
        return
    release_dir = root / "release"
    release_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = release_dir / f"backup_{stamp}.zip"
    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as bf:
        for rel in to_backup:
            bf.write(root / rel, rel)
    print(f"已备份将被覆盖的 {len(to_backup)} 个文件 -> {backup_path.name}")


def _merge_dir(src: Path, dst: Path) -> None:
    """Merge src into dst file-by-file, preserving files that exist only in dst."""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name == "RELEASE.json":
            continue
        dest_item = dst / item.name
        if item.is_dir():
            _merge_dir(item, dest_item)
        else:
            shutil.copy2(item, dest_item)


def _offline_update(root: Path, zip_path: Path) -> None:
    print(f"找到本地更新包: {zip_path.name}")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            _validate_zip(root, zf)
            manifest = _read_zip_manifest(zf)
            _print_manifest(zip_path, manifest)
            if not _confirm("是否应用这个本地更新包？"):
                print("已取消离线更新")
                return

            with tempfile.TemporaryDirectory(prefix="project-upgrade-") as tmp:
                tmp_root = Path(tmp)
                zf.extractall(tmp_root)

                source_dir = tmp_root
                for item in tmp_root.iterdir():
                    if item.is_dir() and (item / "VERSION").exists():
                        source_dir = item
                        break

                _backup_overwritten(root, source_dir)
                _merge_dir(source_dir, root)

        releases_dir = root / "release"
        releases_dir.mkdir(exist_ok=True)
        used_path = releases_dir / f"_used_{zip_path.name}"
        if used_path.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            used_path = releases_dir / f"_used_{stamp}_{zip_path.name}"
        shutil.move(str(zip_path), str(used_path))
    except (OSError, zipfile.BadZipFile) as e:
        sys.exit(f"错误: 离线更新失败: {e}")
    print("离线更新完成")


def _get_remote_url() -> Optional[str]:
    code, stdout, _ = run_git(["git", "remote", "get-url", "origin"])
    if code == 0:
        url = stdout.strip()
        if url:
            return url
    return None


def _ensure_remote() -> None:
    existing = _get_remote_url()
    if existing is None:
        print(f"正在添加 origin -> {_REMOTE_URL}")
        code, _, stderr = run_git(["git", "remote", "add", "origin", _REMOTE_URL])
        if code != 0:
            sys.exit(f"错误: 添加远程仓库失败\n{stderr.strip()}")
        return

    if existing == _REMOTE_URL:
        return

    print("错误: origin 已指向其他远程仓库")
    print(f"  当前: {existing}")
    print(f"  目标: {_REMOTE_URL}")
    sys.exit("请确认仓库地址后再从 GitHub 更新")


def _github_update() -> None:
    _ensure_remote()
    branch = resolve_branch()
    print(f"正在从 GitHub 拉取 {branch} 分支 ...")
    code, stdout, stderr = run_git(["git", "pull", "origin", branch], timeout=120)
    if code != 0:
        output = (stderr + stdout).strip()
        sys.exit(f"错误: 拉取失败\n{output}")
    print(stdout.strip() or "GitHub 更新完成")


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt + " (y/n): ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _pause() -> None:
    try:
        input("按任意键继续...")
    except EOFError:
        pass


def _run_check(root: Path) -> None:
    print("[check] 检测更新源...")
    zip_path = _find_latest_zip(root)
    if zip_path is None:
        print("[check] 未找到本地更新包（正式运行时将询问是否从 GitHub 拉取）")
        return
    print(f"[check] 最新更新包: {zip_path.name}")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            _validate_zip(root, zf)
            manifest = _read_zip_manifest(zf)
            _print_manifest(zip_path, manifest)
            members = [m for m in _member_files(zf) if m != "RELEASE.json"]
    except (OSError, zipfile.BadZipFile) as e:
        sys.exit(f"错误: 读取更新包失败: {e}")
    overwrite = [rel for rel in members if (root / rel).exists()]
    added = [rel for rel in members if not (root / rel).exists()]
    print(f"[check] 应用后将覆盖 {len(overwrite)} 个、新增 {len(added)} 个文件")
    for rel in overwrite[:20]:
        print(f"  ~ {rel}")
    for rel in added[:20]:
        print(f"  + {rel}")
    if len(overwrite) > 20 or len(added) > 20:
        print("  ...（仅显示前 20 个）")
    print("[check] 检查完成（未做任何修改）")


def main() -> None:
    parser = argparse.ArgumentParser(description="项目更新工具")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只预览最新更新包与将覆盖/新增的文件，不做任何修改",
    )
    args = parser.parse_args()

    os.chdir(SCRIPT_DIR)
    root = SCRIPT_DIR

    if args.check:
        _run_check(root)
        return

    _show_version(root, "更新前版本")

    print("[1/2] 检测更新源...")
    zip_path = _find_latest_zip(root)
    if zip_path:
        _offline_update(root, zip_path)
    else:
        print("未找到本地更新包")
        if _confirm("是否从 GitHub 拉取更新？"):
            _github_update()
        else:
            print("已取消更新")
            return

    print("[2/2] 确认版本...")
    _show_version(root, "更新后版本")

    print("\n更新完成。")
    _pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n操作已取消。")
        sys.exit(0)
