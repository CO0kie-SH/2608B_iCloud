from __future__ import annotations

"""mail.com webmail client and message normalization helpers.

mail.com uses a browser login to obtain a navigator sid, then exposes the
mailbox through a short-lived OAuth bearer token. Credentials are read from
``accounts/mail.com*.txt`` and are never included in API responses.
"""

import base64
import html
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests

from .config import env_str
from .mail import MailMessageParser


LOGIN_PAGE_URL = "https://www.mail.com/"
LOGIN_URL = "https://login.mail.com/login"
TOKEN_URL = "https://oauthbridge.navigator-lxa.mail.com/navigator/oauth2/token"
MAIL_LIST_URL = "https://maillist.mail.com/Mailbox/Mail"
MAIL_BODY_URL = "https://mailcom.mailbody-ui.de/Mailbox/Mail/{mail_id}/Body"
OAUTH_CLIENT_ID = "mailcom_mailsidebar_passport_live"
# This is a public web-client credential, not an account password.
OAUTH_PUBLIC_SECRET = "*******"


@dataclass(frozen=True)
class MailComAccount:
    email: str
    password: str
    source: str = ""
    proxy: str = ""


@dataclass(frozen=True)
class MailComHeader:
    id: str
    subject: str
    sender: str
    timestamp: Any = None


def parse_account_row(row: str, *, source: str = "") -> MailComAccount | None:
    text = str(row or "").strip().strip("\ufeff")
    if not text or text.startswith("#") or text.startswith("//"):
        return None
    email, separator, remainder = text.partition("----")
    password, proxy_separator, proxy = remainder.partition("----")
    email = email.strip().lower()
    password = password.strip()
    if not separator or "@" not in email or not password:
        return None
    return MailComAccount(
        email=email,
        password=password,
        source=source,
        proxy=proxy.strip() if proxy_separator else "",
    )


def _account_files(base_dir: Path, files: Iterable[str | Path] | None = None) -> list[Path]:
    if files is None:
        configured = env_str("MAILCOM_ACCOUNTS_FILE", "")
        if configured:
            files = [part.strip() for part in re.split(r"[,;]+", configured) if part.strip()]
        else:
            files = sorted((base_dir / "accounts").glob("mail.com*.txt"))

    result: list[Path] = []
    seen: set[Path] = set()
    for raw in files:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = base_dir / path
        path = path.resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        result.append(path)
    return result


def load_mailcom_accounts(
    base_dir: Path,
    files: Iterable[str | Path] | None = None,
) -> list[MailComAccount]:
    accounts: list[MailComAccount] = []
    seen: set[str] = set()
    for path in _account_files(base_dir, files):
        try:
            rows = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for row in rows:
            account = parse_account_row(row, source=str(path))
            if not account or account.email in seen:
                continue
            seen.add(account.email)
            accounts.append(account)
    return accounts


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _body_parts(value: str) -> tuple[str, str]:
    raw = str(value or "")
    if re.search(r"<\s*[a-z][^>]*>", raw, re.I):
        return MailMessageParser.html_to_text(raw), raw
    return raw.strip(), ""


def _date_fields(value: Any) -> tuple[str, str]:
    if value is None or value == "":
        return "", ""
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e12:
            number /= 1000
        try:
            return str(value), datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return str(value), ""
    text = str(value).strip()
    try:
        return text, parsedate_to_datetime(text).isoformat()
    except (TypeError, ValueError, OverflowError):
        return text, text if "T" in text else ""


def normalize_message(
    account: MailComAccount,
    header: MailComHeader,
    body: str = "",
) -> dict[str, Any]:
    subject = str(header.subject or "")
    sender = str(header.sender or "")
    sender_name, sender_addr = parseaddr(sender)
    if not sender_addr and "@" in sender:
        sender_addr = sender.strip()
    text_body, html_body = _body_parts(body)
    date_header, date_utc = _date_fields(header.timestamp)
    code = MailMessageParser.extract_code(subject, text_body)
    mail_type = MailMessageParser.classify_type(subject, sender_addr, text_body)
    if code:
        mail_type = "code"
    summary = MailMessageParser.make_summary(
        mail_type=mail_type,
        subject=subject,
        from_addr=sender_addr,
        to_addr=account.email,
        content=text_body,
        code=code,
    )
    return {
        "id": header.id,
        "account": account.email,
        "subject": subject,
        "sender": sender,
        "from_name": sender_name or sender_addr,
        "from_addr": sender_addr.lower(),
        "date_header": date_header,
        "date_utc": date_utc,
        "timestamp": header.timestamp,
        "mail_type": mail_type,
        "code": code,
        "summary": summary,
        "body_text": text_body,
        "body_html": html_body,
        "body_text_len": len(text_body),
        "body_html_len": len(html_body),
        "has_body": bool(text_body or html_body),
    }


class MailComClient:
    def __init__(
        self,
        account: MailComAccount,
        *,
        request_timeout: int = 30,
        verify_tls: bool = True,
        proxy: str = "",
    ) -> None:
        self.account = account
        self.request_timeout = max(1, int(request_timeout))
        self.session = requests.Session()
        self.session.verify = verify_tls
        proxy_url = (
            account.proxy
            or proxy
            or env_str("MAILCOM_PROXY", "")
            or env_str("CAMOUFOX_PROXY", "")
            or env_str("HTTPS_PROXY", "")
        ).strip()
        if proxy_url.lower() in {"none", "off", "direct"}:
            proxy_url = ""
        if proxy_url:
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})
        self.session.headers.update(
            {
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "accept-language": "en-US,en;q=0.9",
            }
        )
        self.sid = ""
        self.access_token = ""
        self.token_expires_at = 0.0

    @staticmethod
    def _hidden_input(page: str, name: str) -> str:
        patterns = (
            rf'<input[^>]+name=["\']{re.escape(name)}["\'][^>]+value=["\']([^"\']*)',
            rf'<input[^>]+value=["\']([^"\']*)["\'][^>]+name=["\']{re.escape(name)}["\']',
        )
        for pattern in patterns:
            match = re.search(pattern, page or "", re.I)
            if match:
                return html.unescape(match.group(1))
        return ""

    @staticmethod
    def _feature_redirect(page: str, base_url: str) -> str:
        match = re.search(r'"redirectUrl"\s*:\s*"([^"]+)"', page or "")
        if not match:
            raise RuntimeError("mail.com login response has no redirect URL")
        path = match.group(1).replace(r"\/", "/").replace(r"\u0026", "&")
        return urljoin(base_url, path)

    def login(self) -> None:
        landing = self.session.get(LOGIN_PAGE_URL, timeout=self.request_timeout)
        landing.raise_for_status()
        statistics = self._hidden_input(landing.text, "statistics")
        form = {
            "ibaInfo": "abd=false",
            "service": "mailint",
            "uasServiceID": "mc_starter_mailcom",
            "successURL": "https://$(clientName)-$(dataCenter).mail.com/login",
            "loginFailedURL": "https://www.mail.com/logout?ls=wd",
            "loginErrorURL": "https://www.mail.com/logout?ls=te",
            "edition": "US",
            "lang": "en",
            "usertype": "standard",
            "username": self.account.email,
            "password": self.account.password,
        }
        if statistics:
            form["statistics"] = statistics
        response = self.session.post(
            LOGIN_URL,
            data=form,
            headers={"origin": "https://www.mail.com", "referer": LOGIN_PAGE_URL},
            allow_redirects=True,
            timeout=self.request_timeout,
        )
        redirect_urls = [response.url, *(hop.url for hop in response.history)]
        if response.status_code >= 400 or any("logout?ls=" in url for url in redirect_urls):
            raise RuntimeError(f"mail.com login rejected (HTTP {response.status_code})")
        halo_url = self._feature_redirect(response.text, response.url)
        separator = "&" if "?" in halo_url else "?"
        halo = self.session.get(
            halo_url + separator + urlencode({"tz": 0}),
            allow_redirects=True,
            timeout=self.request_timeout,
        )
        sid = str(parse_qs(urlparse(halo.url).query).get("sid", [""])[0]).strip()
        if not sid:
            for hop in reversed(halo.history):
                location = hop.headers.get("location", "")
                sid = str(parse_qs(urlparse(urljoin(hop.url, location)).query).get("sid", [""])[0]).strip()
                if sid:
                    break
        if not sid:
            raise RuntimeError("mail.com login succeeded but navigator sid was not returned")
        self.sid = sid
        self.access_token = ""
        self.token_expires_at = 0.0

    def token(self, *, allow_relogin: bool = True) -> str:
        if self.access_token and time.time() < self.token_expires_at - 30:
            return self.access_token
        if not self.sid:
            self.login()
        basic = base64.b64encode(f"{OAUTH_CLIENT_ID}:{OAUTH_PUBLIC_SECRET}".encode("ascii")).decode("ascii")
        response = self.session.post(
            TOKEN_URL,
            params={"sid": self.sid},
            data={"grant_type": "urn:mam:oauth:grant-type:spa", "scope": "mail_mailbox_r"},
            headers={
                "authorization": f"Basic {basic}",
                "x-ui-app": "mailcom.webmailer.mail-sidebar/4.3.3",
                "origin": "https://webmailer.mail.com",
                "referer": "https://webmailer.mail.com/",
                "content-type": "application/x-www-form-urlencoded",
            },
            timeout=self.request_timeout,
        )
        if response.status_code in (401, 403) and allow_relogin:
            self.login()
            return self.token(allow_relogin=False)
        if response.status_code != 200:
            raise RuntimeError(f"mail.com token request failed: HTTP {response.status_code}")
        payload = response.json() or {}
        self.access_token = str(payload.get("access_token") or "").strip()
        if not self.access_token:
            raise RuntimeError("mail.com token response has no access_token")
        self.token_expires_at = time.time() + int(payload.get("expires_in") or 300)
        return self.access_token

    def _headers(self, **headers: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.token()}",
            "origin": "https://webmailer.mail.com",
            "referer": "https://webmailer.mail.com/",
            **headers,
        }

    def messages(self, limit: int = 50) -> list[MailComHeader]:
        amount = max(1, min(int(limit or 50), 100))
        response = self.session.post(
            MAIL_LIST_URL,
            params={"folderTypeOrId": "INBOX", "offset": 0, "amount": amount, "orderBy": "INTERNALDATE DESC"},
            json={
                "aditionContext": {"brand": "mailcom", "category": "mail", "section": "3c/folder"},
                "deviceContext": {"app": {"name": "browser"}, "deviceclass": "b"},
                "adBlocker": True,
                "mailboxContext": {"currentPage": 1, "visibleMessages": amount},
            },
            headers=self._headers(
                **{
                    "x-ui-app": "mailcom.webmailer.mail-list/6.6.3",
                    "content-type": "application/vnd.1and1.mms.inboxadrequest-v1+json; charset=utf-8",
                    "accept": "application/vnd.1and1.mms.unified-maillist-v1+json",
                }
            ),
            timeout=self.request_timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(f"mail.com mail list failed: HTTP {response.status_code}")
        result: list[MailComHeader] = []
        for element in (response.json() or {}).get("mailListElements") or []:
            if element.get("type") != "mail":
                continue
            raw = dict(element.get("rawData") or {})
            attribute = dict(raw.get("attribute") or {})
            header = dict(raw.get("mailHeader") or {})
            mail_id = str(attribute.get("mailIdentifier") or "").strip()
            if mail_id:
                result.append(
                    MailComHeader(
                        id=mail_id,
                        subject=str(header.get("subject") or ""),
                        sender=str(header.get("from") or ""),
                        timestamp=header.get("date"),
                    )
                )
        return result

    def body(self, mail_id: str) -> str:
        response = self.session.get(
            MAIL_BODY_URL.format(mail_id=mail_id),
            headers=self._headers(
                **{
                    "x-ui-app": "mailcom.webmailer.mail-detail/7.40.1",
                    "accept": "text/vnd.ui.secure-v3+html, text/html;q=0.9, text/plain;q=0.8",
                }
            ),
            timeout=self.request_timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(f"mail.com mail body failed: HTTP {response.status_code}")
        return response.text


def message_payload(
    client: MailComClient,
    account: MailComAccount,
    header: MailComHeader,
    *,
    include_body: bool = True,
) -> dict[str, Any]:
    body = client.body(header.id) if include_body else ""
    return normalize_message(account, header, body)


__all__ = [
    "MailComAccount",
    "MailComClient",
    "MailComHeader",
    "load_mailcom_accounts",
    "message_payload",
    "normalize_message",
    "parse_account_row",
]
