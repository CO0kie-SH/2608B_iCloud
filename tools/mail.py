from __future__ import annotations

import email
import imaplib
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage, Message
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


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


MAIL_SERVER_PROFILES: dict[str, MailServerProfile] = {
    "apple": MailServerProfile(
        name="apple",
        imap_host="imap.mail.me.com",
        imap_port=993,
        smtp_host="smtp.mail.me.com",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
        need_imap_id=False,
        folders=DEFAULT_FOLDERS,
    ),
    "163mail": MailServerProfile(
        name="163mail",
        imap_host="imap.163.com",
        imap_port=993,
        smtp_host="smtp.163.com",
        smtp_port=465,
        smtp_ssl=True,
        smtp_starttls=False,
        need_imap_id=True,
        folders=DEFAULT_FOLDERS_163,
    ),
}
# 别名
MAIL_SERVER_PROFILES["icloud"] = MAIL_SERVER_PROFILES["apple"]
MAIL_SERVER_PROFILES["163"] = MAIL_SERVER_PROFILES["163mail"]


def resolve_mail_profile(provider: str | None) -> MailServerProfile:
    """provider 名 → 服务器配置；未知则按 apple。"""
    key = (provider or "apple").strip().lower()
    if key in MAIL_SERVER_PROFILES:
        return MAIL_SERVER_PROFILES[key]
    # inbox 兜底：历史逻辑多用 apple 密码
    if key == "inbox":
        return MAIL_SERVER_PROFILES["apple"]
    return MAIL_SERVER_PROFILES["apple"]


@dataclass
class MailProbeResult:
    ok: bool
    detail: str


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

        body_text_out = text_body
        body_html_out = html_body
        if body_limit and body_limit > 0:
            body_text_out = text_body[:body_limit]
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
            "body_text_len": len(text_body),
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
        return f'"{mailbox}"' if " " in mailbox else mailbox

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
        with self._connect_imap() as imap:
            typ, data = imap.list()
        folders: list[str] = []
        if typ != "OK" or not data:
            return folders
        for item in data:
            line = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
            m = re.search(r' "/" "?(.*?)"?$', line)
            if m:
                folders.append(m.group(1))
            else:
                parts = line.rsplit(" ", 1)
                if len(parts) == 2:
                    folders.append(parts[1].strip('"'))
        return folders

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
        with self._connect_imap() as imap:
            typ, data = imap.select(self._mailbox_arg(mailbox), readonly=True)
            if typ != "OK":
                raise RuntimeError(f"select {mailbox} failed")
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

        folders = self.list_folders() if search_all else [mailbox]
        if not folders:
            folders = [mailbox]

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
                return self.parser.to_dict(
                    mailbox=box,
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


def mail_client_from_account(account: Any, timeout: float = 30.0) -> ICloudMailClient:
    """从 Account.resolve_inbox() 构建客户端。"""
    endpoint = account.resolve_inbox()
    if not endpoint or not endpoint.ready:
        raise ValueError(
            f"account mail credentials incomplete: {getattr(account, 'name', '?')}"
        )
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

    def get_client(self, mail: str) -> ICloudMailClient:
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
