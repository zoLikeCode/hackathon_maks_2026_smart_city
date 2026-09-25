from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any


class ContactVerificationError(ValueError):
    """Контакт MAX нельзя использовать для подтверждения номера."""


def normalize_vcard(vcf_info: str) -> str:
    """MAX требует хешировать VCard с настоящими переводами строк CRLF."""
    return vcf_info.replace("\\r\\n", "\r\n")


def verify_contact_signature(token: str, vcf_info: str, signature: str) -> bool:
    if not token or not vcf_info or not signature:
        return False
    expected = hmac.new(
        token.encode("utf-8"),
        normalize_vcard(vcf_info).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected.lower(), signature.lower())


def extract_phone_from_vcard(vcf_info: str) -> str:
    match = re.search(r"(?im)^TEL(?:;[^:]*)?:(.+)$", normalize_vcard(vcf_info))
    if match is None:
        raise ContactVerificationError("В контакте отсутствует номер телефона")

    digits = re.sub(r"\D", "", match.group(1))
    if len(digits) == 11 and digits.startswith("8"):
        digits = f"7{digits[1:]}"
    if not 10 <= len(digits) <= 15:
        raise ContactVerificationError("Номер телефона имеет некорректный формат")
    return f"+{digits}"


def find_contact_payload(message_body: dict[str, Any]) -> dict[str, Any] | None:
    for attachment in message_body.get("attachments") or []:
        if attachment.get("type") == "contact":
            payload = attachment.get("payload")
            if isinstance(payload, dict):
                return payload
    return None


def masked_phone(phone: str) -> str:
    if len(phone) < 6:
        return phone
    return f"{phone[:2]}•••••{phone[-4:]}"
