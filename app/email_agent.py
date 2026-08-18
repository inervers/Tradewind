"""Tradewind 核心：开发信生成 Agent。

业务背景：医美设备出口商，通过 Google Maps/搜索找海外美容院 → 挖邮箱 →
主动发开发信获客。本 Agent 基于产品资料 + 历史邮件，生成个性化开发信。

数据流：
    客户名单（爬虫） → 客户画像
    产品/邮件资料（本地关键词检索） → 卖点 + 话术
    → LLM 生成开发信 → 规则自检 → LLM-as-judge 打分 → history/evaluation log

用法：
    python -m app.email_agent "Glow Skin Clinic" --country Spain --product 激光脱毛仪
    python -m app.email_agent "Beauty House" --country 德国 --product 皮秒 --judge -v
"""

from __future__ import annotations

import argparse
import json
import re
import time

from app.config import get_company_profile, settings
from app.llm import build_llm
from app.memory import save_email
from app.tools import LocalSearchTool

GENERATE_PROMPT = """你是外贸 cold email 写作专家，为一家医美设备出口商撰写一封开发信。

【目标客户】
名称：{customer}
所在国家/地区：{country}
补充背景：{extra}

【产品资料】（来自公司产品库，挑选与客户最相关的内容写入邮件；产品产自中国大陆，对香港客户可提物流快/售后方便）
{products}

【历史邮件风格参考】（公司此前使用过的邮件，学习其语气与结构，不要照抄）
{emails}

{template_rule}

{fmt_inst}
{lang_inst}

写作要求：
1. 开头个性化：自然提到客户所在市场/业务，避免"Dear Sir/Madam"式模板感
2. 正文突出 1-2 个与客户最相关的产品卖点（参数、认证、差异化优势）
3. 语气专业但有温度，不夸张、不虚假承诺
4. 正文结尾不要自行添加公司签名，系统会按设置统一追加

只输出正文内容（邮件格式则以 Subject 行开头），不要解释。"""

# 语言指令：香港市场默认繁体中文（香港用词，非简体）
LANG_INSTRUCTIONS = {
    "en": "使用英文撰写（international business English）。",
    "zh-hant": (
        "使用香港繁體中文撰寫（不是簡體，用香港習慣用詞：聯絡、回覆、感興趣、"
        "美容院、儀器、激光、皮秒）。"
    ),
}

# 输出形态：email = 正式开发信；whatsapp = 手机短消息（口语化）
FORMAT_INSTRUCTIONS = {
    "email": (
        "格式：正式开发信邮件，含 Subject 主题行。"
        "正文 150-200 字（英文 100-120 词），结尾明确 CTA。"
    ),
    "whatsapp": (
        "格式：WhatsApp 短消息（客户在手机上直接看到）。"
        "150 字以内（英文 60 词以内），口语化、有温度。"
        "开头一句打招呼（含店名），正文一句产品价值，结尾一句轻量 CTA"
        "（如『想了解詳情可以回覆我，我發目錄俾你』）。不要 Subject，不要正式信件格式。"
    ),
}

JUDGE_PROMPT = """你是外贸邮件营销专家。给下面这封开发信按 cold email 最佳实践打分（1-5 分，5 最优）：

维度：
- personalization: 个性化程度（是否针对客户，而非模板）
- value_prop: 价值主张清晰度（是否说清产品对客户的价值）
- clarity: 结构与表达清晰度
- cta: 行动号召明确度（是否让客户知道下一步做什么）

只输出 JSON，格式严格如下：
{{"personalization": 0-5, "value_prop": 0-5, "clarity": 0-5, "cta": 0-5, "overall": 0-5, "suggestions": "改进建议（必须用简体中文，中国大陆普通话表达，2-3 条具体可执行）"}}

邮件内容：
{email}"""

_JUDGE_SCORE_FIELDS = ("personalization", "value_prop", "clarity", "cta", "overall")


def _parse_judge_scores(raw: str) -> tuple[dict | None, str]:
    """解析并校验 judge 输出，避免 malformed/越界分数污染历史记录。"""
    text = (raw or "").strip()
    if not text:
        return None, "malformed"
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None, "malformed"
        try:
            payload = json.loads(match.group(0))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, "malformed"
    if not isinstance(payload, dict):
        return None, "malformed"

    normalized: dict[str, object] = {}
    for field in _JUDGE_SCORE_FIELDS:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "malformed"
        if not 0 <= float(value) <= 5:
            return None, "malformed"
        normalized[field] = int(value) if isinstance(value, int) else round(float(value), 3)
    suggestions = payload.get("suggestions", "")
    if not isinstance(suggestions, str):
        return None, "malformed"
    normalized["suggestions"] = suggestions.strip()
    return normalized, "valid"


class GenerationCancelled(Exception):
    """生成被用户取消（停止按钮触发）。"""


# 纯本地检索（产品/话术都存本地 JSON 库，自包含零外部依赖）。


def _local_records(
    query: str,
    max_results: int,
    include_files: tuple[str, ...] | None = None,
) -> list[dict]:
    tool = LocalSearchTool(include_files=include_files)
    return [
        {"title": r.title, "content": r.snippet, "source": r.url}
        for r in tool.search(query, max_results=max_results)
        if r.title
    ]


def _rule_check(email: str, product_kws: list[str], identifiers: list[str],
                language: str = "zh-hant", format_: str = "email") -> list[str]:
    """规则自检（零 token）：按语言 + 形态分支检查。

    identifiers 是从产品资料提取的英文型号（Diode-808 等），
    供英文邮件匹配（中文关键词对英文邮件无效）。
    """
    issues: list[str] = []
    if len(email) > 2000:
        issues.append("内容过长（>2000 字符）")

    closing = {
        "en": r"look forward|best regards|kind regards|warm regards|sincerely|cheers|thanks",
        "zh-hant": r"期待|回覆|聯絡|敬上|此致|祝好|謝謝|多谢",
    }
    cta = {
        "en": r"reply|contact|call|schedule|interested|learn more|sample|catalog",
        "zh-hant": r"回覆|聯絡|感興趣|了解|預約|安排|目錄|樣板|样品",
    }

    if format_ == "whatsapp":
        # 短消息：放宽规则，只查开场、CTA、长度
        if len(email) > 300:
            issues.append("WhatsApp 消息过长（>300 字符）")
        if not re.search(cta[language], email, re.I):
            issues.append("缺少 CTA（回覆/聯絡/感興趣）")
        if not re.search(r"你好|您好|hi |hello|早晨", email, re.I):
            issues.append("缺少开场问候")
    else:
        if not re.search(closing[language], email, re.I):
            issues.append("缺少礼貌收尾")
        if not re.search(cta[language], email, re.I):
            issues.append("缺少明确 CTA")
        if re.search(r"dear sir|dear madam|to whom it may concern|敬啟者|敬启者", email, re.I):
            issues.append("使用了模板化称呼（Dear Sir/Madam/敬啟者）")

    # 产品卖点检查：中文关键词或英文型号，任一命中即可
    all_kws = [k for k in product_kws if k] + identifiers
    if all_kws and not any(k.lower() in email.lower() for k in all_kws):
        issues.append(f"未提及产品卖点（{all_kws[:3]}）")
    return issues


def _extract_identifiers(products: list[dict]) -> list[str]:
    """从产品资料提取英文标识（型号 Diode-808、英文产品词），供英文邮件匹配。"""
    ids: set[str] = set()
    for p in products:
        text = f"{p.get('title', '')} {p.get('content', '')}"
        # 型号：字母+数字组合（Diode-808, Pico-1064, Hydra-7）
        ids.update(re.findall(r"\b[A-Z][A-Za-z]*[- ]?\d{1,4}\b", text))
        # 英文 2-3 词卖点短语（laser hair removal 等）
        ids.update(
            m.group(0)
            for m in re.finditer(r"\b([a-z]+ ){1,2}(system|machine|device|technology|treatment)\b", text, re.I)
        )
    return sorted(ids)


def _render(records: list[dict], content_limit: int = 800) -> str:
    lines = []
    for r in records:
        if r.get("title"):
            lines.append(f"- {r['title']}: {r.get('content', '')[:content_limit]}")
    return "\n".join(lines)


def _signature_block(profile: dict[str, str], format_: str) -> str:
    """按消息形态生成确定性签名；WhatsApp 只保留轻量身份。"""
    sender = profile.get("sender_name", "").strip()
    company = profile.get("company_name", "").strip()
    if format_ == "whatsapp":
        brand = re.split(r"\s+[–—-]\s+", company, maxsplit=1)[0].strip()
        return "｜".join(part for part in (sender, brand) if part)
    lines = [sender, company]
    for label, key in (("Email", "email"), ("WhatsApp", "whatsapp"), ("Website", "website")):
        value = profile.get(key, "").strip()
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(line for line in lines if line)


def _ensure_signature(text: str, profile: dict[str, str], format_: str) -> str:
    """缺少签名时追加；先清理尾部模型复制出的同值行，避免出现双签名。"""
    body = text.rstrip()
    block = _signature_block(profile, format_)
    if not block:
        return body
    expected = [line.strip() for line in block.splitlines() if line.strip()]
    lowered = body.casefold()
    if all(line.casefold() in lowered for line in expected):
        return body

    values = {str(value).strip().casefold() for value in profile.values() if str(value).strip()}
    lines = body.splitlines()
    start = max(0, len(lines) - 8)
    kept_tail = []
    for line in lines[start:]:
        normalized = re.sub(r"^(?:email|e-mail|whatsapp|website|web|tel|phone)\s*[:：]\s*", "", line.strip(), flags=re.I).casefold()
        if normalized and normalized in values:
            continue
        kept_tail.append(line)
    body = "\n".join([*lines[:start], *kept_tail]).rstrip()
    return f"{body}\n\n{block}" if body else block


def _stream_email(llm, prompt: str, cancel_check=None, stream_callback=None) -> tuple[str, int]:
    """流式生成：每收到一段内容检查取消标记，取消即抛 GenerationCancelled。

    stream_callback(chunk): 每段内容实时回调（前端流式展示用）。
    相比一次性 invoke，点停止后 1-2 秒内真正中断，token 浪费最小。
    """
    parts: list[str] = []
    tokens = 0
    for chunk in llm.stream(prompt):
        if cancel_check and cancel_check():
            raise GenerationCancelled()
        content = getattr(chunk, "content", "")
        if content:
            parts.append(content)
            if stream_callback:
                stream_callback(content)
        um = getattr(chunk, "usage_metadata", None)
        if um and um.get("total_tokens"):
            tokens = um["total_tokens"]
    return "".join(parts), tokens


def generate_email(
    customer: str,
    country: str,
    product: str,
    judge: bool = False,
    verbose: bool = False,
    extra: str = "",
    cancel_check=None,
    stream_callback=None,
    language: str = "zh-hant",
    format_: str = "email",
    template_record: dict | None = None,
    company_profile: dict[str, str] | None = None,
) -> dict:
    """生成开发信。

    cancel_check: Callable[[], bool]，返回 True 表示用户要停止（取消抛 GenerationCancelled）。
    stream_callback: Callable[[str], None]，每生成一段内容实时回调（前端打字机效果）。
    language: "zh-hant"（香港繁体，默认）|"en"（英文）。
    format_: "email"（正式开发信）|"whatsapp"（手机短消息）。
    """
    t0 = time.time()

    # ① 检索卖点 + 话术（纯本地：产品资料/话术模板都存在本地 JSON 库）
    products = _local_records(product, 3, ("products.json",))
    if not products:
        # 没指定产品/关键词检索不到 → 取全部设备（tags 都含「产品」）
        products = _local_records("产品", 3, ("products.json",))
    # 批量生成可固定使用用户选中的模板；未指定时保持原有自动检索行为。
    emails = ([{
        "title": str(template_record.get("title") or "指定话术模板"),
        "content": str(template_record.get("content") or ""),
        "source": str(template_record.get("source") or "local"),
    }] if template_record else _local_records("邮件 开发信 模板", 2, ("emails.json",)))

    # ② LLM 生成
    prod_block = _render(products, 1200) or "（产品库暂无匹配，按医美设备通用卖点撰写）"
    email_block = _render(emails, 4000 if template_record else 800) or "（暂无历史邮件，按行业惯例撰写）"
    template_rule = (
        "【指定模板要求】用户已明确选择上述模板。保留模板的核心信息、表达顺序和关键话术，"
        "只针对当前客户与产品做必要的个性化调整，不要忽略用户后来补充的段落。"
        if template_record else
        "【模板使用方式】以上内容仅作语气与结构参考，按当前客户重新组织。"
    )

    llm = build_llm(temperature=0.7)
    prompt = GENERATE_PROMPT.format(
        customer=customer, country=country, extra=extra or "（无）",
        products=prod_block, emails=email_block,
        template_rule=template_rule,
        fmt_inst=FORMAT_INSTRUCTIONS.get(format_, FORMAT_INSTRUCTIONS["email"]),
        lang_inst=LANG_INSTRUCTIONS.get(language, LANG_INSTRUCTIONS["zh-hant"]),
    )
    email, tokens = _stream_email(llm, prompt, cancel_check, stream_callback)
    email = _ensure_signature(email, company_profile or get_company_profile(), format_)

    # ③ 规则自检（按语言 + 形态）
    identifiers = _extract_identifiers(products)
    issues = _rule_check(
        email, re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", product),
        identifiers, language=language, format_=format_,
    )

    # ④ LLM-as-judge 打分（取消后跳过）
    scores = None
    judge_status = "disabled"
    if judge and not (cancel_check and cancel_check()):
        try:
            judge_llm = build_llm(temperature=0.0)
            jresp = judge_llm.invoke(JUDGE_PROMPT.format(email=email))
            scores, judge_status = _parse_judge_scores(getattr(jresp, "content", ""))
        except Exception:  # noqa: BLE001 - 打分失败不影响主流程
            scores = None
            judge_status = "unavailable"

    # 取消检查（生成完成但用户在保存前点停）
    if cancel_check and cancel_check():
        raise GenerationCancelled()

    elapsed = time.time() - t0

    # ⑤ 写入 history/evaluation log（不自动回灌 Prompt，供人工复盘）
    score = scores.get("overall", 0) if scores else 0
    save_email(customer, country, product, email, score, language, format_)

    if verbose:
        print(f"[tradewind] 产品{len(products)}条/邮件{len(emails)}条 tokens={tokens} time={elapsed:.1f}s")
        print(f"[tradewind] 规则自检: {'通过' if not issues else issues}")

    return {
        "customer": customer,
        "country": country,
        "product": product,
        "email": email,
        "issues": issues,
        "scores": scores,
        "judge_status": judge_status,
        "tokens": tokens,
        "time_s": round(elapsed, 1),
        "language": language,
        "format": format_,
        # 生成时实际引用了哪份话术模板（前端展示“参考话术”）
        "templates_used": [e.get("title", "") for e in emails if e.get("title")],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Tradewind 开发信 Agent")
    parser.add_argument("customer", help="目标客户（美容院名称）")
    parser.add_argument("--country", default="", help="所在国家/地区")
    parser.add_argument("--product", default="医美设备", help="产品类别/设备名")
    parser.add_argument("--extra", default="", help="客户补充背景（爬虫提供的画像）")
    parser.add_argument("--judge", action="store_true", help="LLM-as-judge 质量打分（花少量 token）")
    parser.add_argument("--lang", default="zh-hant", choices=["zh-hant", "en"], help="输出语言（默认香港繁体）")
    parser.add_argument("--format", dest="fmt", default="email", choices=["email", "whatsapp"], help="输出形态")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    result = generate_email(
        args.customer, args.country, args.product,
        judge=args.judge, verbose=args.verbose, extra=args.extra,
        language=args.lang, format_=args.fmt,
    )

    print("\n" + "=" * 50)
    print(result["email"])
    print("=" * 50)
    if result["issues"]:
        print(f"[规则自检] {len(result['issues'])} 项待改进:")
        for i in result["issues"]:
            print(f"  - {i}")
    else:
        print("[规则自检] 通过")
    if result["scores"]:
        s = result["scores"]
        print(f"[质量评分] personalization={s.get('personalization')} "
              f"value_prop={s.get('value_prop')} clarity={s.get('clarity')} "
              f"cta={s.get('cta')} overall={s.get('overall')}")
        if s.get("suggestions"):
            print(f"[改进建议] {s['suggestions']}")


if __name__ == "__main__":
    main()
