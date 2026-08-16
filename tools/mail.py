from __future__ import annotations

import base64
import email
import imaplib
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage, Message
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import env_bool, env_int, env_str


# 兼容旧常量（默认 iCloud）
IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.mail.me.com"
SMTP_PORT = 587

DEFAULT_FOLDERS = (
    "INBOX",
    "Junk",
    "Archive",
    "Deleted Messages",
    "Sent Messages",
    "Drafts",
)

DEFAULT_FOLDERS_163 = (
    "INBOX",
    "Drafts",
    "Sent Messages",
    "Deleted Messages",
    "Junk",
)

# IMAP SPECIAL-USE / 常见显示名 → 逻辑角色。163 垃圾箱不叫 Junk，
# LIST 出来是 modified UTF-7 的「垃圾邮件」，只能靠 \Junk 标志或中文名对齐。
_FOLDER_ROLE_BY_FLAG: dict[str, str] = {
    "inbox": "INBOX",
    "junk": "Junk",
    "spam": "Junk",
    "drafts": "Drafts",
    "sent": "Sent",
    "trash": "Trash",
    "bin": "Trash",
    "archive": "Archive",
}

_FOLDER_ROLE_BY_NAME: dict[str, str] = {
    "inbox": "INBOX",
    "junk": "Junk",
    "spam": "Junk",
    "bulk mail": "Junk",
    "junk e-mail": "Junk",
    "junk email": "Junk",
    "junk mail": "Junk",
    "垃圾邮件": "Junk",
    "垃圾箱": "Junk",
    "drafts": "Drafts",
    "draft": "Drafts",
    "草稿箱": "Drafts",
    "草稿": "Drafts",
    "sent": "Sent",
    "sent messages": "Sent",
    "sent items": "Sent",
    "已发送": "Sent",
    "已发送邮件": "Sent",
    "deleted messages": "Trash",
    "deleted items": "Trash",
    "trash": "Trash",
    "已删除": "Trash",
    "已删除邮件": "Trash",
    "archive": "Archive",
}

# 逻辑角色 / 常见箱名 → 中文展示。163 的「垃圾邮件」和英文 Junk 都归到「垃圾箱」。
MAILBOX_LABELS: dict[str, str] = {
    "INBOX": "收件箱",
    "Junk": "垃圾箱",
    "Drafts": "草稿箱",
    "Sent": "已发送",
    "Sent Messages": "已发送",
    "Trash": "已删除",
    "Deleted Messages": "已删除",
    "Archive": "归档",
    "垃圾邮件": "垃圾箱",
    "垃圾箱": "垃圾箱",
    "草稿箱": "草稿箱",
    "已发送": "已发送",
    "已删除": "已删除",
}

MAILBOX_ORDER: tuple[str, ...] = ("INBOX", "Junk")


def mailbox_label(mailbox: str) -> str:
    """逻辑名 / UTF-7 / 中文名 → 界面展示名。"""
    raw = (mailbox or "").strip()
    if not raw:
        return ""
    if raw in MAILBOX_LABELS:
        return MAILBOX_LABELS[raw]
    decoded = decode_imap_utf7(raw)
    if decoded in MAILBOX_LABELS:
        return MAILBOX_LABELS[decoded]
    role = classify_folder_role(raw, decoded, ())
    if role and role in MAILBOX_LABELS:
        return MAILBOX_LABELS[role]
    key = decoded.lower()
    for name, label in MAILBOX_LABELS.items():
        if name.lower() == key:
            return label
    return decoded or raw


def mailbox_role(mailbox: str) -> str:
    """把 IMAP 原始箱名/中文名归一为稳定的业务目录键。"""
    raw = (mailbox or "").strip()
    if not raw:
        return ""
    decoded = decode_imap_utf7(raw)
    role = classify_folder_role(raw, decoded, ())
    return role or raw


@dataclass(frozen=True)
class ImapFolder:
    """IMAP LIST 解析结果。raw 用于 SELECT，name 给人看，role 给同步逻辑用。"""

    raw: str
    name: str
    flags: tuple[str, ...] = ()
    role: str = ""
    selectable: bool = True


def decode_imap_utf7(name: str) -> str:
    """IMAP mailbox modified UTF-7（RFC 3501）→ Unicode。"""
    if not name or "&" not in name:
        return name
    out: list[str] = []
    i = 0
    n = len(name)
    while i < n:
        amp = name.find("&", i)
        if amp < 0:
            out.append(name[i:])
            break
        if amp > i:
            out.append(name[i:amp])
        dash = name.find("-", amp + 1)
        if dash < 0:
            out.append(name[amp:])
            break
        chunk = name[amp + 1 : dash]
        i = dash + 1
        if chunk == "":
            out.append("&")
            continue
        b64 = chunk.replace(",", "/")
        b64 += "=" * ((4 - len(b64) % 4) % 4)
        try:
            out.append(base64.b64decode(b64).decode("utf-16-be"))
        except Exception:
            out.append(name[amp : dash + 1])
    return "".join(out)


def classify_folder_role(raw: str, decoded: str, flags: tuple[str, ...]) -> str:
    for flag in flags:
        role = _FOLDER_ROLE_BY_FLAG.get(flag.lower())
        if role:
            return role
    key = (decoded or raw or "").strip().lower()
    if key == "inbox":
        return "INBOX"
    return _FOLDER_ROLE_BY_NAME.get(key, "") or _FOLDER_ROLE_BY_NAME.get(
        (decoded or "").strip(), ""
    )


def parse_imap_list_line(line: str | bytes) -> ImapFolder | None:
    """解析 IMAP LIST 一行：`(flags) delimiter mailbox`。"""
    text = line.decode("utf-8", errors="replace") if isinstance(line, (bytes, bytearray)) else str(line)
    text = text.strip()
    if not text.startswith("("):
        return None
    depth = 0
    flags_end = -1
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                flags_end = i
                break
    if flags_end < 0:
        return None
    flags = tuple(f.lstrip("\\") for f in text[1:flags_end].split() if f)
    rest = text[flags_end + 1 :].strip()
    if rest.upper().startswith("NIL"):
        rest = rest[3:].strip()
    elif rest.startswith('"'):
        d_end = rest.find('"', 1)
        rest = rest[d_end + 1 :].strip() if d_end >= 0 else ""
    else:
        parts = rest.split(None, 1)
        rest = parts[1] if len(parts) > 1 else ""
    raw = rest.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if not raw:
        return None
    decoded = decode_imap_utf7(raw)
    role = classify_folder_role(raw, decoded, flags)
    selectable = "Noselect" not in flags and "NonExistent" not in flags
    return ImapFolder(raw=raw, name=decoded, flags=flags, role=role, selectable=selectable)


@dataclass(frozen=True)
class MailServerProfile:
    """某一邮件服务商的 IMAP/SMTP 连接参数。"""

    name: str
    imap_host: str
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 587
    # True: SMTP_SSL；False: 明文连上后 STARTTLS
    smtp_ssl: bool = False
    smtp_starttls: bool = True
    # 网易等要求登录后发 IMAP ID
    need_imap_id: bool = False
    folders: tuple[str, ...] = DEFAULT_FOLDERS


# 出厂默认值：开箱即可收取 icloud.com 与 163.com。
# 服务器地址基本固定，需要改（换自建/企业邮或端口被封）时在 .env 覆盖对应项，
# 不必改代码。env 键名 = MAIL_<PREFIX>_IMAP_HOST 等，见 .env.example。
_PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "apple": {
        "prefix": "APPLE",
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "smtp_ssl": False,
        "smtp_starttls": True,
        "need_imap_id": False,
        "folders": DEFAULT_FOLDERS,
    },
    "163mail": {
        "prefix": "163",
        "imap_host": "imap.163.com",
        "imap_port": 993,
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "smtp_starttls": False,
        # 网易要求登录后发 IMAP ID，否则报 Unsafe Login
        "need_imap_id": True,
        "folders": DEFAULT_FOLDERS_163,
    },
}


@lru_cache(maxsize=1)
def mail_server_profiles() -> dict[str, MailServerProfile]:
    """provider 名 → 服务器配置（读一次 .env 后缓存）。"""
    profiles: dict[str, MailServerProfile] = {}
    for name, d in _PROFILE_DEFAULTS.items():
        p = f"MAIL_{d['prefix']}"
        profiles[name] = MailServerProfile(
            name=name,
            imap_host=env_str(f"{p}_IMAP_HOST", d["imap_host"]),
            imap_port=env_int(f"{p}_IMAP_PORT", d["imap_port"]),
            smtp_host=env_str(f"{p}_SMTP_HOST", d["smtp_host"]),
            smtp_port=env_int(f"{p}_SMTP_PORT", d["smtp_port"]),
            smtp_ssl=env_bool(f"{p}_SMTP_SSL", d["smtp_ssl"]),
            smtp_starttls=env_bool(f"{p}_SMTP_STARTTLS", d["smtp_starttls"]),
            need_imap_id=env_bool(f"{p}_IMAP_ID", d["need_imap_id"]),
            folders=d["folders"],
        )
    profiles["icloud"] = profiles["apple"]
    profiles["163"] = profiles["163mail"]
    return profiles


def resolve_mail_profile(provider: str | None) -> MailServerProfile:
    """provider 名 → 服务器配置；未知（含历史的 inbox 兜底）按 apple。"""
    profiles = mail_server_profiles()
    key = (provider or "apple").strip().lower()
    return profiles.get(key) or profiles["apple"]


@dataclass
class MailProbeResult:
    ok: bool
    detail: str


# 判断「代发」时按注册域比较，需要识别两段后缀，否则 a.co.uk 与 b.co.uk 会被当成同源
_MULTI_PART_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "net.uk",
        "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
        "com.hk", "com.tw", "com.au", "net.au", "org.au",
        "co.jp", "or.jp", "ne.jp", "co.kr", "co.nz", "co.za",
        "com.br", "com.mx", "com.sg", "com.my", "co.in", "com.tr",
    }
)

# 还原「收件别名」时要跳过的投递头噪声值
_ALIAS_HEADER_NAMES = (
    "Delivered-To",
    "X-Original-To",
    "Envelope-To",
    "X-Envelope-To",
    "X-Forwarded-To",
    "X-Apple-Relay-To",
)

# iCloud 隐私转发会把原始发件地址改写成
#   <local>_at_<域名下划线分隔>_<hash>_<hash>@icloud.com
# 不还原的话，所有转发邮件的 From/Return-Path 域名都会变成 icloud.com，
# 既无法展示真实发件人，也会让「代发」判断彻底失效（两边恒等）。
_RELAY_REWRITE_SEP = "_at_"
_RELAY_REWRITE_HOSTS = ("icloud.com", "me.com", "mac.com")


class MailMessageParser:
    """邮件 MIME / IMAP FETCH 元数据解析。"""

    @staticmethod
    def decode_mime(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", errors="replace")
            except Exception:
                value = value.decode("latin-1", errors="replace")
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return str(value)

    @classmethod
    def header(cls, msg: Message, name: str) -> str:
        return cls.decode_mime(msg.get(name))

    @classmethod
    def addr_pair(cls, raw: str | None) -> tuple[str, str]:
        """'张三 <a@b.com>' -> ('张三', 'a@b.com')；地址统一小写。"""
        if not raw:
            return "", ""
        name, addr = parseaddr(cls.decode_mime(raw))
        return (name or "").strip(), (addr or "").strip().lower()

    @staticmethod
    def unmask_relay_addr(addr: str) -> str:
        """
        还原 iCloud 隐私转发改写的发件地址，无法确定时返回空串。

        sender_at_service_example_com_x9y8@icloud.com
          -> sender@service.example.com
        """
        a = (addr or "").strip().lower()
        local, _, domain = a.rpartition("@")
        if domain not in _RELAY_REWRITE_HOSTS or _RELAY_REWRITE_SEP not in local:
            return ""

        head, _, tail = local.partition(_RELAY_REWRITE_SEP)
        parts = [p for p in tail.split("_") if p]
        # 尾部是 Apple 加的哈希段（含数字的混合串）；真实域名各段为纯字母或含连字符，
        # 且必须以纯字母 TLD 结尾。从右往左丢弃哈希段。
        while parts and not parts[-1].replace("-", "").isalpha():
            parts.pop()
        if len(parts) < 2 or not head:
            return ""
        return f"{head}@{'.'.join(parts)}"

    @classmethod
    def real_from_addr(cls, addr: str) -> str:
        """展示用发件地址：能还原就用还原值，否则用原值。"""
        return cls.unmask_relay_addr(addr) or (addr or "").strip().lower()

    @classmethod
    def envelope_from_addr(cls, msg: Message) -> str:
        """从 Received-SPF / Authentication-Results 抽出 envelope-from。"""
        blobs: list[str] = []
        for name in ("Received-SPF", "Authentication-Results"):
            for raw in msg.get_all(name) or []:
                blobs.append(cls.decode_mime(raw))
        text = " ".join(blobs)
        if not text:
            return ""
        hit = re.search(
            r"(?:envelope-from|smtp\.mail)\s*=\s*<?([^\s>;]+)>?",
            text,
            re.I,
        )
        if not hit:
            return ""
        return (hit.group(1) or "").strip().strip("<>").lower()

    @classmethod
    def received_spf_header(cls, msg: Message) -> str:
        """保留完整 Received-SPF 值；多条头按原顺序换行拼接。"""
        return "\n".join(
            cls.decode_mime(raw).strip()
            for raw in (msg.get_all("Received-SPF") or [])
            if cls.decode_mime(raw).strip()
        )

    @classmethod
    def addr_list(cls, raw: str | None) -> list[dict[str, str]]:
        """多地址头 -> [{'name','addr'}]，按地址去重保序。"""
        if not raw:
            return []
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for name, addr in getaddresses([cls.decode_mime(raw)]):
            a = (addr or "").strip().lower()
            if not a or a in seen:
                continue
            seen.add(a)
            out.append({"name": (name or "").strip(), "addr": a})
        return out

    @staticmethod
    def domain_key(addr: str) -> str:
        """
        取注册域用于同源比较：bounce@mail.x.com 与 no-reply@x.com 都得到 x.com，
        因此不会被误判成「代发」。
        """
        domain = (addr or "").rpartition("@")[2].strip().lower().rstrip(".")
        if not domain:
            return ""
        parts = domain.split(".")
        if len(parts) <= 2:
            return domain
        if ".".join(parts[-2:]) in _MULTI_PART_SUFFIXES:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:])

    @classmethod
    def alias_candidates(cls, msg: Message, to_addrs: list[dict[str, str]]) -> list[str]:
        """
        收件别名候选：投递类头优先（HME 转发后 To 常仍是原始别名，但被代发时会变），
        其次才是 To。顺序即优先级，供调用方与本地别名表求交。
        """
        out: list[str] = []
        seen: set[str] = set()
        for name in _ALIAS_HEADER_NAMES:
            for raw in msg.get_all(name) or []:
                for item in cls.addr_list(raw):
                    if item["addr"] not in seen:
                        seen.add(item["addr"])
                        out.append(item["addr"])
        for item in to_addrs:
            if item["addr"] not in seen:
                seen.add(item["addr"])
                out.append(item["addr"])
        return out

    @classmethod
    def bodies(cls, msg: Message) -> tuple[str, str]:
        text_parts: list[str] = []
        html_parts: list[str] = []

        def decode_payload(part: Message) -> str:
            payload = part.get_payload(decode=True)
            if payload is None:
                return ""
            charset = part.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except Exception:
                return payload.decode("utf-8", errors="replace")

        if msg.is_multipart():
            for part in msg.walk():
                ctype = (part.get_content_type() or "").lower()
                disp = str(part.get("Content-Disposition") or "").lower()
                if "attachment" in disp:
                    continue
                if ctype == "text/plain":
                    text_parts.append(decode_payload(part))
                elif ctype == "text/html":
                    html_parts.append(decode_payload(part))
        else:
            ctype = (msg.get_content_type() or "").lower()
            content = decode_payload(msg)
            if ctype == "text/html":
                html_parts.append(content)
            else:
                text_parts.append(content)

        return "\n".join(text_parts).strip(), "\n".join(html_parts).strip()

    @classmethod
    def attachments(cls, msg: Message) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "")
            filename = part.get_filename()
            if not filename and "attachment" not in disp.lower():
                continue
            payload = part.get_payload(decode=True)
            size = len(payload) if isinstance(payload, (bytes, bytearray)) else 0
            result.append(
                {
                    "filename": cls.decode_mime(filename) if filename else "(unnamed)",
                    "content_type": part.get_content_type() or "",
                    "size": size,
                    "disposition": disp,
                }
            )
        return result

    @staticmethod
    def parse_fetch_meta(meta: bytes | str) -> dict[str, Any]:
        text = meta.decode("utf-8", errors="replace") if isinstance(meta, (bytes, bytearray)) else str(meta)
        out: dict[str, Any] = {
            "seq": None,
            "uid": "",
            "flags": [],
            "internaldate": "",
            "size": None,
        }
        m = re.match(r"(\d+)\s+\(", text)
        if m:
            out["seq"] = int(m.group(1))
        m = re.search(r"UID\s+(\d+)", text)
        if m:
            out["uid"] = m.group(1)
        m = re.search(r"FLAGS\s+\(([^)]*)\)", text)
        if m:
            out["flags"] = [f for f in m.group(1).split() if f]
        m = re.search(r'INTERNALDATE\s+"([^"]+)"', text)
        if m:
            out["internaldate"] = m.group(1)
        m = re.search(r"RFC822\.SIZE\s+(\d+)", text)
        if m:
            out["size"] = int(m.group(1))
        return out

    @staticmethod
    def html_to_text(html: str) -> str:
        if not html:
            return ""
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p\s*>", "\n", text)
        text = re.sub(r"(?i)</div\s*>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = (
            text.replace("&nbsp;", " ")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&amp;", "&")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
        )
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        return text.strip()

    @classmethod
    def plain_content(cls, text_body: str, html_body: str) -> str:
        if text_body and text_body.strip():
            return text_body.strip()
        return cls.html_to_text(html_body)

    @classmethod
    def extract_code(cls, subject: str, content: str) -> str:
        blob = f"{subject}\n{content}"
        patterns = [
            r"(?:验证码|校验码|动态码|安全码|临时验证码|verification code|verify code|security code|one[- ]?time(?: code| password)?|otp|auth(?:entication)? code)[^\d]{0,20}(\d{4,8})",
            r"(?:code|验证码)\s*[:：是为]?\s*(\d{4,8})",
            r"\b(\d{6})\b",
        ]
        for pat in patterns:
            m = re.search(pat, blob, flags=re.I)
            if m:
                return m.group(1)
        return ""

    @classmethod
    def classify_type(cls, subject: str, from_addr: str, content: str) -> str:
        subj = subject or ""
        frm = from_addr or ""
        body = content or ""
        blob = f"{subj}\n{frm}\n{body}".lower()

        code = cls.extract_code(subj, body)
        code_hints = (
            "验证码",
            "校验码",
            "动态码",
            "临时验证码",
            "verification code",
            "verify code",
            "security code",
            "one-time",
            "one time",
            "otp",
            "auth code",
            "authentication code",
        )
        if code or any(h in blob for h in code_hints):
            return "code"

        if any(k in blob for k in ("欢迎", "welcome", "getting started", "开始使用")):
            return "welcome"
        if any(k in blob for k in ("家人共享", "family sharing", "家庭共享", "受邀加入")):
            return "invite"
        if any(k in blob for k in ("password reset", "reset password", "重置密码", "找回密码")):
            return "security"
        if any(k in blob for k in ("invoice", "receipt", "账单", "发票", "payment")):
            return "billing"
        if any(k in blob for k in ("unsubscribe", "newsletter", "促销", "优惠", "discount")):
            return "promo"
        return "other"

    @classmethod
    def make_summary(
        cls,
        *,
        mail_type: str,
        subject: str,
        from_addr: str,
        to_addr: str,
        content: str,
        code: str = "",
        max_len: int = 180,
    ) -> str:
        content_one = re.sub(r"\s+", " ", content or "").strip()
        if mail_type == "code":
            if code:
                return f"验证码 {code}" + (f"（{subject}）" if subject else "")
            # fallback: first digits already failed
            return subject or content_one[:max_len] or "验证码邮件"
        if mail_type == "welcome":
            return subject or content_one[:max_len] or "欢迎邮件"
        if mail_type == "invite":
            return subject or f"邀请邮件 from {from_addr}" if from_addr else "邀请邮件"

        parts: list[str] = []
        if subject:
            parts.append(subject)
        elif content_one:
            parts.append(content_one[:max_len])
        if from_addr and not parts:
            parts.append(f"from {from_addr}")
        if to_addr and "Hide My Email" in to_addr:
            parts.append(f"to {to_addr}")
        summary = " | ".join(parts) if parts else "(empty)"
        if len(summary) > max_len:
            summary = summary[: max_len - 1] + "…"
        return summary

    @classmethod
    def to_dict(
        cls,
        *,
        mailbox: str,
        meta: dict[str, Any],
        msg: Message,
        include_body: bool = True,
        body_limit: int = 0,
        account: str = "",
    ) -> dict[str, Any]:
        text_body, html_body = cls.bodies(msg)
        attachments = cls.attachments(msg)
        date_header = cls.header(msg, "Date")
        date_parsed = ""
        try:
            if date_header:
                date_parsed = parsedate_to_datetime(date_header).isoformat()
        except Exception:
            date_parsed = ""

        subject = cls.header(msg, "Subject")
        from_addr = cls.header(msg, "From")
        to_addr = cls.header(msg, "To")

        from_name_p, from_addr_raw = cls.addr_pair(msg.get("From"))
        sender_name_p, sender_addr_raw = cls.addr_pair(msg.get("Sender"))
        _, return_path_raw = cls.addr_pair(msg.get("Return-Path"))
        envelope_from = cls.envelope_from_addr(msg)
        received_spf = cls.received_spf_header(msg)
        to_addrs = cls.addr_list(msg.get("To"))
        delivered_to_addrs = cls.alias_candidates(msg, [])
        candidates = cls.alias_candidates(msg, to_addrs)

        # 隐私转发会把三个地址都改写到 @icloud.com；先还原再比较，
        # 否则域名恒等，真代发检测不出、假代发又误报。
        from_addr_p = cls.real_from_addr(from_addr_raw)
        sender_addr_p = cls.real_from_addr(sender_addr_raw)
        return_path_p = cls.real_from_addr(return_path_raw)
        is_masked = bool(cls.unmask_relay_addr(from_addr_raw))

        # Sender 语义上就是代发方；缺失时用 Return-Path（退信地址）兜底
        relay_addr = sender_addr_p or return_path_p
        from_key = cls.domain_key(from_addr_p)
        relay_key = cls.domain_key(relay_addr)
        is_relayed = bool(relay_addr and from_key and relay_key and relay_key != from_key)
        relay_label = f"via {relay_key}" if is_relayed else ""

        plain = cls.plain_content(text_body, html_body)
        mail_type = cls.classify_type(subject, from_addr, plain)
        code = cls.extract_code(subject, plain) if mail_type == "code" else ""
        summary = cls.make_summary(
            mail_type=mail_type,
            subject=subject,
            from_addr=from_addr,
            to_addr=to_addr,
            content=plain,
            code=code,
        )

        # 很多验证码/通知邮件只有 text/html。详情接口仍应提供可读文本，
        # 否则前端的文本视图只能显示“无纯文本正文”。
        body_text_out = plain
        body_html_out = html_body
        if body_limit and body_limit > 0:
            body_text_out = plain[:body_limit]
            body_html_out = html_body[:body_limit]

        return {
            "account": account,
            "mailbox": mailbox,
            "seq": meta.get("seq"),
            "uid": str(meta.get("uid") or ""),
            "flags": meta.get("flags") or [],
            "size": meta.get("size"),
            "date": date_header,
            "date_parsed": date_parsed,
            "internaldate": meta.get("internaldate") or "",
            "from": from_addr,
            "to": to_addr,
            "cc": cls.header(msg, "Cc"),
            "bcc": cls.header(msg, "Bcc"),
            "reply_to": cls.header(msg, "Reply-To"),
            "from_name": from_name_p,
            "from_addr": from_addr_p,
            "from_addr_masked": from_addr_raw if is_masked else "",
            "is_masked_sender": is_masked,
            "sender_name": sender_name_p,
            "sender_addr": sender_addr_p,
            "return_path_addr": return_path_p,
            "received_spf": received_spf,
            "envelope_from": envelope_from,
            "relay_addr": relay_addr if is_relayed else "",
            "is_relayed": is_relayed,
            "relay_label": relay_label,
            "to_addrs": to_addrs,
            "delivered_to": delivered_to_addrs[0] if delivered_to_addrs else "",
            "delivered_to_addrs": delivered_to_addrs,
            "alias_candidates": candidates,
            "subject": subject,
            "message_id": cls.header(msg, "Message-ID"),
            "in_reply_to": cls.header(msg, "In-Reply-To"),
            "references": cls.header(msg, "References"),
            "content_type": msg.get_content_type() or "",
            "type": mail_type,
            "summary": summary,
            "code": code,
            "attachments": attachments,
            "body_text": body_text_out if include_body else "",
            "body_html": body_html_out if include_body else "",
            "body_text_len": len(plain),
            "body_html_len": len(html_body),
        }


class ICloudMailClient:
    """
    多 provider 邮件客户端（IMAP 收信 / SMTP 发信）。

    provider:
      - apple / icloud → imap.mail.me.com
      - 163mail / 163  → imap.163.com（登录后发 IMAP ID）
    类名保留 ICloudMailClient 以兼容旧导入；也可用 MailClient 别名。
    """

    def __init__(
        self,
        mail: str,
        app_password: str,
        timeout: float = 30.0,
        *,
        provider: str = "apple",
        profile: MailServerProfile | None = None,
    ) -> None:
        if not mail or not app_password:
            raise ValueError("mail 与 app_password 不能为空")
        self.mail = mail
        self.app_password = app_password.replace(" ", "")
        self.timeout = timeout
        self.provider = (provider or "apple").strip().lower()
        self.profile = profile or resolve_mail_profile(self.provider)
        self.parser = MailMessageParser()
        self._folder_cache: list[ImapFolder] | None = None

    def _imap_id(self, imap: imaplib.IMAP4) -> None:
        """
        网易邮箱要求 LOGIN 后发送 IMAP ID，否则 SELECT 报：
        Unsafe Login. Please contact kefu@188.com for help

        用 xatom 自动登记扩展命令（标准库默认无 ID）。
        """
        payloads = (
            '("name" "2608B_iCloud" "version" "1.0.0" "vendor" "2608B" "support-email" "support@local")',
            '("name" "IMAPClient" "version" "2.3.1" "vendor" "python" "support-email" "support@local")',
        )
        last: tuple[str, object] | None = None
        for payload in payloads:
            try:
                # xatom 会把 ID 登记进 Commands，避免 illegal command / KeyError
                typ, data = imap.xatom("ID", payload)
            except Exception as e:
                last = ("exc", e)
                continue
            last = (str(typ), data)
            if typ == "OK":
                return
        raise RuntimeError(f"IMAP ID failed: {last}")

    def _connect_imap(self) -> imaplib.IMAP4_SSL:
        host = self.profile.imap_host
        port = self.profile.imap_port
        imap = imaplib.IMAP4_SSL(host, port, timeout=self.timeout)
        typ, _ = imap.login(self.mail, self.app_password)
        if typ != "OK":
            try:
                imap.logout()
            except Exception:
                pass
            raise RuntimeError(f"IMAP login failed: {typ} ({host})")
        if self.profile.need_imap_id:
            try:
                self._imap_id(imap)
            except Exception as e:
                try:
                    imap.logout()
                except Exception:
                    pass
                raise RuntimeError(
                    f"IMAP ID (网易安全登录) 失败: {e}; host={host}"
                ) from e
        return imap

    def _smtp_login(self, smtp: smtplib.SMTP) -> None:
        smtp.login(self.mail, self.app_password)

    def _connect_smtp(self) -> smtplib.SMTP:
        """
        连接 SMTP。163 优先 465 SSL，失败再试 587 STARTTLS（部分网络封 465）。
        """
        context = ssl.create_default_context()
        host = self.profile.smtp_host
        attempts: list[tuple[str, int, str]] = []
        if self.profile.smtp_ssl:
            attempts.append(("ssl", self.profile.smtp_port, host))
            # 163 常见备选
            if self.profile.name == "163mail" and self.profile.smtp_port != 587:
                attempts.append(("starttls", 587, host))
        else:
            attempts.append(("starttls" if self.profile.smtp_starttls else "plain", self.profile.smtp_port, host))

        errors: list[str] = []
        for mode, port, h in attempts:
            try:
                if mode == "ssl":
                    smtp = smtplib.SMTP_SSL(h, port, timeout=self.timeout, context=context)
                    smtp.ehlo()
                else:
                    smtp = smtplib.SMTP(h, port, timeout=self.timeout)
                    smtp.ehlo()
                    if mode == "starttls":
                        smtp.starttls(context=context)
                        smtp.ehlo()
                self._smtp_login(smtp)
                return smtp
            except Exception as e:
                errors.append(f"{mode}:{h}:{port} -> {type(e).__name__}: {e}")
        raise RuntimeError("SMTP connect failed: " + " | ".join(errors))

    @staticmethod
    def _mailbox_arg(mailbox: str) -> str:
        # 163 垃圾箱是 `&V4NXPpCuTvY-`，空格/&/" 都必须加引号，否则 SELECT 解析炸
        if not mailbox:
            return mailbox
        if re.search(r'[\s"\\&]', mailbox) or mailbox != mailbox.encode("ascii", "ignore").decode("ascii"):
            escaped = mailbox.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return mailbox

    def describe_folders(self, *, refresh: bool = False) -> list[ImapFolder]:
        """LIST 并解析角色。结果按连接缓存，refresh=True 强制重拉。"""
        if self._folder_cache is not None and not refresh:
            return list(self._folder_cache)
        with self._connect_imap() as imap:
            typ, data = imap.list()
        folders: list[ImapFolder] = []
        if typ == "OK" and data:
            for item in data:
                parsed = parse_imap_list_line(item)
                if parsed:
                    folders.append(parsed)
        self._folder_cache = folders
        return list(folders)

    def resolve_mailbox(self, mailbox: str, folders: list[ImapFolder] | None = None) -> str:
        """
        把调用方给的逻辑名（Junk / 垃圾邮件 / INBOX）解析成 SELECT 用的真实箱名。
        对不上就原样返回，让 SELECT 自己报错。
        """
        want = (mailbox or "").strip()
        if not want:
            return want
        if want.upper() == "INBOX":
            return "INBOX"
        catalog = folders if folders is not None else self.describe_folders()
        want_decoded = decode_imap_utf7(want)
        want_role = classify_folder_role(want, want_decoded, ())
        for folder in catalog:
            if want == folder.raw or want_decoded == folder.name or want.lower() == folder.name.lower():
                return folder.raw
        if want_role:
            for folder in catalog:
                if folder.role == want_role and folder.selectable:
                    return folder.raw
        return want

    def probe_imap(self) -> MailProbeResult:
        try:
            with self._connect_imap() as imap:
                typ, data = imap.select("INBOX", readonly=True)
                if typ != "OK":
                    return MailProbeResult(False, f"select INBOX failed: {typ}")
                count = int(data[0]) if data and data[0] else 0
                return MailProbeResult(
                    True,
                    f"INBOX messages={count} via {self.profile.imap_host} ({self.profile.name})",
                )
        except Exception as e:
            return MailProbeResult(False, f"{type(e).__name__}: {e}")

    def probe_smtp(self) -> MailProbeResult:
        try:
            smtp = self._connect_smtp()
            try:
                smtp.quit()
            except Exception:
                try:
                    smtp.close()
                except Exception:
                    pass
            return MailProbeResult(
                True,
                f"SMTP auth OK via {self.profile.smtp_host}:{self.profile.smtp_port} ({self.profile.name})",
            )
        except Exception as e:
            return MailProbeResult(False, f"{type(e).__name__}: {e}")
    def list_folders(self) -> list[str]:
        """兼容旧接口：返回可 SELECT 的真实箱名（163 上是 modified UTF-7）。"""
        return [f.raw for f in self.describe_folders() if f.selectable]

    def _iter_fetch(self, msg_data: list) -> list[tuple[dict[str, Any], Message]]:
        parsed: list[tuple[dict[str, Any], Message]] = []
        for part in msg_data:
            if not isinstance(part, tuple) or len(part) < 2:
                continue
            meta_raw, raw_msg = part[0], part[1]
            if not isinstance(raw_msg, (bytes, bytearray)):
                continue
            meta = self.parser.parse_fetch_meta(
                meta_raw if isinstance(meta_raw, (bytes, bytearray)) else str(meta_raw)
            )
            msg = email.message_from_bytes(bytes(raw_msg))
            parsed.append((meta, msg))
        return parsed

    def list_recent(
        self,
        limit: int = 5,
        mailbox: str = "INBOX",
        include_body: bool = True,
        body_limit: int = 2000,
    ) -> list[dict[str, Any]]:
        box = self.resolve_mailbox(mailbox)
        with self._connect_imap() as imap:
            typ, data = imap.select(self._mailbox_arg(box), readonly=True)
            if typ != "OK":
                raise RuntimeError(f"select {mailbox} ({box}) failed")
            total = int(data[0]) if data and data[0] else 0
            if total <= 0:
                return []

            start = max(1, total - limit + 1)
            ids = ",".join(str(i) for i in range(start, total + 1))
            typ, msg_data = imap.fetch(
                ids, "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[])"
            )
            if typ != "OK" or not msg_data:
                return []

            items = [
                self.parser.to_dict(
                    mailbox=mailbox,
                    meta=meta,
                    msg=msg,
                    include_body=include_body,
                    body_limit=body_limit,
                    account=self.mail,
                )
                for meta, msg in self._iter_fetch(msg_data)
            ]
        return list(reversed(items))

    def fetch_since_uid(
        self,
        mailbox: str = "INBOX",
        since_uid: int = 0,
        limit: int = 200,
        include_body: bool = True,
        body_limit: int = 0,
        batch_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        增量拉取 UID > since_uid 的邮件。
        返回 (邮件字典列表按 UID 升序, 本次见到的最大 UID)。

        since_uid=0 表示全量。
        """
        start = max(0, int(since_uid)) + 1
        max_uid = max(0, int(since_uid))
        box = self.resolve_mailbox(mailbox)

        with self._connect_imap() as imap:
            typ, data = imap.select(self._mailbox_arg(box), readonly=True)
            if typ != "OK":
                detail = ""
                if data and data[0]:
                    detail = data[0].decode("utf-8", errors="replace") if isinstance(data[0], bytes) else str(data[0])
                raise RuntimeError(f"select {mailbox} ({box}) failed: {typ} {detail}".strip())

            typ, sdata = imap.uid("SEARCH", None, f"UID {start}:*")
            if typ != "OK" or not sdata:
                return [], max_uid

            raw = sdata[0]
            tokens = (
                raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
            ).split()

            # IMAP 的 `UID n:*` 在没有任何 UID >= n 时仍会返回最后一封，
            # 不过滤会导致每次同步都重复处理同一封邮件。
            uids = sorted({int(t) for t in tokens if t.isdigit()})
            uids = [u for u in uids if u >= start]
            if not uids:
                return [], max_uid

            if limit and limit > 0:
                uids = uids[-int(limit):]

            items: list[dict[str, Any]] = []
            step = max(1, int(batch_size))
            for i in range(0, len(uids), step):
                chunk = uids[i : i + step]
                typ, msg_data = imap.uid(
                    "FETCH",
                    ",".join(str(u) for u in chunk),
                    "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[])",
                )
                if typ != "OK" or not msg_data:
                    continue
                for meta, msg in self._iter_fetch(msg_data):
                    item = self.parser.to_dict(
                        mailbox=mailbox,
                        meta=meta,
                        msg=msg,
                        include_body=include_body,
                        body_limit=body_limit,
                        account=self.mail,
                    )
                    if item.get("uid", "").isdigit():
                        max_uid = max(max_uid, int(item["uid"]))
                    items.append(item)

        items.sort(key=lambda d: int(d["uid"]) if str(d.get("uid", "")).isdigit() else 0)
        return items, max_uid

    def get_by_uid(
        self,
        uid: str | int,
        mailbox: str = "INBOX",
        include_body: bool = True,
        body_limit: int = 0,
        search_all: bool = False,
    ) -> dict[str, Any]:
        """按 UID 获取单封邮件，返回标准字典。"""
        uid_s = str(uid).strip()
        if not uid_s.isdigit():
            raise ValueError(f"invalid uid: {uid}")

        catalog = self.describe_folders()
        if search_all:
            folders = [f.raw for f in catalog if f.selectable] or [self.resolve_mailbox(mailbox, catalog)]
        else:
            folders = [self.resolve_mailbox(mailbox, catalog)]

        with self._connect_imap() as imap:
            for box in folders:
                typ, _ = imap.select(self._mailbox_arg(box), readonly=True)
                if typ != "OK":
                    continue
                typ, msg_data = imap.uid(
                    "FETCH", uid_s, "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[])"
                )
                if typ != "OK" or not msg_data:
                    continue
                parsed = self._iter_fetch(msg_data)
                if not parsed:
                    continue
                meta, msg = parsed[0]
                if not meta.get("uid"):
                    meta["uid"] = uid_s
                # 入库/详情按逻辑名（Junk），不要把 163 的 UTF-7 箱名写回去
                label = mailbox
                if search_all:
                    hit = next((f for f in catalog if f.raw == box), None)
                    label = (hit.role or hit.name or box) if hit else box
                return self.parser.to_dict(
                    mailbox=label,
                    meta=meta,
                    msg=msg,
                    include_body=include_body,
                    body_limit=body_limit,
                    account=self.mail,
                )

        raise LookupError(f"uid={uid_s} not found in {folders}")

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.mail
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        smtp = self._connect_smtp()
        try:
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                try:
                    smtp.close()
                except Exception:
                    pass


def mail_client_from_endpoint(
    mail: str,
    password: str,
    provider: str = "apple",
    timeout: float = 30.0,
) -> ICloudMailClient:
    return ICloudMailClient(mail, password, timeout=timeout, provider=provider)


def mail_client_from_account(account: Any, timeout: float = 30.0) -> Any:
    """从 Account.resolve_inbox() 构建客户端。"""
    endpoint = account.resolve_inbox()
    if not endpoint or not endpoint.ready:
        raise ValueError(
            f"account mail credentials incomplete: {getattr(account, 'name', '?')}"
        )
    if endpoint.name == "outlook":
        from .outlook_graph import outlook_client_from_endpoint

        return outlook_client_from_endpoint(endpoint, timeout=timeout)
    return ICloudMailClient(
        endpoint.mail,
        endpoint.password,
        timeout=timeout,
        provider=endpoint.name,
    )


# 对外别名
MailClient = ICloudMailClient


class MailService:
    """基于 accounts 配置的邮件服务：主邮箱 + uid -> 字典。"""

    def __init__(self, base_dir: Path | None = None) -> None:
        from .config import load_settings

        self.settings = load_settings()
        if base_dir is not None:
            object.__setattr__(self.settings, "base_dir", base_dir) if False else None
            self.base_dir = base_dir
        else:
            self.base_dir = self.settings.base_dir

    def _load_account(self, mail: str):
        from .accounts import find_account, load_all_accounts

        _, accounts = load_all_accounts(self.settings.accounts_files, self.base_dir)
        acc = find_account(accounts, mail)
        if not acc:
            names = ", ".join(a.name for a in accounts) or "(none)"
            raise LookupError(f"account not found: {mail}; available: {names}")
        if not acc.mail_ready:
            raise ValueError(f"account mail credentials incomplete: {acc.name}")
        return acc

    def get_client(self, mail: str) -> Any:
        acc = self._load_account(mail)
        return mail_client_from_account(acc)

    def get_mail(
        self,
        mail: str,
        uid: str | int,
        mailbox: str = "INBOX",
        include_body: bool = True,
        body_limit: int = 0,
        search_all: bool = False,
    ) -> dict[str, Any]:
        """
        入口：主邮箱名 + uid
        返回：标准邮件字典（date 发送时间 / internaldate 到达时间 / 正文等）
        """
        client = self.get_client(mail)
        return client.get_by_uid(
            uid=uid,
            mailbox=mailbox,
            include_body=include_body,
            body_limit=body_limit,
            search_all=search_all,
        )


def get_mail_by_uid(
    mail: str,
    uid: str | int,
    mailbox: str = "INBOX",
    include_body: bool = True,
    body_limit: int = 0,
    search_all: bool = False,
) -> dict[str, Any]:
    """便捷函数：主邮箱 + uid -> 邮件字典。"""
    return MailService().get_mail(
        mail=mail,
        uid=uid,
        mailbox=mailbox,
        include_body=include_body,
        body_limit=body_limit,
        search_all=search_all,
    )
