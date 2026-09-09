"""Pack the project into a distributable zip archive with a rollback tag."""

import argparse
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# PROJECT-SPECIFIC: files/dirs included in the release zip
_INCLUDE_PATHS = [
    "main.py",
    "requirements.txt",
    "README.md",
    "CLAUDE.md",
    "VERSION",
    "pack.py",
    "push.py",
    "upgrade.py",
    ".gitignore",
    ".env.example",
    "proxy.yaml",
    "accounts/_example.yaml.example",
    "web/",
    "tools/",
    "tests/",
    "scripts/",
    "duck/",
    "bat/",
    "third_party/",
    "start_web.bat",
    "cookie_login.bat",
    "generate_alias.bat",
    "produce.bat",
    "produce.sh",
]

# PROJECT-SPECIFIC: files/dirs staged for the release commit
_STAGE_PATHS = list(_INCLUDE_PATHS)

# PROJECT-SPECIFIC: files/dirs excluded from zip output
_EXCLUDE_PREFIXES = [
    "docs/",
    "release/",
    ".clinerules/",
    "db/",
    "logs/",
    "browsers/",
    "sava/",
]

# PROJECT-SPECIFIC: zip filename prefix
_ZIP_PREFIX = "2608B_iCloud"

# PROJECT-SPECIFIC: .gitignore content
_GITIGNORE_CONTENT = """\
.env
auth_credentials.local.txt
accounts/*
!accounts/_example.yaml.example
db/
*.db
*.bak
sava/
2026-09-06_*.md
browsers/
logs/
__pycache__/
*.py[cod]
*.egg-info/
.venv/
venv/
release/
"""


def run_git(cmd: list[str], root: Path, timeout: int = 30) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        sys.exit("错误: 系统中未找到 git 命令，请确认 git 已安装并在 PATH 中")
    except subprocess.TimeoutExpired:
        sys.exit(f"错误: Git 操作超时 ({timeout}s): {' '.join(cmd)}")


def _read_version(root: Path) -> str:
    version_file = root / "VERSION"
    if version_file.exists():
        version = version_file.read_text(encoding="utf-8").strip()
        if version:
            return version
    return "0.0.0"


def _show_version(root: Path) -> None:
    print(f"当前版本: v{_read_version(root)}")


def _check_git(root: Path) -> None:
    code, stdout, stderr = run_git(["git", "--version"], root, timeout=10)
    if code != 0:
        sys.exit(f"错误: Git 不可用\n{(stderr + stdout).strip()}")


def _ensure_gitignore(root: Path) -> None:
    gitignore = root / ".gitignore"
    if gitignore.exists():
        return
    gitignore.write_text(_GITIGNORE_CONTENT, encoding="utf-8")
    print("已创建 .gitignore")


def _ensure_version(root: Path) -> None:
    version_file = root / "VERSION"
    if version_file.exists():
        return
    version_file.write_text("1.0.0\n", encoding="utf-8")
    print("已创建 VERSION: 1.0.0")


def _git_init(root: Path) -> None:
    if (root / ".git").exists():
        print("Git 仓库已存在，跳过 git init")
        return
    code, _, stderr = run_git(["git", "init"], root)
    if code != 0:
        sys.exit(f"错误: git init 失败\n{stderr.strip()}")
    print("已初始化 Git 仓库")


def _validate_version(version: str) -> None:
    _parse_version(version, "patch")


def _validate_paths(root: Path) -> None:
    missing = []
    for path in _INCLUDE_PATHS:
        if not (root / path).exists():
            missing.append(path)
    if missing:
        print("警告: 以下 include 路径不存在，将在打包时跳过:")
        for path in missing:
            print(f"  - {path}")


def _validate_release_ignored(root: Path) -> None:
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return
    text = gitignore.read_text(encoding="utf-8", errors="ignore")
    if "release/" not in text and "/release/" not in text:
        print("警告: .gitignore 中没有 release/，建议加入以避免提交发布包")


def _run_checks(root: Path) -> None:
    print("[check] 检查 git...")
    _check_git(root)
    print("[check] 检查 VERSION...")
    if (root / "VERSION").exists():
        version = _read_version(root)
        _validate_version(version)
        print(f"[check] 当前版本: v{version}")
    else:
        print("[check] VERSION 不存在（正式运行时将创建 1.0.0）")
    print("[check] 检查 include 路径...")
    _validate_paths(root)
    print("[check] 检查 release 忽略规则...")
    _validate_release_ignored(root)
    print("[check] 检查完成（未做任何修改）")


def _tracked_in_index(root: Path, path: str) -> bool:
    code, stdout, _ = run_git(["git", "ls-files", "--", path], root)
    return code == 0 and bool(stdout.strip())


def _git_add(root: Path) -> None:
    stage_paths = []
    for path in _STAGE_PATHS:
        if (root / path).exists() or _tracked_in_index(root, path):
            stage_paths.append(path)
        else:
            print(f"警告: 跳过不存在且未跟踪的路径: {path}")
    if not stage_paths:
        print("没有可暂存的路径，跳过 git add")
        return
    code, _, stderr = run_git(["git", "add", "--"] + stage_paths, root)
    if code != 0:
        sys.exit(f"错误: git add 失败\n{stderr.strip()}")
    print("已添加发布文件到暂存区")


def _git_commit(root: Path, message: str) -> bool:
    code, _, _ = run_git(["git", "diff", "--cached", "--quiet"], root)
    if code == 0:
        print("没有暂存变更，跳过 git commit 和 tag")
        return False

    code, _, stderr = run_git(["git", "commit", "-m", message], root)
    if code != 0:
        sys.exit(f"错误: git commit 失败\n{stderr.strip()}")
    print(f"已提交: {message}")
    return True


def _git_head(root: Path) -> str:
    code, stdout, _ = run_git(["git", "rev-parse", "HEAD"], root)
    return stdout.strip() if code == 0 else ""


def _git_tag(root: Path, tag: str, version: str) -> None:
    code, _, stderr = run_git(["git", "tag", "-a", tag, "-m", f"Release v{version}"], root)
    if code != 0:
        sys.exit(f"错误: 创建 tag 失败\n{stderr.strip()}")
    print(f"已打回退标签: {tag}")


def _bump_version(root: Path, current: str) -> str:
    print()
    print(f"下一次发布版本设置 (当前已打包版本: {current})")
    print("  p) patch  {}.{}.{}  (小修复，默认)".format(*_parse_version(current, "patch")))
    print("  n) minor  {}.{}.{}  (新功能)".format(*_parse_version(current, "minor")))
    print("  m) major  {}.{}.{}  (大版本)".format(*_parse_version(current, "major")))
    print(f"  s) skip   保持 {current}")
    try:
        choice = input("选择下一次版本 [p/n/m/s，默认 p]: ").strip().lower()
    except EOFError:
        choice = "p"

    if choice in ("", "p"):
        new = _bump(current, "patch")
    elif choice == "n":
        new = _bump(current, "minor")
    elif choice == "m":
        new = _bump(current, "major")
    else:
        print(f"VERSION 保持为 {current}")
        return current

    (root / "VERSION").write_text(new + "\n", encoding="utf-8")
    print(f"VERSION 已更新为 {new}，将在下一次 pack 时提交和打包")
    return new


def _parse_version(v: str, part: str) -> tuple[int, int, int]:
    pieces_raw = v.split(".")
    if len(pieces_raw) != 3:
        print(f"错误: VERSION 不是合法 semver: {v}")
        sys.exit(1)
    try:
        major, minor, patch = [int(piece) for piece in pieces_raw]
    except ValueError:
        print(f"错误: VERSION 不是合法 semver: {v}")
        sys.exit(1)
    if part == "major":
        return major + 1, 0, 0
    if part == "minor":
        return major, minor + 1, 0
    return major, minor, patch + 1


def _bump(v: str, part: str) -> str:
    return "{}.{}.{}".format(*_parse_version(v, part))


def _should_exclude(member_path: str) -> bool:
    parts = Path(member_path).parts
    if any(part in {"__pycache__", ".pytest_cache"} for part in parts):
        return True

    filename = parts[-1] if parts else ""
    if filename.endswith((".pyc", ".pyo", ".tmp", ".temp", ".zip", ".bak")):
        return True
    if filename in {".env", "auth_credentials.local.txt"}:
        return True
    if filename.startswith("_") and "scripts" in parts:
        return True

    normalized = member_path.replace("\\", "/")
    if normalized.startswith("accounts/") and not normalized.endswith("_example.yaml.example"):
        return True
    return any(normalized.startswith(prefix) for prefix in _EXCLUDE_PREFIXES)


def _iter_package_files(root: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for include in _INCLUDE_PATHS:
        target = root / include
        if not target.exists():
            continue
        if target.is_file():
            rel = include.replace("\\", "/")
            if not _should_exclude(rel):
                files.append((target, rel))
        elif target.is_dir():
            for file_path in target.rglob("*"):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(root).as_posix()
                if not _should_exclude(rel):
                    files.append((file_path, rel))
    return files


def _build_manifest(
    root: Path,
    version: str,
    tag: str | None,
    created_at: str,
    zip_name: str,
    included_paths: list[str],
) -> dict:
    return {
        "project": _ZIP_PREFIX,
        "version": version,
        "tag": tag,
        "commit": _git_head(root),
        "created_at": created_at,
        "zip_name": zip_name,
        "included_paths": included_paths,
    }


def _print_release_preview(
    version: str,
    tag: str,
    zip_name: str,
    message: str,
    package_files: list[tuple[Path, str]],
) -> None:
    print()
    print("发布预览")
    print(f"  版本: v{version}")
    print(f"  tag（如产生新提交）: {tag}")
    print(f"  zip: {zip_name}")
    print(f"  commit -m: {message}")
    print("  stage 路径:")
    for path in _STAGE_PATHS:
        print(f"    - {path}")
    print(f"  package 文件数: {len(package_files)}")
    print("  include 路径:")
    for path in _INCLUDE_PATHS:
        print(f"    - {path}")


def _create_zip(root: Path, version: str, tag: str | None, created_at: str) -> Path:
    zip_name = f"{_ZIP_PREFIX}_v{version}_{created_at}.zip"
    release_dir = root / "release"
    release_dir.mkdir(exist_ok=True)
    zip_path = release_dir / zip_name

    package_files = _iter_package_files(root)
    manifest = _build_manifest(
        root=root,
        version=version,
        tag=tag,
        created_at=created_at,
        zip_name=zip_name,
        included_paths=[rel for _, rel in package_files],
    )

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path, rel in package_files:
                zf.write(file_path, rel)
                print(f"  + {rel}")
            zf.writestr("RELEASE.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            print("  + RELEASE.json")
    except OSError as e:
        print(f"错误: 创建 zip 文件失败: {e}")
        sys.exit(1)

    print(f"\n已生成完整包: {zip_path}")
    print(f"包内版本: v{version}")
    if tag:
        print(f"回退标签: {tag}")
    else:
        print("回退标签: 无（本次无新提交）")
    print(f"包含 {len(package_files)} 个文件 + RELEASE.json")
    return zip_path


def _read_message(args_message: str | None) -> str:
    if args_message:
        return args_message
    try:
        message = input("\n请输入 commit -m: ").strip()
    except EOFError:
        print("错误: 无法读取输入，请使用 -m 参数指定提交信息")
        sys.exit(1)
    if not message:
        print("错误: commit message 不能为空")
        sys.exit(1)
    return message


def _pause() -> None:
    try:
        input("按任意键继续...")
    except EOFError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="项目打包发布工具")
    parser.add_argument("-m", "--message", default=None, help="commit -m 提交信息")
    parser.add_argument("--check", action="store_true", help="只检查环境，不提交、不打 tag、不打包")
    args = parser.parse_args()

    os.chdir(SCRIPT_DIR)
    root = SCRIPT_DIR

    if args.check:
        _run_checks(root)
        return

    _show_version(root)

    print("[1/5] 环境准备...")
    _check_git(root)
    _ensure_gitignore(root)
    _ensure_version(root)
    _git_init(root)
    _validate_release_ignored(root)

    print("[2/5] 发布预览...")
    message = _read_message(args.message)
    version = _read_version(root)
    _validate_version(version)
    created_at = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = f"v{version}-{created_at}"
    zip_name = f"{_ZIP_PREFIX}_v{version}_{created_at}.zip"
    package_files = _iter_package_files(root)
    _print_release_preview(version, tag, zip_name, message, package_files)

    print("[3/5] 提交代码...")
    _git_add(root)
    committed = _git_commit(root, message)

    print("[4/5] 打版本标签...")
    if committed:
        _git_tag(root, tag, version)
    else:
        print("跳过 tag（没有新提交）")
        tag = None

    print("[5/5] 生成发布包...")
    zip_path = _create_zip(root, version, tag, created_at)

    print(f"发布包: {zip_path.name}")
    _bump_version(root, version)

    print("\n打包完成。")
    _pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n操作已取消。")
        sys.exit(0)
