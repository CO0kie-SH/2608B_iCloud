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
            "from": cls.header(msg, "From"),
            "to": cls.header(msg, "To"),
            "cc": cls.header(msg, "Cc"),
            "bcc": cls.header(msg, "Bcc"),
            "reply_to": cls.header(msg, "Reply-To"),
            "subject": cls.header(msg, "Subject"),
            "message_id": cls.header(msg, "Message-ID"),
            "in_reply_to": cls.header(msg, "In-Reply-To"),
            "references": cls.header(msg, "References"),
            "content_type": msg.get_content_type() or "",
            "attachments": attachments,
            "body_text": body_text_out if include_body else "",
            "body_html": body_html_out if include_body else "",
            "body_text_len": len(text_body),
            "body_html_len": len(html_body),
        }


class ICloudMailClient:
    """iCloud 邮件客户端（IMAP 收信 / SMTP 发信）。"""

    def __init__(self, mail: str, app_password: str, timeout: float = 30.0) -> None:
        if not mail or not app_password:
            raise ValueError("mail 与 app_password 不能为空")
        self.mail = mail
        self.app_password = app_password.replace(" ", "")
        self.timeout = timeout
        self.parser = MailMessageParser()

    def _connect_imap(self) -> imaplib.IMAP4_SSL:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=self.timeout)
        typ, _ = imap.login(self.mail, self.app_password)
        if typ != "OK":
            imap.logout()
            raise RuntimeError(f"IMAP login failed: {typ}")
        return imap

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
                return MailProbeResult(True, f"INBOX messages={count}")
        except Exception as e:
            return MailProbeResult(False, f"{type(e).__name__}: {e}")

    def probe_smtp(self) -> MailProbeResult:
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=self.timeout) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(self.mail, self.app_password)
            return MailProbeResult(True, "SMTP auth OK")
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

        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=self.timeout) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(self.mail, self.app_password)
            smtp.send_message(msg)


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
        return ICloudMailClient(acc.mail, acc.app_password)

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
