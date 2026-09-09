"""Push the current git repo and rollback tags to GitHub."""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent

# PROJECT-SPECIFIC: replace with your GitHub owner/repo
_REPO = "CO0kie-SH/2608B_iCloud"
_REMOTE_URL = f"https://github.com/{_REPO}.git"


def _read_version() -> str:
    version_file = Path("VERSION")
    if version_file.exists():
        return version_file.read_text(encoding="utf-8").strip()
    return "0.0.0"


def _show_version() -> None:
    print(f"当前版本: v{_read_version()}")


def run_git(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        sys.exit("错误: 系统中未找到 git 命令，请确认 git 已安装并在 PATH 中")
    except subprocess.TimeoutExpired:
        sys.exit(f"错误: Git 操作超时 ({timeout}s): {' '.join(cmd)}")


def ensure_git_repo() -> None:
    if not Path(".git").exists():
        sys.exit("错误: 当前目录不是一个 Git 仓库")


def ensure_at_least_one_commit() -> None:
    code, _, _ = run_git(["git", "rev-parse", "HEAD"])
    if code != 0:
        sys.exit("错误: 仓库中没有任何提交，请先运行 pack.py 完成一次提交")


def get_remote_url(remote_name: str = "origin") -> Optional[str]:
    code, stdout, _ = run_git(["git", "remote", "get-url", remote_name])
    if code == 0:
        url = stdout.strip()
        if url:
            return url
    return None


def resolve_branch() -> str:
    code, stdout, _ = run_git(["git", "branch", "--show-current"])
    branch = stdout.strip() if code == 0 else ""
    if branch:
        return branch

    for name in ("main", "master"):
        code2, stdout2, _ = run_git(["git", "branch", "--list", name])
        if code2 == 0 and stdout2.strip():
            code3, _, stderr3 = run_git(["git", "checkout", name])
            if code3 == 0:
                return name
            sys.exit(f"错误: 无法切换到 {name} 分支\n{stderr3.strip()}")

    code4, _, stderr4 = run_git(["git", "checkout", "-b", "main"])
    if code4 != 0:
        sys.exit(f"错误: 无法创建 main 分支\n{stderr4.strip()}")
    return "main"


def ensure_remote() -> None:
    existing = get_remote_url("origin")

    if existing is None:
        print(f"正在添加 origin -> {_REMOTE_URL}")
        code, _, stderr = run_git(["git", "remote", "add", "origin", _REMOTE_URL])
        if code != 0:
            sys.exit(f"错误: 添加远程仓库失败\n{stderr.strip()}")
        return

    if existing == _REMOTE_URL:
        print(f"origin: {existing}")
        return

    print("错误: origin 已指向其他远程仓库")
    print(f"  当前: {existing}")
    print(f"  目标: {_REMOTE_URL}")
    sys.exit("请确认仓库地址后再运行 push.py，避免推送到错误仓库")


def show_changes() -> None:
    print("本地状态:")
    code, stdout, _ = run_git(["git", "status", "--short"])
    if code == 0 and stdout.strip():
        print(stdout.strip())
    else:
        print("(干净)")


def _check_remote_readonly() -> None:
    existing = get_remote_url("origin")
    if existing is None:
        print(f"[check] origin 未配置（正式运行时将添加 -> {_REMOTE_URL}）")
        return
    if existing == _REMOTE_URL:
        print(f"[check] origin: {existing}")
        return
    print("错误: origin 已指向其他远程仓库")
    print(f"  当前: {existing}")
    print(f"  目标: {_REMOTE_URL}")
    sys.exit("请确认仓库地址后再运行 push.py，避免推送到错误仓库")


def _run_checks() -> None:
    print("[check] 检查 Git 仓库...")
    ensure_git_repo()
    print("[check] 检查提交...")
    ensure_at_least_one_commit()
    print("[check] 检查 origin...")
    _check_remote_readonly()
    print("[check] 检查当前分支...")
    code, stdout, _ = run_git(["git", "branch", "--show-current"])
    branch = stdout.strip() if code == 0 else ""
    if branch:
        print(f"[check] 当前分支: {branch}")
    else:
        print("[check] 当前无活动分支（正式运行时将自动切换或创建 main）")
    show_changes()
    print("[check] 检查完成（未做任何修改）")


def push_branch(branch: str) -> None:
    print(f"正在推送分支 {branch} -> origin ...")
    code, stdout, stderr = run_git(["git", "push", "-u", "origin", branch], timeout=120)
    if code != 0:
        output = (stderr + stdout).strip()
        sys.exit(f"错误: 推送分支失败\n{output}")
    print(stdout.strip() or "分支推送成功")


def push_tags() -> None:
    print("正在推送 tags ...")
    code, stdout, stderr = run_git(["git", "push", "origin", "--tags"], timeout=120)
    if code != 0:
        output = (stderr + stdout).strip()
        sys.exit(f"错误: 推送 tags 失败\n{output}")
    print(stdout.strip() or "tags 推送成功")


def _pause() -> None:
    try:
        input("按任意键继续...")
    except EOFError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="项目推送工具")
    parser.add_argument("--check", action="store_true", help="只检查仓库、origin 和分支，不推送")
    args = parser.parse_args()

    os.chdir(SCRIPT_DIR)

    _show_version()
    print("版本回退依赖 pack.py 创建的 v版本-时间戳 tag")

    if args.check:
        _run_checks()
        return

    print("[1/3] 环境检查...")
    ensure_git_repo()
    ensure_at_least_one_commit()
    ensure_remote()
    show_changes()

    print("[2/3] 推送分支...")
    branch = resolve_branch()
    push_branch(branch)

    print("[3/3] 推送标签...")
    push_tags()

    print("\n推送完成。")
    _pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n操作已取消。")
        sys.exit(0)
