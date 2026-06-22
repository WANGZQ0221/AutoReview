"""OpenAI-compatible LLM helper for chat intent fallback."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class LlmConfig:
    enabled: bool = False
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: int = 30
    temperature: float = 0.2
    max_tokens: int = 800
    openclaw_command: str = "openclaw"
    openclaw_args: tuple[str, ...] = ("run", "--stdin")
    openclaw_cwd: str = ""

    @classmethod
    def from_mapping(cls, raw: JsonDict | None) -> "LlmConfig":
        data = raw or {}
        openclaw = data.get("openclaw") if isinstance(data.get("openclaw"), dict) else {}
        args = openclaw.get("args") or data.get("openclaw_args") or ("run", "--stdin")
        if isinstance(args, str):
            args_tuple = tuple(item for item in args.split(" ") if item)
        else:
            args_tuple = tuple(str(item) for item in args)
        return cls(
            enabled=bool(data.get("enabled")),
            provider=str(data.get("provider") or data.get("type") or "openai_compatible").strip().lower(),
            base_url=str(data.get("base_url") or "").rstrip("/"),
            api_key=str(data.get("api_key") or ""),
            model=str(data.get("model") or ""),
            timeout_seconds=int(data.get("timeout_seconds") or 30),
            temperature=float(data.get("temperature") if data.get("temperature") is not None else 0.2),
            max_tokens=int(data.get("max_tokens") or 800),
            openclaw_command=str(openclaw.get("command") or data.get("openclaw_command") or "openclaw"),
            openclaw_args=args_tuple,
            openclaw_cwd=str(openclaw.get("cwd") or data.get("openclaw_cwd") or ""),
        )

    @property
    def ready(self) -> bool:
        if not self.enabled:
            return False
        if self.provider == "openclaw":
            return bool(self.openclaw_command)
        return bool(self.base_url and self.api_key and self.model)


class LlmError(RuntimeError):
    pass


def build_llm_client(raw_config: JsonDict | None) -> Any:
    config = LlmConfig.from_mapping(raw_config)
    if config.provider == "openclaw":
        return OpenClawLlmClient(config)
    return OpenAICompatibleLlmClient(config)


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

    def choose_tool(self, message: str, session: JsonDict, tools: list[JsonDict]) -> JsonDict:
        """Ask the model to choose one local tool using the ToolCall JSON protocol."""
        if not self.enabled:
            return {"tool": "disabled"}
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": _TOOL_CALL_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_tool_call_prompt(message, session, tools),
                },
            ],
            "temperature": self.config.temperature,
            "max_tokens": min(self.config.max_tokens, 900),
            "response_format": {"type": "json_object"},
        }
        response = self._post_json("/chat/completions", payload)
        content = _extract_message_content(response)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LlmError(f"LLM returned non-JSON ToolCall: {content[:120]}") from exc
        if not isinstance(parsed, dict):
            raise LlmError("LLM ToolCall response must be an object")
        return parsed

    def summarize_tool_result(
        self,
        message: str,
        session: JsonDict,
        tool_call: JsonDict,
        tool_result: JsonDict,
    ) -> str:
        """Turn a structured local tool result into a user-facing Chinese reply."""
        if not self.enabled:
            return ""
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": _TOOL_SUMMARY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_tool_summary_prompt(message, session, tool_call, tool_result),
                },
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
            raise LlmError(f"LLM returned non-JSON tool summary: {content[:120]}") from exc
        if not isinstance(parsed, dict):
            raise LlmError("LLM tool summary response must be an object")
        return str(parsed.get("reply") or "").strip()

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


class OpenClawLlmClient:
    """Command-backed LLM client that relies on OpenClaw's local account auth."""

    def __init__(self, config: LlmConfig):
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.ready

    def interpret(self, message: str, session: JsonDict) -> JsonDict:
        if not self.enabled:
            return {"intent": "disabled"}
        prompt = "\n\n".join(
            [
                _SYSTEM_PROMPT,
                "只输出 JSON 对象，不要输出 Markdown。",
                _build_user_prompt(message, session),
            ]
        )
        content = self._run_openclaw(prompt)
        parsed = _parse_json_object_from_text(content, error_label="OpenClaw returned non-JSON intent")
        return parsed

    def choose_tool(self, message: str, session: JsonDict, tools: list[JsonDict]) -> JsonDict:
        if not self.enabled:
            return {"tool": "disabled"}
        prompt = "\n\n".join(
            [
                _TOOL_CALL_SYSTEM_PROMPT,
                "只输出 ToolCall JSON 对象，不要输出 Markdown。",
                _build_tool_call_prompt(message, session, tools),
            ]
        )
        content = self._run_openclaw(prompt)
        return _parse_json_object_from_text(content, error_label="OpenClaw returned non-JSON ToolCall")

    def summarize_tool_result(
        self,
        message: str,
        session: JsonDict,
        tool_call: JsonDict,
        tool_result: JsonDict,
    ) -> str:
        if not self.enabled:
            return ""
        prompt = "\n\n".join(
            [
                _TOOL_SUMMARY_SYSTEM_PROMPT,
                "只输出 JSON 对象：{\"reply\":\"最终回复文本\"}，不要输出 Markdown。",
                _build_tool_summary_prompt(message, session, tool_call, tool_result),
            ]
        )
        content = self._run_openclaw(prompt)
        parsed = _parse_json_object_from_text(content, error_label="OpenClaw returned non-JSON summary")
        return str(parsed.get("reply") or "").strip()

    def _run_openclaw(self, prompt: str) -> str:
        args_template = " ".join(self.config.openclaw_args)
        prompt_arg = _compact_command_prompt(prompt) if "{prompt}" in args_template else prompt
        args = [self._expand_arg(item, prompt=prompt_arg) for item in self.config.openclaw_args]
        command = [self.config.openclaw_command, *args]
        use_stdin = "{prompt}" not in args_template
        try:
            result = subprocess.run(
                command,
                input=prompt if use_stdin else None,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                cwd=self.config.openclaw_cwd or None,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise LlmError(f"OpenClaw command not found: {self.config.openclaw_command}") from exc
        except subprocess.TimeoutExpired as exc:
            raise LlmError(f"OpenClaw timed out after {self.config.timeout_seconds}s") from exc
        output = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            detail = stderr or output or f"exit code {result.returncode}"
            raise LlmError(f"OpenClaw failed: {detail[:300]}")
        if not output:
            raise LlmError(f"OpenClaw returned empty output{': ' + stderr[:240] if stderr else ''}")
        return output

    def _expand_arg(self, value: str, *, prompt: str) -> str:
        return (
            str(value)
            .replace("{prompt}", prompt)
            .replace("{model}", self.config.model)
            .replace("{max_tokens}", str(self.config.max_tokens))
            .replace("{temperature}", str(self.config.temperature))
        )


def _extract_message_content(response: JsonDict) -> str:
    choices = response.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise LlmError("LLM response has no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise LlmError("LLM response has empty message content")
    return str(content)


def _parse_json_object_from_text(text: str, *, error_label: str) -> JsonDict:
    clean = str(text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.IGNORECASE).strip()
        clean = re.sub(r"\s*```$", "", clean).strip()
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(clean[start : end + 1])
            except json.JSONDecodeError as exc:
                raise LlmError(f"{error_label}: {clean[:160]}") from exc
        else:
            raise LlmError(f"{error_label}: {clean[:160]}")
    if not isinstance(parsed, dict):
        raise LlmError(f"{error_label}: JSON response must be an object")
    parsed = _unwrap_nested_json_object(parsed)
    if not isinstance(parsed, dict):
        raise LlmError(f"{error_label}: JSON response must be an object")
    return parsed


def _unwrap_nested_json_object(parsed: JsonDict) -> JsonDict:
    # Some OpenClaw JSON modes return an outer object whose reply/content field
    # is itself a JSON string. Unwrap only that narrow case.
    for key in ("reply", "content", "text", "message", "output"):
        value = parsed.get(key)
        nested = _try_parse_json_object(value)
        if nested is not None:
            return nested
    data = parsed.get("data")
    if isinstance(data, dict):
        for key in ("reply", "content", "text", "message", "output"):
            nested = _try_parse_json_object(data.get(key))
            if nested is not None:
                return nested
    result = parsed.get("result")
    if isinstance(result, dict):
        for key in ("reply", "content", "text", "message", "output"):
            nested = _try_parse_json_object(result.get(key))
            if nested is not None:
                return nested
    return parsed


def _try_parse_json_object(value: Any) -> JsonDict | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("{") or not text.endswith("}"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _compact_command_prompt(prompt: str) -> str:
    """Keep command-line prompt arguments single-line on Windows."""
    text = str(prompt or "")
    if len(text) > 12000:
        text = text[:12000] + "\n\n[内容过长，已截断]"
    return re.sub(r"\s+", " ", text).strip()


def _build_user_prompt(message: str, session: JsonDict) -> str:
    session_context = session if isinstance(session, dict) else {}
    if "session" in session_context:
        compact_session = _compact_session(session_context.get("session") or {})
        default_config = session_context.get("default_config") or {}
        preferences = session_context.get("preferences") or {}
        supported_market_stores = session_context.get("supported_market_stores") or []
        recent_conversation = session_context.get("recent_conversation") or []
        long_term_memory = session_context.get("long_term_memory") or {}
    else:
        compact_session = _compact_session(session_context)
        default_config = {}
        preferences = {}
        supported_market_stores = []
        recent_conversation = []
        long_term_memory = {}
    context = {
        "message": message,
        "session": compact_session,
        "recent_conversation": recent_conversation[-20:],
        "long_term_memory": long_term_memory,
        "default_config": default_config,
        "preferences": preferences,
        "supported_market_stores": supported_market_stores,
    }
    return json.dumps(context, ensure_ascii=False)


def _build_tool_call_prompt(message: str, session: JsonDict, tools: list[JsonDict]) -> str:
    context = json.loads(_build_user_prompt(message, session))
    context["tools"] = tools
    return json.dumps(context, ensure_ascii=False)


def _build_tool_summary_prompt(
    message: str,
    session: JsonDict,
    tool_call: JsonDict,
    tool_result: JsonDict,
) -> str:
    context = {
        "message": message,
        "session": json.loads(_build_user_prompt(message, session)),
        "tool_call": tool_call,
        "tool_result": tool_result,
    }
    return json.dumps(context, ensure_ascii=False)


def _compact_session(session: JsonDict) -> JsonDict:
    return {
        "app_info": session.get("app_info") or {},
        "agent_memory": session.get("agent_memory") or [],
        "long_term_memory": session.get("long_term_memory") or {},
        "conversation_history": (session.get("conversation_history") or [])[-20:],
        "market_store_preferences": session.get("market_store_preferences") or {},
        "last_rejection_analysis": session.get("last_rejection_analysis") or {},
        "last_market_search": session.get("last_market_search") or {},
        "last_market_search_request": session.get("last_market_search_request") or {},
        "last_package_lookup": session.get("last_package_lookup") or {},
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
- package_apk：单个 APK 打包。
- package_lookup：按应用名查询 packlist 中的包名、渠道、版本。
- batch_package：批量打包。
- view_submission_config：查看提交配置。
- stage_config_update：暂存配置修改。
- confirm_config_update：确认保存配置修改。
- cancel_config_update：取消配置修改。
- bind_material：绑定最近上传材料。
- index_materials：扫描本地上架资源并暂存可填充的 submission 材料字段。
- market_store_preference：调整当前会话的应用商店查询偏好，例如默认不查 Google Play、恢复查询 Google Play。
- market_search：查询应用商店公开数据，或搜索竞品。
- market_download_snapshot：记录本月竞品下载数据。
- unknown：确实无法判断。

输出字段：
{
  "intent": "上面的 intent",
  "confidence": 0.0到1.0,
  "query": "竞品搜索关键词，可空",
  "exact_match": true,
  "target_stores": ["本次只查询的商店 id，例如 oppo_app_market，可空"],
  "exclude_terms": ["不想要的名称片段，例如 极速版、火山版"],
  "include_terms": ["必须包含的名称片段，可空"],
  "reason": "驳回原因文本，可空",
  "version_code": "审核状态版本号，可空",
  "config_assignment": "例如 submission.version_code=10002，可空",
  "app_name": "用户要查询或打包的应用名，可空",
  "pkg_name": "Android 包名，可空",
  "channels": ["渠道名，可空"],
  "dry_run": true,
  "material_label": "APK/图标/截图1/版权证明/ICP证明，可空",
  "materials_root": "上架资源目录，可空",
  "disable_stores": ["要在当前会话排除的商店 id，例如 google_play"],
  "enable_stores": ["要在当前会话恢复查询的商店 id，例如 google_play"],
  "app_info": {"app_name":"","pkg_name":"","version_code":""},
  "preferences": {"结构化偏好键": "结构化偏好值，可空"},
  "memories": ["需要长期记住的事实或偏好，优先短句；也可输出 {category,text} 对象"],
  "reply": "chat/unknown 时给用户的简短中文回复"
}

安全规则：
- 不要执行提交、上传、保存配置等动作，只输出意图。
- 配置修改必须提取为 config_assignment，不要直接声称已保存。
- 用户要求最终提交/上架时，优先识别为 submission_check 或 submit_checklist，让本地工具先检查。
- 用户要求打包 APK 时，优先识别为 package_apk；如果用户给的是应用名，例如“八年级语文下册”，放到 app_name；如果给的是 com.xxx 包名，放到 pkg_name；如果给的是 xm1067 这种渠道，放到 channels。
- 用户要求查询“某应用对应什么包/渠道/版本/包名”时，识别为 package_lookup，并把应用名放到 app_name 或 query。
- 用户说“这个应用/这个包/刚才那个”时，结合 session.last_package_lookup 判断：如果是打包，识别为 package_apk；如果是询问信息，识别为 package_lookup。
- 用户要求批量打包时，识别为 batch_package。
- 用户要求“索引上架资源/查找上架材料/填充上架材料/匹配上架材料”时，识别为 index_materials；给中文应用名放 app_name，给 com.xxx 放 pkg_name；如果消息里有本地目录，放到 materials_root。该动作只暂存配置，不直接保存。
- 用户要求“默认不查/以后不查/恢复查询某应用商店”时，识别为 market_store_preference，并使用 supported_market_stores 里的 store id。
- 生成 market_search 或 market_download_snapshot 时，要结合 preferences.market_stores，不要建议查询当前会话已排除的商店。
- 用户说“查 OPPO 应用商店/只看 OPPO/小米应用商店里”这类一次性限定搜索范围时，不是 market_store_preference；应识别为 market_search 或 market_download_snapshot，并把对应商店放入 target_stores。
- 用户说“只要抖音APP，不要极速版/火山版/其他的不要”这类话时，不是 market_store_preference。应识别为 market_search 或 market_download_snapshot，并设置 exact_match=true，必要时填 exclude_terms。
- 用户说“之前发给过你/刚才说了/其他应用商店/换别的商店搜”时，优先参考 recent_conversation、session.conversation_history 和 session.last_market_search_request 里的上一轮关键词、精确匹配、排除词、商店范围，不要退回无关的 app_info。
- 判断用户意图时参考 recent_conversation 最近 20 条会话和 long_term_memory 结构化长期记忆。
- 需要长期保留的信息放入 memories 或 preferences；提交所需结构化数据放入 app_info/config_assignment 等对应字段。
- market_search 只用于“到应用商店里查同类 APP/游戏”。如果用户问“类似七麦/点点数据/蝉大师/Sensor Tower/data.ai 的第三方数据平台、ASO 平台、应用商店统计数据平台”，这是行业资料调研，不是应用商店竞品搜索；识别为 chat，并直接给出简短平台清单或说明需要网页搜索。
- 回答和命令判断要参考 default_config 和 session；不要把用户偏好写入默认配置。
- 如果缺关键信息，intent 可保持目标意图，同时 reply 提示补充。
"""


_TOOL_CALL_SYSTEM_PROMPT = """你是 AutoReview 的工具调度器。你只负责把用户消息转换成一个安全的 ToolCall JSON。

必须只输出 JSON 对象，不要 Markdown，不要解释。

ToolCall JSON 协议：
{
  "tool": "工具名；不需要工具时填 none",
  "arguments": {"传给工具的 JSON 参数"},
  "confidence": 0.0到1.0,
  "reason": "一句话说明为什么选这个工具",
  "memories": ["可选，需要长期记住的事实或偏好"],
  "preferences": {"可选，结构化偏好"},
  "app_info": {"app_name":"","pkg_name":"","version_code":""}
}

规则：
- 只能选择用户消息中 tools 列表里的工具名，不能编造工具。
- 普通闲聊、解释能力、问配置位置等不需要工具，tool=none，并在 reason 里说明。
- 用户要查应用商店、指定 APP、竞品、下载量时，优先 market_search；只有明确要求“记录/保存/月报/月度统计”本月下载数据时才用 market_download_snapshot。
- 用户明确“只要某某APP本体，不要极速版/火山版/其他版本”时，arguments.exact_match=true，并把不需要的版本名放到 exclude_terms。
- 用户说“只看 OPPO/小米/华为/荣耀/vivo 应用商店”是一次性范围，放到 target_stores，不要当成长期偏好。
- 用户说“之前发给过你/刚才说了/其他应用商店/换别的商店搜”时，优先沿用 recent_conversation、session.conversation_history 和 session.last_market_search_request 里的上一轮 market_search 参数，不要改问用户，也不要退回无关的 app_info。
- 用户要打包单个 APP 用 package_apk；给中文应用名放 app_name，给 com.xxx 放 pkg_name，给 xm1067 放 channels。
- 用户要查“应用名对应什么包/渠道/版本”用 package_lookup。
- 用户要批量打包用 batch_package。
- 用户要查 OPPO 审核状态用 oppo_status；版本号放 version_code。
- 用户问现在状态/进度用 session_status；用户问能做什么用 help。
- 用户问能不能提交、提交前检查、缺什么材料用 submission_check。
- 用户要查看提交配置用 view_submission_config。
- 用户要修改配置时用 stage_config_update，只能暂存；把类似 submission.version_code=10002 的内容放到 config_assignment，把 JSON 修改放到 json_patch。
- 用户要修改打包脚本 package.js 路径时，用 stage_config_update，并把 config_assignment 写成 packaging.script=完整路径；不要使用 package_script_path。
- 用户问“在哪个文件改打包脚本路径”，回答应指向 config/packaging.json 的 packaging.script 字段。
- 用户要求“全文搜索/在项目里搜索/查旧路径/搜索 development_sercer/package.js 残留”时，用 file_search。
- 不要编造工具参数。package_apk 不支持 use_staged_config；配置修改需要先 stage_config_update，再 confirm_config_update 保存后重新打包。
- 用户明确确认保存暂存配置时用 confirm_config_update；用户取消/放弃配置修改时用 cancel_config_update。
- 用户要把最近上传的 APK、图标、截图、版权证明、ICP 证明绑定到配置时，用 bind_material，并把材料类型放到 material_label。
- 用户要求“索引上架资源/查找上架材料/填充上架材料/匹配上架材料”时，用 index_materials；给中文应用名放 app_name，给 com.xxx 放 pkg_name；如果消息里有本地目录，放到 materials_root。该工具只暂存配置，不直接保存。
- 用户发来审核不通过/驳回原因文本并要求分析时，用 analyze_rejection，驳回正文放到 reason。
- 用户要求“分析这张图/分析最近图片/用 OCR 内容分析驳回”时，用 analyze_last_image。
- 用户要整改清单、待办、下一步整改时，用 remediation_checklist。
- 涉及正式提交、撤回、上传等高风险动作，如果没有对应工具或缺确认，tool=none。
"""


_TOOL_SUMMARY_SYSTEM_PROMPT = """你是 AutoReview 的飞书协作 agent。你会收到用户原话、ToolCall 和本地工具结果。

目标：把工具结果整理成简洁、准确、面向用户的中文回复。

必须只输出 JSON 对象：
{"reply": "最终回复文本"}

规则：
- 不要编造工具结果里没有的数据。
- 如果工具结果里已有 text，可以在保持事实不变的前提下压缩、整理、纠正口吻。
- 对应用商店搜索结果，严格遵守 filtered_names、exact_match、exclude_terms：已排除的应用不要再作为结果展示。
- 如果没有精确匹配结果，要明确说没有查到精确匹配，而不是把近似版本当成本体。
- 对打包、提交检查、状态查询，保留关键结论、路径、错误原因和下一步。
- 回复尽量使用清晰版块。普通一句话总结也用“处理结果 / 说明”；如果工具 text 已有清晰结构，可以保留原结构。
- 密钥、client_secret、api_key 等敏感字段不要展示。
- 回复不要超过 1200 字。
"""
