from __future__ import annotations

import os
import platform
import secrets
from typing import Callable


def detect_os() -> str:
    """返回规范化系统名：windows / linux / darwin / other。"""
    name = platform.system().lower()
    if name.startswith("windows"):
        return "windows"
    if name == "linux":
        return "linux"
    if name == "darwin":
        return "darwin"
    return name or "other"


def _linux_urandom(n: int) -> bytes:
    """Linux: 优先 /dev/urandom（阻塞极少的内核 CSPRNG）。"""
    with open("/dev/urandom", "rb") as f:
        data = f.read(n)
    if len(data) != n:
        raise OSError(f"/dev/urandom returned {len(data)} bytes, want {n}")
    return data


def _linux_getrandom(n: int) -> bytes:
    """Linux: os.getrandom（glibc getrandom 包装，不依赖打开设备节点）。"""
    # flags=0: 阻塞直到熵池初始化完成
    data = os.getrandom(n, flags=0)
    if len(data) != n:
        raise OSError(f"os.getrandom returned {len(data)} bytes, want {n}")
    return data


def _windows_secrets(n: int) -> bytes:
    """Windows: secrets → CryptGenRandom / BCryptGenRandom。"""
    return secrets.token_bytes(n)


def _darwin_secrets(n: int) -> bytes:
    """macOS: secrets / getentropy。"""
    return secrets.token_bytes(n)


def _fallback_secrets(n: int) -> bytes:
    return secrets.token_bytes(n)


def _pick_backend() -> tuple[str, str, Callable[[int], bytes]]:
    system = detect_os()
    if system == "linux":
        # 优先 getrandom，失败再 urandom，再 secrets
        if hasattr(os, "getrandom"):
            return system, "linux:os.getrandom", _linux_getrandom
        if os.path.exists("/dev/urandom"):
            return system, "linux:/dev/urandom", _linux_urandom
        return system, "linux:secrets.token_bytes", _fallback_secrets
    if system == "windows":
        return system, "windows:secrets.token_bytes", _windows_secrets
    if system == "darwin":
        return system, "darwin:secrets.token_bytes", _darwin_secrets
    return system, f"{system}:secrets.token_bytes", _fallback_secrets


_SYSTEM, _BACKEND_NAME, _BACKEND_FN = _pick_backend()


def secure_random_bytes(n: int = 32) -> bytes:
    """按当前系统选择安全随机接口，生成 n 字节。"""
    if n <= 0:
        raise ValueError("n must be positive")
    try:
        return _BACKEND_FN(n)
    except Exception:
        # 任意平台最终回退 secrets
        return secrets.token_bytes(n)


def random_backend_info() -> dict[str, str]:
    return {
        "system": _SYSTEM,
        "backend": _BACKEND_NAME,
        "platform": platform.platform(),
    }
