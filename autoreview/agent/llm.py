"""OpenAI-compatible LLM helper for chat intent fallback."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class LlmConfig:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: int = 30
    temperature: float = 0.2
    max_tokens: int = 800

    @classmethod
    def from_mapping(cls, raw: JsonDict | None) -> "LlmConfig":
        data = raw or {}
        return cls(
            enabled=bool(data.get("enabled")),
            base_url=str(data.get("base_url") or "").rstrip("/"),
            api_key=str(data.get("api_key") or ""),
            model=str(data.get("model") or ""),
            timeout_seconds=int(data.get("timeout_seconds") or 30),
            temperature=float(data.get("temperature") if data.get("temperature") is not None else 0.2),
            max_tokens=int(data.get("max_tokens") or 800),
        )

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.base_url and self.api_key and self.model)


class LlmError(RuntimeError):
    pass


class OpenAICompatibleLlmClient:
    """Small client for OpenAI-compatible chat completion APIs."""

    def __init__(self, config: LlmConfig):
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.ready

    def interpret(self, message: str, session: JsonDict) -> JsonDict:
        if not self.enabled:
            return {"intent": "disabled"}
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(message, session)},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "response_format": {"type": "json_object"},
        }
        response = self._post_json("/chat/completions", payload)
        content = _extract_message_content(response)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LlmError(f"LLM returned non-JSON content: {content[:120]}") from exc
        if not isinstance(parsed, dict):
            raise LlmError("LLM JSON response must be an object")
        return parsed

    def _post_json(self, path: str, payload: JsonDict) -> JsonDict:
        url = self.config.base_url + path
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise LlmError(f"LLM HTTP {exc.code}: {body[:240]}") from exc
        except URLError as exc:
            raise LlmError(f"LLM request failed: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LlmError(f"LLM HTTP response is not JSON: {raw[:120]}") from exc
        if not isinstance(data, dict):
            raise LlmError("LLM HTTP response must be a JSON object")
        return data


def _extract_message_content(response: JsonDict) -> str:
    choices = response.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise LlmError("LLM response has no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise LlmError("LLM response has empty message content")
    return str(content)


def _build_user_prompt(message: str, session: JsonDict) -> str:
    session_context = session if isinstance(session, dict) else {}
    if "session" in session_context:
        compact_session = _compact_session(session_context.get("session") or {})
        default_config = session_context.get("default_config") or {}
        preferences = session_context.get("preferences") or {}
        supported_market_stores = session_context.get("supported_market_stores") or []
    else:
        compact_session = _compact_session(session_context)
        default_config = {}
        preferences = {}
        supported_market_stores = []
    context = {
        "message": message,
        "session": compact_session,
        "default_config": default_config,
        "preferences": preferences,
        "supported_market_stores": supported_market_stores,
    }
    return json.dumps(context, ensure_ascii=False)


def _compact_session(session: JsonDict) -> JsonDict:
    return {
        "app_info": session.get("app_info") or {},
        "agent_memory": session.get("agent_memory") or [],
        "market_store_preferences": session.get("market_store_preferences") or {},
        "last_rejection_analysis": session.get("last_rejection_analysis") or {},
        "last_market_search": session.get("last_market_search") or {},
        "pending_config_patch": session.get("pending_config_patch") or {},
        "has_last_upload": bool(session.get("last_upload")),
        "has_last_image_analysis": bool(session.get("last_image_analysis")),
    }


_SYSTEM_PROMPT = """你是 AutoReview 的飞书协作 agent，项目目标是自动打包 Android APP，并协助上架到 OPPO、小米、荣耀、vivo、华为等应用商店。

你只做两件事：
1. 把用户消息识别成一个安全的结构化意图。
2. 如果不需要工具，给出简短中文建议或回复。

必须只输出 JSON 对象，不要输出 Markdown。

可用 intent：
- chat：普通交流、解释、建议。
- remember：记录用户偏好或长期上下文。
- clear_session_state：清空当前飞书会话状态。
- clear_all_state：清空全部飞书会话状态。
- status：查看当前会话状态。
- help：查看可用能力。
- record_app：记录应用信息。
- analyze_rejection：分析审核驳回原因。
- analyze_last_image：分析最近图片 OCR 文本。
- remediation_checklist：生成整改清单。
- oppo_status：查询 OPPO 审核状态。
- submission_check：提交前检查。
- submit_checklist：准备提交清单。
- view_submission_config：查看提交配置。
- stage_config_update：暂存配置修改。
- confirm_config_update：确认保存配置修改。
- cancel_config_update：取消配置修改。
- bind_material：绑定最近上传材料。
- market_store_preference：调整当前会话的应用商店查询偏好，例如默认不查 Google Play、恢复查询 Google Play。
- market_search：搜索竞品。
- market_download_snapshot：记录本月竞品下载数据。
- unknown：确实无法判断。

输出字段：
{
  "intent": "上面的 intent",
  "confidence": 0.0到1.0,
  "query": "竞品搜索关键词，可空",
  "reason": "驳回原因文本，可空",
  "version_code": "审核状态版本号，可空",
  "config_assignment": "例如 submission.version_code=10002，可空",
  "material_label": "APK/图标/截图1/版权证明/ICP证明，可空",
  "disable_stores": ["要在当前会话排除的商店 id，例如 google_play"],
  "enable_stores": ["要在当前会话恢复查询的商店 id，例如 google_play"],
  "app_info": {"app_name":"","pkg_name":"","version_code":""},
  "memories": ["需要长期记住的事实或偏好"],
  "reply": "chat/unknown 时给用户的简短中文回复"
}

安全规则：
- 不要执行提交、上传、保存配置等动作，只输出意图。
- 配置修改必须提取为 config_assignment，不要直接声称已保存。
- 用户要求最终提交/上架时，优先识别为 submission_check 或 submit_checklist，让本地工具先检查。
- 用户要求“默认不查/以后不查/恢复查询某应用商店”时，识别为 market_store_preference，并使用 supported_market_stores 里的 store id。
- 生成 market_search 或 market_download_snapshot 时，要结合 preferences.market_stores，不要建议查询当前会话已排除的商店。
- 回答和命令判断要参考 default_config 和 session；不要把用户偏好写入默认配置。
- 如果缺关键信息，intent 可保持目标意图，同时 reply 提示补充。
"""
