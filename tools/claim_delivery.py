from __future__ import annotations

"""本地领取订单发信：导出注册机可直接粘贴的 邮箱----取码地址。"""

from typing import Any
from urllib.parse import quote


def public_base_url(settings: Any | None = None, request_base: str | None = None) -> str:
    """对外取码根地址：请求 host > PUBLIC_BASE_URL > 本地默认。"""
    raw = (request_base or "").strip().rstrip("/")
    if raw:
        return raw
    if settings is not None:
        configured = str(getattr(settings, "public_base_url", "") or "").strip().rstrip("/")
        if configured:
            return configured
    import os

    env = str(os.getenv("PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if env:
        return env
    return "http://127.0.0.1:8770"


def build_code_url(base_url: str, token: str, *, email: str = "") -> str:
    root = (base_url or "").strip().rstrip("/") or "http://127.0.0.1:8770"
    key = (token or "").strip()
    if not key:
        return ""
    # 注册机认 email----https://... 这种接码地址；token 放 query 即可。
    url = f"{root}/api/v1/code?token={quote(key, safe='')}"
    mail = (email or "").strip()
    if mail:
        url += f"&email={quote(mail, safe='')}"
    return url


def build_claim_lines(
    order: dict[str, Any],
    *,
    base_url: str = "http://127.0.0.1:8770",
) -> list[str]:
    """每行：邮箱----取码URL，给注册机直接导入。"""
    lines: list[str] = []
    for item in order.get("items") or []:
        hme = str(item.get("hme") or "").strip()
        token = str(item.get("access_token") or "").strip()
        if not hme:
            continue
        code_url = build_code_url(base_url, token, email=hme)
        if code_url:
            lines.append(f"{hme}----{code_url}")
        else:
            lines.append(hme)
    if lines:
        return lines
    # 兼容只有 emails 的旧结构
    return [str(x).strip() for x in (order.get("emails") or []) if str(x).strip()]


def build_claim_email(
    order: dict[str, Any],
    *,
    base_url: str = "http://127.0.0.1:8770",
) -> tuple[str, str]:
    order_no = str(order.get("order_no") or "").strip()
    note = str(order.get("note") or "").strip() or "（无）"
    contact = str(order.get("contact_email") or "").strip()
    lines = build_claim_lines(order, base_url=base_url)
    count = int(order.get("count") or len(lines) or 0)
    subject = f"[iCloud邮箱领取] {order_no} · {count}个"
    body_lines = [
        "这是本地号池的领取凭证，不是真实消费订单。",
        "领走即占用，请自行保管；本系统不再负责这些邮箱的后续用途。",
        "",
        "注册机导入格式（每行一条）：",
        "邮箱----取码地址",
        "",
        f"订单号：{order_no}",
        f"数量：{count}",
        f"常用邮箱：{contact}",
        f"备注：{note}",
        f"时间(UTC)：{order.get('created_at') or ''}",
        f"取码服务：{base_url}",
        "",
        "凭证列表：",
        *lines,
        "",
        "可直接复制：",
        "\n".join(lines),
    ]
    return subject, "\n".join(body_lines)


def deliver_claim_order(
    mail_client: Any,
    order: dict[str, Any],
    *,
    base_url: str = "http://127.0.0.1:8770",
) -> dict[str, str]:
    """用已配置的 SMTP 账户把订单发到 contact_email。"""
    to_addr = str(order.get("contact_email") or "").strip()
    if not to_addr or "@" not in to_addr:
        raise ValueError("常用邮箱无效，无法发信")
    subject, body = build_claim_email(order, base_url=base_url)
    mail_client.send(to_addr, subject, body)
    return {
        "deliver_status": "sent",
        "deliver_from": str(getattr(mail_client, "mail", "") or ""),
        "deliver_error": "",
    }
