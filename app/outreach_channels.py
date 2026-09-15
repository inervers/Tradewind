"""Deterministic preparation helpers for reviewed outreach drafts."""

from __future__ import annotations

import re
from urllib.parse import quote


EN_OPT_OUT = "If you prefer not to receive further messages, please let me know."
ZH_OPT_OUT = "如不希望再收到联系，请告诉我，我会停止发送。"


def normalize_whatsapp_number(phone: str, country: str) -> str:
    raw = phone.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and 10 <= len(digits) <= 15:
        return digits
    if raw.startswith("00") and 10 <= len(digits[2:]) <= 15:
        return digits[2:]
    if re.search(r"香港|hong\s*kong|\bhk\b", country, re.I) and len(digits) == 8:
        return "852" + digits
    if digits.startswith("852") and len(digits) == 11:
        return digits
    if re.search(
        r"中国大陆|中國大陸|大陆|大陸|mainland|\bchina\b", country, re.I
    ) and re.fullmatch(r"1\d{10}", digits):
        return "86" + digits
    if digits.startswith("86") and len(digits) == 13:
        return digits
    raise ValueError("号码缺少可确认的国家码")


def build_whatsapp_link(phone: str, country: str, body: str) -> str:
    if not body.strip():
        raise ValueError("WhatsApp 消息不能为空")
    number = normalize_whatsapp_number(phone, country)
    return f"https://wa.me/{number}?text={quote(body.strip(), safe='')}"


def split_generated_email(content: str) -> tuple[str, str]:
    text = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return "", ""
    first, separator, rest = text.partition("\n")
    match = re.match(r"^(?:subject|主题)\s*[:：]\s*(.+?)\s*$", first, re.I)
    if not match:
        return "", text
    return match.group(1).strip(), rest.lstrip() if separator else ""


def _is_chinese(language: str) -> bool:
    value = language.strip().casefold()
    return value.startswith("zh") or "中文" in value or "chinese" in value


def ensure_cold_email_footer(
    body: str,
    sender_name: str,
    company_name: str,
    language: str,
) -> str:
    value = body.strip()
    folded = value.casefold()
    signature_parts = [part.strip() for part in (sender_name, company_name) if part.strip()]
    if signature_parts and not all(part.casefold() in folded for part in signature_parts):
        value = f"{value}\n\n" if value else ""
        value += "\n".join(signature_parts)
    opt_out = ZH_OPT_OUT if _is_chinese(language) else EN_OPT_OUT
    folded = value.casefold()
    opt_out_present = (
        "not to receive further messages" in folded
        or "stop future contact" in folded
        or "停止发送" in value
        or "不希望再收到联系" in value
    )
    if not opt_out_present:
        value = f"{value}\n\n" if value else ""
        value += opt_out
    return value


def validate_email_draft(
    subject: str,
    body: str,
    sender_name: str,
    company_name: str,
) -> list[str]:
    issues: list[str] = []
    if not subject.strip():
        issues.append("missing_subject")
    if not body.strip():
        issues.append("missing_body")
    folded = body.casefold()
    identities = [part.strip().casefold() for part in (sender_name, company_name) if part.strip()]
    if len(identities) != 2 or not all(part in folded for part in identities):
        issues.append("missing_sender_identity")
    if not (
        "not to receive further messages" in folded
        or "stop future contact" in folded
        or "停止发送" in body
        or "不希望再收到联系" in body
    ):
        issues.append("missing_opt_out")
    return issues
