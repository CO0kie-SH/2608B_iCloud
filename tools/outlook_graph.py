from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from .mail import MailMessageParser, MailProbeResult, MailServerProfile


TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
GRAPH_SCOPE = "https://graph.microsoft.com/Mail.Read"
GRAPH_FOLDERS = {"INBOX": "inbox", "Junk": "junkemail"}
GRAPH_FIELDS = (
    "id,subject,from,sender,toRecipients,ccRecipients,replyTo,receivedDateTime,"
    "sentDateTime,isRead,hasAttachments,importance,internetMessageId,body,"
    "bodyPreview,internetMessageHeaders"
)
_DELIVERED_HEADER_NAMES = (
    "Delivered-To",
    "X-Original-To",
    "Envelope-To",
    "X-Envelope-To",
    "X-Forwarded-To",
    "X-Apple-Relay-To",
)


class OutlookGraphError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutlookAccount:
    email: str
    password: str
    client_id: str
    refresh_token: str
    source_file: Path


def _client_id_like(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            value or "",
        )
    )


def parse_outlook_account_file(path: str | Path) -> OutlookAccount:
    source = Path(path).resolve()
    try:
        line = source.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise OutlookGraphError(f"Outlook 授权文件读取失败: {source.name}: {exc}") from exc

    parts = line.split("----", 3)
    if len(parts) != 4:
        raise OutlookGraphError(
            f"Outlook 授权文件格式错误: {source.name}（需要 4 列，实际 {len(parts)} 列）"
        )
    email, password, col3, col4 = parts
    if _client_id_like(col3.strip()):
        client_id, refresh_token = col3, col4
    elif _client_id_like(col4.strip()):
        refresh_token, client_id = col3, col4
    else:
        client_id, refresh_token = DEFAULT_CLIENT_ID, col4

    email = email.strip().lower()
    client_id = client_id.strip() or DEFAULT_CLIENT_ID
    refresh_token = refresh_token.strip()
    if not email or "@" not in email:
        raise OutlookGraphError(f"Outlook 授权文件邮箱格式错误: {source.name}")
    if not refresh_token:
        raise OutlookGraphError(f"Outlook 授权文件缺少 refresh_token: {source.name}")
    return OutlookAccount(
        email=email,
        password=password,
        client_id=client_id,
        refresh_token=refresh_token,
        source_file=source,
    )


def _email_address(value: Any) -> tuple[str, str]:
    raw = value if isinstance(value, dict) else {}
    item = raw.get("emailAddress") if isinstance(raw.get("emailAddress"), dict) else raw
    name = str(item.get("name") or "").strip()
    addr = str(item.get("address") or "").strip().lower()
    return name, addr


def _address_text(value: Any) -> str:
    name, addr = _email_address(value)
    if not addr:
        return ""
    return f"{name} <{addr}>" if name else addr


def _address_list(values: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values if isinstance(values, list) else []:
        name, addr = _email_address(value)
        if not addr or addr in seen:
            continue
        seen.add(addr)
        out.append({"name": name, "addr": addr})
    return out


def _join_addresses(values: Any) -> str:
    return ", ".join(
        f"{item['name']} <{item['addr']}>" if item["name"] else item["addr"]
        for item in _address_list(values)
    )


def _header_map(message: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for item in message.get("internetMessageHeaders") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if name and value:
            result.setdefault(name, []).append(value)
    return result


def _first_header(headers: dict[str, list[str]], name: str) -> str:
    values = headers.get(name.lower()) or []
    return values[0] if values else ""


def _envelope_from(headers: dict[str, list[str]]) -> str:
    text = " ".join(
        (headers.get("received-spf") or []) + (headers.get("authentication-results") or [])
    )
    hit = re.search(
        r"(?:envelope-from|smtp\.mail)\s*=\s*<?([^\s>;]+)>?",
        text,
        flags=re.I,
    )
    return (hit.group(1) if hit else "").strip().strip("<>").lower()


def normalize_graph_message(
    message: dict[str, Any],
    *,
    mailbox: str,
    account: str,
    include_body: bool = True,
    body_limit: int = 0,
) -> dict[str, Any]:
    headers = _header_map(message)
    subject = str(message.get("subject") or "")
    from_text = _address_text(message.get("from")) or _first_header(headers, "From")
    from_name_raw, from_addr_raw = _email_address(message.get("from"))
    if not from_addr_raw:
        from_name_raw, from_addr_raw = MailMessageParser.addr_pair(from_text)
    sender_text = _address_text(message.get("sender")) or _first_header(headers, "Sender")
    sender_name_raw, sender_addr_raw = MailMessageParser.addr_pair(sender_text)
    return_path_text = _first_header(headers, "Return-Path")
    _, return_path_raw = MailMessageParser.addr_pair(return_path_text)

    from_addr = MailMessageParser.real_from_addr(from_addr_raw)
    sender_addr = MailMessageParser.real_from_addr(sender_addr_raw)
    return_path = MailMessageParser.real_from_addr(return_path_raw)
    relay_addr = sender_addr or return_path
    from_key = MailMessageParser.domain_key(from_addr)
    relay_key = MailMessageParser.domain_key(relay_addr)
    is_relayed = bool(relay_addr and from_key and relay_key and relay_key != from_key)

    to_addrs = _address_list(message.get("toRecipients"))
    to_text = _join_addresses(message.get("toRecipients")) or _first_header(headers, "To")
    cc_text = _join_addresses(message.get("ccRecipients")) or _first_header(headers, "Cc")
    reply_to = _join_addresses(message.get("replyTo")) or _first_header(headers, "Reply-To")

    candidates: list[str] = []
    seen: set[str] = set()
    delivered_to = ""
    for header_name in _DELIVERED_HEADER_NAMES:
        for raw in headers.get(header_name.lower()) or []:
            for item in MailMessageParser.addr_list(raw):
                addr = item["addr"]
                if not delivered_to:
                    delivered_to = addr
                if addr not in seen:
                    seen.add(addr)
                    candidates.append(addr)
    for item in to_addrs:
        if item["addr"] not in seen:
            seen.add(item["addr"])
            candidates.append(item["addr"])

    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    body_kind = str(body.get("contentType") or "").strip().lower()
    body_content = str(body.get("content") or "")
    if body_kind == "html":
        html_body = body_content
        text_body = MailMessageParser.html_to_text(body_content)
    else:
        text_body = body_content or str(message.get("bodyPreview") or "")
        html_body = ""
    plain = MailMessageParser.plain_content(text_body, html_body)
    mail_type = MailMessageParser.classify_type(subject, from_text, plain)
    code = MailMessageParser.extract_code(subject, plain) if mail_type == "code" else ""
    summary = MailMessageParser.make_summary(
        mail_type=mail_type,
        subject=subject,
        from_addr=from_text,
        to_addr=to_text,
        content=plain,
        code=code,
    )

    body_text_out = plain
    body_html_out = html_body
    if body_limit and body_limit > 0:
        body_text_out = body_text_out[:body_limit]
        body_html_out = body_html_out[:body_limit]

    received_at = str(message.get("receivedDateTime") or "")
    sent_at = str(message.get("sentDateTime") or "")
    date_header = _first_header(headers, "Date") or sent_at
    has_attachments = bool(message.get("hasAttachments"))
    attachments = (
        [{"filename": "(Graph attachment)", "content_type": "", "size": 0, "disposition": ""}]
        if has_attachments
        else []
    )
    received_spf = "\n".join(headers.get("received-spf") or [])
    return {
        "account": account,
        "mailbox": mailbox,
        "seq": None,
        "uid": str(message.get("id") or ""),
        "flags": ["\\Seen"] if bool(message.get("isRead")) else [],
        "size": None,
        "date": date_header,
        "date_parsed": sent_at or received_at,
        "internaldate": received_at,
        "from": from_text,
        "to": to_text,
        "cc": cc_text,
        "bcc": "",
        "reply_to": reply_to,
        "from_name": from_name_raw,
        "from_addr": from_addr,
        "sender_name": sender_name_raw,
        "sender_addr": sender_addr,
        "return_path_addr": return_path,
        "received_spf": received_spf,
        "envelope_from": _envelope_from(headers),
        "relay_addr": relay_addr if is_relayed else "",
        "is_relayed": is_relayed,
        "relay_label": f"via {relay_key}" if is_relayed else "",
        "to_addrs": to_addrs,
        "delivered_to": delivered_to,
        "delivered_to_addrs": candidates,
        "alias_candidates": candidates,
        "subject": subject,
        "message_id": str(message.get("internetMessageId") or ""),
        "in_reply_to": _first_header(headers, "In-Reply-To"),
        "references": _first_header(headers, "References"),
        "content_type": "text/html" if html_body else "text/plain",
        "type": mail_type,
        "summary": summary,
        "code": code,
        "attachments": attachments,
        "body_text": body_text_out if include_body else "",
        "body_html": body_html_out if include_body else "",
        "body_text_len": len(plain),
        "body_html_len": len(html_body),
    }


class OutlookGraphClient:
    provider = "outlook"
    supports_sync_cursor = True

    def __init__(
        self,
        token_file: str | Path,
        *,
        expected_mail: str = "",
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        self.account = parse_outlook_account_file(token_file)
        if expected_mail and self.account.email.lower() != expected_mail.strip().lower():
            raise OutlookGraphError(
                f"Outlook 授权邮箱与 inbox.mail 不一致: {self.account.email}"
            )
        self.mail = self.account.email
        self.timeout = float(timeout)
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False
            self.session.proxies = {"http": None, "https": None}
        self.profile = MailServerProfile(
            name="outlook",
            imap_host=GRAPH_BASE,
            smtp_host="",
            folders=("INBOX", "Junk"),
        )
        self._access_token = ""
        self._access_expires_at = 0.0

    @staticmethod
    def _error_detail(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return (response.text or "").strip()[:180]
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            return f"{error.get('code') or 'error'}: {str(error.get('message') or '')[:140]}"
        return str(error or payload)[:180]

    def refresh_access_token(self) -> str:
        last_error = ""
        for attempt in range(3):
            try:
                response = self.session.post(
                    TOKEN_URL,
                    data={
                        "client_id": self.account.client_id,
                        "grant_type": "refresh_token",
                        "refresh_token": self.account.refresh_token,
                        "scope": GRAPH_SCOPE,
                    },
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < 2:
                    time.sleep(1.0)
                    continue
                raise OutlookGraphError(f"Outlook token 网络请求失败: {last_error}") from exc
            if response.status_code != 200:
                raise OutlookGraphError(
                    f"Outlook token 刷新失败 HTTP {response.status_code}: "
                    f"{self._error_detail(response)}"
                )
            payload = response.json()
            token = str(payload.get("access_token") or "")
            if not token:
                raise OutlookGraphError("Outlook token 响应缺少 access_token")
            self._access_token = token
            try:
                expires_in = max(60, int(payload.get("expires_in") or 3600))
            except (TypeError, ValueError):
                expires_in = 3600
            self._access_expires_at = time.time() + expires_in - 60
            return token
        raise OutlookGraphError(f"Outlook token 网络请求失败: {last_error}")

    def _token(self, *, force: bool = False) -> str:
        if force or not self._access_token or time.time() >= self._access_expires_at:
            return self.refresh_access_token()
        return self._access_token

    def _graph_get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        *,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        if not url.startswith(GRAPH_BASE + "/"):
            raise OutlookGraphError("Graph 同步游标地址无效")
        response = self.session.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Prefer": 'outlook.body-content-type="html"',
            },
            timeout=self.timeout,
        )
        if response.status_code == 401 and retry_auth:
            self._token(force=True)
            return self._graph_get(url, params, retry_auth=False)
        if response.status_code != 200:
            raise OutlookGraphError(
                f"Graph GET 失败 HTTP {response.status_code}: {self._error_detail(response)}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise OutlookGraphError("Graph 响应格式错误")
        return payload

    @staticmethod
    def _folder(mailbox: str) -> str:
        key = (mailbox or "INBOX").strip()
        if key in GRAPH_FOLDERS:
            return GRAPH_FOLDERS[key]
        lowered = key.lower()
        if lowered in GRAPH_FOLDERS.values():
            return lowered
        return key

    def fetch_since_cursor(
        self,
        *,
        mailbox: str,
        cursor: str = "",
        limit: int = 200,
        include_body: bool = True,
        body_limit: int = 0,
    ) -> tuple[list[dict[str, Any]], str]:
        folder = urllib.parse.quote(self._folder(mailbox), safe="")
        next_url = cursor or f"{GRAPH_BASE}/me/mailFolders/{folder}/messages/delta"
        next_params: dict[str, str] | None = None
        if not cursor:
            next_params = {"$select": GRAPH_FIELDS, "$top": str(max(1, min(limit, 50)))}
        items: list[dict[str, Any]] = []
        final_cursor = cursor
        while next_url:
            payload = self._graph_get(next_url, next_params)
            for raw in payload.get("value") or []:
                if not isinstance(raw, dict) or raw.get("@removed"):
                    continue
                item = normalize_graph_message(
                    raw,
                    mailbox=mailbox,
                    account=self.mail,
                    include_body=include_body,
                    body_limit=body_limit,
                )
                if item["uid"]:
                    items.append(item)
            next_link = str(payload.get("@odata.nextLink") or "")
            delta_link = str(payload.get("@odata.deltaLink") or "")
            final_cursor = delta_link or next_link or final_cursor
            if len(items) >= max(1, limit) or not next_link:
                break
            next_url = next_link
            next_params = None
        return items, final_cursor

    def list_recent(
        self,
        limit: int = 10,
        mailbox: str = "INBOX",
        include_body: bool = True,
        body_limit: int = 2000,
    ) -> list[dict[str, Any]]:
        folder = urllib.parse.quote(self._folder(mailbox), safe="")
        url = f"{GRAPH_BASE}/me/mailFolders/{folder}/messages"
        payload = self._graph_get(
            url,
            {
                "$select": GRAPH_FIELDS,
                "$top": str(max(1, min(int(limit or 10), 100))),
                "$orderby": "receivedDateTime desc",
            },
        )
        return [
            normalize_graph_message(
                raw,
                mailbox=mailbox,
                account=self.mail,
                include_body=include_body,
                body_limit=body_limit,
            )
            for raw in payload.get("value") or []
            if isinstance(raw, dict) and raw.get("id")
        ]

    def get_by_uid(
        self,
        uid: str | int,
        mailbox: str = "INBOX",
        include_body: bool = True,
        body_limit: int = 0,
        search_all: bool = False,
    ) -> dict[str, Any]:
        del search_all
        message_id = urllib.parse.quote(str(uid), safe="")
        payload = self._graph_get(
            f"{GRAPH_BASE}/me/messages/{message_id}",
            {"$select": GRAPH_FIELDS},
        )
        return normalize_graph_message(
            payload,
            mailbox=mailbox,
            account=self.mail,
            include_body=include_body,
            body_limit=body_limit,
        )

    def probe_imap(self) -> MailProbeResult:
        try:
            self._graph_get(f"{GRAPH_BASE}/me/mailFolders/inbox", {"$select": "id"})
            return MailProbeResult(True, "Microsoft Graph Mail.Read OK")
        except Exception as exc:
            return MailProbeResult(False, f"{type(exc).__name__}: {exc}")

    def probe_smtp(self) -> MailProbeResult:
        return MailProbeResult(False, "Graph 授权范围为 Mail.Read（仅收件）")

    def send(self, *, to: str, subject: str, body: str) -> None:
        del to, subject, body
        raise OutlookGraphError("Graph 授权范围为 Mail.Read（仅收件）")


def outlook_client_from_endpoint(endpoint: Any, timeout: float = 30.0) -> OutlookGraphClient:
    token_file = str((getattr(endpoint, "extra", {}) or {}).get("token_file") or "")
    if not token_file:
        raise ValueError("Outlook token_file missing")
    return OutlookGraphClient(
        token_file,
        expected_mail=str(getattr(endpoint, "mail", "") or ""),
        timeout=timeout,
    )


__all__ = [
    "GRAPH_BASE",
    "GRAPH_FIELDS",
    "OutlookAccount",
    "OutlookGraphClient",
    "OutlookGraphError",
    "normalize_graph_message",
    "outlook_client_from_endpoint",
    "parse_outlook_account_file",
]
