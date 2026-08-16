from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from email.utils import getaddresses
from typing import Any, Iterable, Mapping


_EMAIL_RE = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+", re.I)


@dataclass(frozen=True)
class MailAliasContext:
    account: str
    parent_mail: str
    inbox_mail: str
    provider: str


@dataclass(frozen=True)
class MailAliasMatch:
    address: str = ""
    source: str = ""
    provider: str = ""

    @property
    def matched(self) -> bool:
        return bool(self.address)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["matched"] = self.matched
        return result


class MailAliasExtractor:
    """按收件 provider 从邮件元数据中抽取实际注册别名。"""

    def __init__(self, accounts: Iterable[Any] = ()) -> None:
        self._contexts: dict[str, MailAliasContext] = {}
        for account in accounts:
            self._add_account(account)

    @staticmethod
    def normalize_address(value: str) -> str:
        return (value or "").strip().strip("<>;,.").lower()

    @classmethod
    def addresses(cls, value: Any) -> list[str]:
        """兼容标准地址头及 Graph 的 `邮箱 <同一邮箱>` 显示格式。"""
        text = str(value or "")
        result: list[str] = []
        seen: set[str] = set()

        for _, raw in getaddresses([text]):
            address = cls.normalize_address(raw)
            if "@" in address and address not in seen:
                seen.add(address)
                result.append(address)

        # 显示名本身含 @ 时 email.utils 会丢弃整项，正则作为结构化兜底。
        for raw in _EMAIL_RE.findall(text):
            address = cls.normalize_address(raw)
            if "@" in address and address not in seen:
                seen.add(address)
                result.append(address)
        return result

    @staticmethod
    def _row(record: Any) -> dict[str, Any]:
        if isinstance(record, Mapping):
            return dict(record)
        to_dict = getattr(record, "to_dict", None)
        if callable(to_dict):
            value = to_dict()
            return dict(value) if isinstance(value, Mapping) else {}
        return {
            name: getattr(record, name, "")
            for name in (
                "account",
                "parent_mail",
                "alias_hme",
                "envelope_from",
                "return_path",
                "to_addr",
                "delivered_to",
            )
        }

    def _add_account(self, account: Any) -> None:
        resolver = getattr(account, "resolve_inbox", None)
        endpoint = resolver() if callable(resolver) else None
        context = MailAliasContext(
            account=self.normalize_address(str(getattr(account, "name", "") or "")),
            parent_mail=self.normalize_address(str(getattr(account, "mail", "") or "")),
            inbox_mail=self.normalize_address(str(getattr(endpoint, "mail", "") or "")),
            provider=str(getattr(endpoint, "name", "") or "").strip().lower(),
        )
        for key in (context.account, context.parent_mail):
            if key:
                self._contexts[key] = context

    def _context(self, row: Mapping[str, Any]) -> MailAliasContext | None:
        for value in (row.get("account"), row.get("parent_mail")):
            key = self.normalize_address(str(value or ""))
            if key and key in self._contexts:
                return self._contexts[key]
        return None

    def extract(self, record: Any) -> MailAliasMatch:
        row = self._row(record)
        context = self._context(row)
        provider = context.provider if context else ""

        if provider == "163mail":
            return self._extract_163(row, context)
        if provider in {"apple", "outlook"}:
            return self._extract_return_path(row, context)

        explicit = self.addresses(row.get("alias_hme"))
        if explicit:
            return MailAliasMatch(
                address=explicit[0],
                source="alias_hme",
                provider=provider,
            )

        return MailAliasMatch(provider=provider)

    def extract_address(self, record: Any) -> str:
        return self.extract(record).address

    def inbox_mail(self, record: Any) -> str:
        """返回该邮件实际使用的收件邮箱；未加载账号上下文时返回空串。"""
        context = self._context(self._row(record))
        return context.inbox_mail if context else ""

    @classmethod
    def decode_verp_alias(cls, value: Any) -> str:
        """解码 bounce/bounces VERP 地址中编码的真实收件别名。"""
        addresses = cls.addresses(value)
        if not addresses:
            return ""
        outer_local = addresses[0].partition("@")[0]
        prefix = re.match(r"^bounces?\+(?P<body>.+)$", outer_local, flags=re.I)
        if not prefix:
            return ""
        body = prefix.group("body")
        first_separator = body.find("-")
        if first_separator <= 0:
            return ""
        remainder = body[first_separator + 1 :]
        # bounces+campaign-token-recipient 与 bounce+campaign-recipient
        # 并存；第二段为短十六进制 token 时将其跳过，否则保留别名中的连字符。
        token_separator = remainder.find("-")
        if token_separator > 0:
            token = remainder[:token_separator]
            if re.fullmatch(r"[0-9a-f]{4,}", token, flags=re.I):
                remainder = remainder[token_separator + 1 :]
        encoded = remainder
        local, separator, domain = encoded.rpartition("=")
        if not separator or not local or not domain:
            return ""
        decoded = cls.normalize_address(f"{local}@{domain}")
        return decoded if re.fullmatch(_EMAIL_RE, decoded) else ""

    @classmethod
    def decode_163_verp_alias(cls, envelope_from: Any) -> str:
        """兼容旧接口名：163 使用 envelope_from 的 VERP 解码。"""
        return cls.decode_verp_alias(envelope_from)

    def _extract_163(
        self,
        row: Mapping[str, Any],
        context: MailAliasContext,
    ) -> MailAliasMatch:
        excluded = {
            self.normalize_address(str(row.get("account") or "")),
            self.normalize_address(str(row.get("parent_mail") or "")),
            context.account,
            context.parent_mail,
            context.inbox_mail,
        }
        excluded.discard("")

        # SendGrid/OpenAI 的 VERP 退信地址把真实投递目标编码在外层本地部分：
        # bounces+ID-TOKEN-user+tag=icloud.com@sender -> user+tag@icloud.com
        for source in ("envelope_from", "return_path"):
            decoded = self.decode_verp_alias(row.get(source))
            if decoded and decoded not in excluded:
                return MailAliasMatch(
                    address=decoded,
                    source=f"{source}_verp",
                    provider=context.provider,
                )

        explicit = self.addresses(row.get("alias_hme"))
        if explicit:
            return MailAliasMatch(
                address=explicit[0],
                source="alias_hme",
                provider=context.provider,
            )

        # 投递头比 To 更接近 SMTP 最终收件地址；163 样本中投递头为空，
        # 因而会自然回落到 To 并识别转发前的 iCloud HME 地址。
        for source in ("delivered_to", "to_addr"):
            for address in self.addresses(row.get(source)):
                if address not in excluded:
                    return MailAliasMatch(
                        address=address,
                        source=source,
                        provider=context.provider,
                    )
        return MailAliasMatch(provider=context.provider)

    def _extract_return_path(
        self,
        row: Mapping[str, Any],
        context: MailAliasContext,
    ) -> MailAliasMatch:
        excluded = {
            self.normalize_address(str(row.get("account") or "")),
            self.normalize_address(str(row.get("parent_mail") or "")),
            context.account,
            context.parent_mail,
            context.inbox_mail,
        }
        excluded.discard("")
        decoded = self.decode_verp_alias(row.get("return_path"))
        if decoded and decoded not in excluded:
            return MailAliasMatch(
                address=decoded,
                source="return_path_verp",
                provider=context.provider,
            )

        explicit = self.addresses(row.get("alias_hme"))
        if explicit:
            return MailAliasMatch(
                address=explicit[0],
                source="alias_hme",
                provider=context.provider,
            )
        return MailAliasMatch(provider=context.provider)


__all__ = ["MailAliasContext", "MailAliasExtractor", "MailAliasMatch"]
