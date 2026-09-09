"""Extract recipient aliases using the rules in tools/mail_alias.py (stdlib only)."""

from __future__ import annotations

import re
from email.utils import getaddresses
from typing import Any, Mapping


EMAIL_RE = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+", re.I)


def normalize(value: Any) -> str:
    return str(value or "").strip().strip("<>;,.").lower()


def addresses(value: Any) -> list[str]:
    text = str(value or "")
    parsed = [raw for _, raw in getaddresses([text])]
    # Graph may put an email in both the display name and address field.
    candidates = (normalize(raw) for raw in parsed + EMAIL_RE.findall(text))
    return list(dict.fromkeys(address for address in candidates if "@" in address))


def decode_verp(value: Any) -> str:
    candidates = addresses(value)
    local_part = candidates[0].partition("@")[0] if candidates else ""
    match = re.match(r"^bounces?\+([^-]+)-(.+)$", local_part, re.I)
    if not match:
        return ""
    encoded = match.group(2)
    token, separator, remainder = encoded.partition("-")
    # Preserve recipient hyphens unless the leading segment looks like a token.
    if separator and re.fullmatch(r"[0-9a-f]{4,}", token, re.I):
        encoded = remainder
    local, separator, domain = encoded.rpartition("=")
    decoded = normalize(f"{local}@{domain}")
    return decoded if separator and local and domain and EMAIL_RE.fullmatch(decoded) else ""


def extract_alias(
    record: Mapping[str, Any],
    *,
    provider: str,
    account: str = "",
    parent_mail: str = "",
    inbox_mail: str = "",
) -> dict[str, Any]:
    """Return address/source/provider/matched; missing matches have an empty address."""
    provider = provider.strip().lower()
    excluded = {
        normalize(value)
        for value in (account, parent_mail, inbox_mail, record.get("account"), record.get("parent_mail"))
    }

    def result(address: str = "", source: str = "") -> dict[str, Any]:
        return {"address": address, "source": source, "provider": provider, "matched": bool(address)}

    verp_sources = {
        "163mail": ("envelope_from", "return_path"),
        "apple": ("return_path",),
        "outlook": ("return_path",),
    }.get(provider, ())
    for source in verp_sources:
        decoded = decode_verp(record.get(source))
        if decoded and decoded not in excluded:
            return result(decoded, f"{source}_verp")

    # Match production behavior: alias_hme is trusted, including base addresses.
    explicit = addresses(record.get("alias_hme"))
    if explicit:
        return result(explicit[0], "alias_hme")

    if provider == "163mail":
        for source in ("delivered_to", "to_addr"):
            for address in addresses(record.get(source)):
                if address not in excluded:
                    return result(address, source)
    return result()


if __name__ == "__main__":
    import json

    sample = {
        "account": "owner@icloud.com",
        "envelope_from": "bounces+12345-a1b2-demo+2=icloud.com@relay.example.com",
        "alias_hme": "demo@icloud.com",
    }
    print(json.dumps(extract_alias(sample, provider="163mail", inbox_mail="owner@163.com"), indent=2))
