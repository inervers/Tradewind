from __future__ import annotations

from collections.abc import Callable


def matches_targets(result: dict, targets: list[str]) -> bool:
    """Maps/Webs 的原始结果是否符合前端选择的联系方式。"""
    if not targets or "all" in targets:
        return True
    email = str(result.get("email") or "")
    has_email = bool(email and "WhatsApp" not in email)
    has_contact = bool(result.get("phone") or result.get("wa_link") or result.get("whatsapp"))
    return bool(
        ("email" in targets and has_email)
        or (("whatsapp" in targets or "phone" in targets) and has_contact)
    )


def target_reached(results: list[dict], target: int,
                   result_filter: Callable[[dict], bool] | None = None) -> bool:
    """目标数按有效结果计算，而不是按已检查候选数计算。"""
    if result_filter is None:
        return len(results) >= target
    return sum(1 for item in results if result_filter(item)) >= target
