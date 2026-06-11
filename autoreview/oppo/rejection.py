"""Helpers for OPPO review rejection analysis."""

from __future__ import annotations

import re
from typing import Any


JsonDict = dict[str, Any]

SIMILARITY_RE = re.compile(r"相似度\s*([0-9]+(?:\.[0-9]+)?)")
SIMILAR_APP_RE = re.compile(r"与[“\"]?([^”\"，,。；;\s]+)[”\"]?存在")

APK_SIMILARITY_KEYWORDS = (
    "套用模板",
    "马甲",
    "APK相似度",
    "相似度",
    "充分改动之前请勿重复提交",
)

ICP_KEYWORDS = (
    "ICP备案",
    "备案网站",
    "备案号",
)

REPEAT_WARNING_KEYWORDS = (
    "请勿重复提交",
    "充分改动之前",
)


def analyze_rejection_reason(reason: str) -> JsonDict:
    normalized = (reason or "").strip()
    categories: list[str] = []
    required_actions: list[str] = []
    blocking_reasons: list[str] = []

    has_similarity_issue = any(keyword in normalized for keyword in APK_SIMILARITY_KEYWORDS)
    has_icp_issue = any(keyword in normalized for keyword in ICP_KEYWORDS)
    has_repeat_warning = any(keyword in normalized for keyword in REPEAT_WARNING_KEYWORDS)

    if has_similarity_issue:
        categories.append("apk_similarity_or_template")
        required_actions.append(
            "充分修改 APK，降低模板/马甲包相似度；如果确实是独立应用，准备能证明独立性的申诉材料。"
        )
        blocking_reasons.append(
            "OPPO 已标记模板/马甲包相似风险，原 APK 不改动直接重提很可能再次被拒。"
        )

    if has_icp_issue:
        categories.append("missing_icp_proof")
        required_actions.append(
            "补充公司自有、与应用一致的 ICP 备案网站证明；网站需可访问、展示备案号，并能查到该应用相关信息。"
        )

    if has_repeat_warning:
        categories.append("do_not_repeat_submit_without_changes")

    similarity_score = None
    similarity_match = SIMILARITY_RE.search(normalized)
    if similarity_match:
        similarity_score = float(similarity_match.group(1))

    similar_app = None
    similar_app_match = SIMILAR_APP_RE.search(normalized)
    if similar_app_match:
        similar_app = similar_app_match.group(1)

    return {
        "categories": categories,
        "similarity_score": similarity_score,
        "similar_app": similar_app,
        "can_resubmit_same_apk": not has_similarity_issue and not has_repeat_warning,
        "required_actions": required_actions,
        "blocking_reasons": blocking_reasons,
        "evidence_targets": [
            "OPPO backend: 测试附加说明",
            "OPPO backend: 版权证明",
            "OPPO backend: 特殊类证书",
        ]
        if has_icp_issue
        else [],
    }
