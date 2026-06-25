"""Review collaboration agent.

This layer turns chat messages into review workflow actions. Store-specific
automation remains in the OPPO package.
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
import json
import re
import shutil
import time
import uuid
from typing import Any, Callable

from autoreview.market import AppMarketSearchResult, AppMarketSearcher, build_monthly_snapshot
from autoreview.materials.indexer import MaterialIndexError, suggest_submission_materials
from autoreview.packaging.agent import (
    PackagingAgent,
    format_batch_package_result,
    format_package_result,
    parse_package_request,
)
from autoreview.packaging.packlist import (
    resolve_packlist_app_name_entries,
    resolve_packlist_channel_entries,
    scan_packlist,
    scan_packlist_snapshot,
)
from autoreview.oppo.agent import OppoSubmissionAgent, extract_rejection_reason
from autoreview.oppo.config import OppoSubmissionConfig
from autoreview.oppo.errors import OppoError
from autoreview.oppo.rejection import analyze_rejection_reason

from .config_editor import (
    ConfigEditError,
    apply_config_patch_to_targets,
    build_assignment_patch,
    build_json_patch,
    format_config_summary,
    format_patch_summary,
)
from .materials import MaterialBindError, bind_uploaded_material
from .state import JsonStateStore
from .tools import ToolCall, ToolRegistry


JsonDict = dict[str, Any]

HELP_TEXT = """我可以协助这些场景：

可用场景

1. 闲聊与记忆
- “状态”
- “记录应用：应用名 / 包名 / 版本号”
- “清空当前记录”

2. 应用商店查询
- “搜索应用商店：抖音”
- “帮我查一下小米应用市场，抖音的下载量”

3. 竞品分析
- “搜索竞品：英语四级单词”
- “记录竞品下载：英语四级单词”

4. 打包与批量打包
- “八年级语文下册对应什么包”
- “打包 八年级语文下册 dry-run”
- “批量打包 dry-run”

5. OPPO 审核与提交检查
- “查询审核状态”
- “提交检查”
- “准备提交”
- “确认提审 OPPO”

6. 驳回分析与整改
- “分析驳回：<原因>”
- 发送驳回截图后说“分析这张图”
- “整改清单”

7. 配置与材料
- “查看提交配置”
- “设置提交配置：字段=值”
- “确认保存配置”
- 发送文件后说“绑定材料：APK/图标/截图1/版权证明/ICP证明”
- “索引上架资源：应用名/包名”
- “把这个 APK 放到项目根目录下的 release 里面”
- “确认移动文件”
- “取消移动文件”

8. 图片 OCR / image2
- 直接发送图片
- “分析最近图片”"""

PROJECT_LOGIC_TEXT = """AutoReview Agent
处理完成，结果如下。

当前这套项目逻辑主要分四层：

1. 主入口配置
- `config/oppo_submission.json` 是飞书入口和 OPPO 提交主配置。
- `config/packaging.json` 是通用打包配置，负责 Android 项目目录、package.js、批量清单和 packlist 快照。
- `config/llm_config.json` 是共享大模型配置；当前通过 OpenClaw wrapper 调用本机授权。
- `config/market_data.json` 是应用商店/竞品数据查询配置。

原则：打包配置不塞进 OPPO submission；商店配置只管提交材料、凭证和平台字段。

2. 记忆怎么处理
- 结构化会话状态保存在 `data/review_agent_state.json`。
- 最近 20 轮对话保存在 `data/sessions/<session_id>/turns.jsonl`，用于“刚才那个/这个应用/继续”等上下文。
- 全链路调试 trace 保存在 `data/sessions/<session_id>/trace-YYYY-MM-DD.jsonl`，记录大模型理解、工具选择、工具结果和最终回复。
- 长期记忆不是大杂烩，分成 notes、app_info、submission、preferences。

常见写入规则：
- “记录应用：应用名 / 包名 / 版本号”写入当前会话 app_info。
- “默认不查 Google Play”这类偏好写入当前会话 preferences，不改默认配置。
- “设置提交配置：字段=值”只暂存 pending_config_patch；必须再发“确认保存配置”才写文件。
- “记录竞品下载”会按月份写入 market_download_snapshots。

3. 工具调用怎么判断
优先级是：本地确定性规则 > LLM ToolCall > LLM intent > 本地语义兜底。

- 查包/打包优先走本地解析，避免大模型把“八年级语文下册”改错或乱码。
- “对应什么包/渠道/版本”走 package_lookup。
- “打包 xxx dry-run”走 package_apk；dry-run 不会真正打包。
- “批量打包 dry-run”走 batch_package。
- “帮我查小米应用市场，抖音下载量”走 market_search；只有明确“记录/月度/保存”才走 market_download_snapshot。
- “查询审核状态”走 oppo_status。
- “提交检查/能不能提交”走 submission_check。
- “查看提交配置”只展示非密钥摘要。
- “设置提交配置”走 stage_config_update，确认后才写入。
- “把这个 APK 放到 release 里”走 stage_file_move，只暂存；“确认移动文件”才执行复制/移动。
- “绑定材料：APK/图标/截图1/版权证明/ICP证明”走 bind_material。
- “分析驳回/分析最近图片/整改清单”分别走驳回分析、图片 OCR 上下文和整改清单。

安全边界：
- 大模型只输出结构化意图或 ToolCall，不直接改文件、不提交审核、不上传材料。
- 密钥字段不在飞书展示，也不允许通过飞书修改。
- 文件移动/复制必须先暂存再确认，目标路径限制在项目目录内。
- 正式提交、上传、撤回这类高风险动作不靠自由聊天触发。

4. skill/能力说明怎么写
当前 AutoReview 没有单独的 OpenClaw `skills/*.md` 目录；能力说明主要写在三处：
- `HELP_TEXT`：飞书“帮助/工具总结/有哪些 skill”展示给用户。
- `llm.py` 里的 system prompt：告诉大模型可用 intent、字段、边界和安全规则。
- `ToolRegistry`：每个本地工具的 name、description、input_schema、handler。

如果后续要做成真正的 skill 文件，建议拆成路由型结构：
- `SKILL.md`：只写触发条件、第一反应、路由边界。
- `references/config-boundary.md`：配置文件职责边界。
- `references/memory.md`：会话状态、长期记忆、trace 规则。
- `references/tools.md`：工具选择矩阵。
- `references/safety.md`：密钥、提交、写文件边界。

一句话总结：AutoReview 不是让大模型直接干活，而是让它把自然语言变成安全、可审计的本地工具调用；真正执行都在 Python 工具层。"""


@dataclass
class AgentResponse:
    text: str
    data: JsonDict


class ReviewAgent:
    def __init__(
        self,
        state_store: JsonStateStore,
        *,
        oppo_config_path: str | Path | None = None,
        market_data_config_path: str | Path | None = None,
        packaging_config_path: str | Path | None = None,
        oppo_agent_factory: Callable[[], Any] | None = None,
        market_searcher_factory: Callable[[], Any] | None = None,
        llm_client: Any | None = None,
    ):
        self.state_store = state_store
        self.oppo_config_path = Path(oppo_config_path) if oppo_config_path else None
        self.market_data_config_path = Path(market_data_config_path) if market_data_config_path else None
        self.packaging_config_path = Path(packaging_config_path) if packaging_config_path else self._default_packaging_config_path()
        self.oppo_agent_factory = oppo_agent_factory
        self.market_searcher_factory = market_searcher_factory
        self.llm_client = llm_client
        self.packaging_agent = self._make_packaging_agent()
        self.tool_registry = self._build_tool_registry()
        self._active_traces: dict[str, JsonDict] = {}

    def _default_packaging_config_path(self) -> Path | None:
        if not self.oppo_config_path:
            return None
        candidate = self.oppo_config_path.parent / "packaging.json"
        return candidate if candidate.exists() else None

    def _make_packaging_agent(self) -> PackagingAgent:
        if self.packaging_config_path and self.packaging_config_path.exists():
            return PackagingAgent(self.packaging_config_path)
        if self.oppo_config_path:
            return PackagingAgent(self.oppo_config_path)
        return PackagingAgent(None)

    def _reload_packaging_agent(self) -> None:
        self.packaging_agent = self._make_packaging_agent()

    def handle_message(self, session_id: str, text: str, sender_id: str | None = None) -> AgentResponse:
        clean_text = self._normalize_incoming_text(text)
        trace_id = self._start_trace(session_id, clean_text, sender_id=sender_id)
        if not clean_text:
            response = AgentResponse("我收到空消息了。发送“帮助”可以查看可用指令。", {})
            self._finish_trace(session_id, trace_id, response)
            self._record_conversation_turn(session_id, "", response, sender_id=sender_id)
            return response

        response = self._handle_message_logic(session_id, clean_text, sender_id=sender_id)
        self._finish_trace(session_id, trace_id, response)
        if response.data.get("intent") not in {"clear_session_state", "clear_all_state"}:
            self._record_conversation_turn(session_id, clean_text, response, sender_id=sender_id)
        return response

    def _handle_message_logic(self, session_id: str, clean_text: str, sender_id: str | None = None) -> AgentResponse:
        if self._looks_like_oppo_app_list_request(clean_text.lower()):
            return self.query_oppo_app_list(session_id, sender_id=sender_id)

        if self._looks_like_oppo_submit_confirmation(clean_text.lower()):
            return self.submit_oppo(session_id, sender_id=sender_id, confirm=True)

        if self._looks_like_confirm_file_move_request(clean_text):
            return self.confirm_file_move(session_id, sender_id=sender_id)

        if self._looks_like_cancel_file_move_request(clean_text):
            return self.cancel_file_move(session_id, sender_id=sender_id)

        if not self._should_apply_llm_before_rules(clean_text):
            hard_rule_response = self._handle_hard_rule(session_id, clean_text, sender_id=sender_id)
            if hard_rule_response:
                return hard_rule_response
            return self._handle_message_with_local_fallback(session_id, clean_text, sender_id=sender_id)

        # 第 1 层：LLM 优先（开放表达先交给 LLM 判断）
        llm_decision = self._interpret_with_llm(session_id, clean_text, sender_id=sender_id)
        
        # 如果 LLM 决定使用工具，直接执行
        if tool_response := self._response_from_llm_tool_call(session_id, clean_text, sender_id=sender_id):
            return tool_response
        
        # 第 2 层：LLM 决策后的业务路由
        llm_response = self._response_from_llm_decision(
            session_id,
            clean_text,
            llm_decision,
            sender_id=sender_id,
            allow_chat=True,
        )
        if llm_response:
            return llm_response
        
        # 第 3 层：本地硬规则兜底（高频简单场景）
        hard_rule_response = self._handle_hard_rule(session_id, clean_text, sender_id=sender_id)
        if hard_rule_response:
            return hard_rule_response
        
        # 第 4 层：本地语义解析兜底
        return self._handle_message_with_local_fallback(session_id, clean_text, sender_id=sender_id)

    def _handle_hard_rule(self, session_id: str, clean_text: str, sender_id: str | None = None) -> AgentResponse | None:
        if self._looks_like_oppo_app_list_request(clean_text.lower()):
            return self.query_oppo_app_list(session_id, sender_id=sender_id)

        if self._looks_like_oppo_submit_confirmation(clean_text.lower()):
            return self.submit_oppo(session_id, sender_id=sender_id, confirm=True)

        market_followup = self._handle_market_followup(session_id, clean_text, sender_id=sender_id)
        if market_followup:
            return market_followup

        recent_context_response = self._answer_recent_context_question(session_id, clean_text)
        if recent_context_response:
            return recent_context_response

        packaging_followup = self._handle_packaging_followup(session_id, clean_text)
        if packaging_followup:
            return packaging_followup

        if clean_text in {"帮助", "help", "/help"}:
            return AgentResponse(HELP_TEXT, {"intent": "help"})

        if clean_text in {"状态", "当前状态"}:
            session = self.state_store.get_session(session_id)
            return AgentResponse(self._format_session(session), {"intent": "status", "session": session})

        if clean_text in {"清空记录", "清空当前记录", "清空当前状态", "重置记录", "重置当前记录", "重置当前会话"}:
            return self.clear_session_state(session_id)

        if clean_text in {"清空所有记录", "清空全部记录", "重置所有记录", "重置全部会话"}:
            return self.clear_all_state()

        if clean_text in {"确认保存配置", "保存配置", "确认配置"}:
            return self.confirm_config_update(session_id, sender_id=sender_id)

        if clean_text in {"取消保存配置", "取消配置修改", "放弃配置修改"}:
            return self.cancel_config_update(session_id, sender_id=sender_id)

        if self._looks_like_confirm_file_move_request(clean_text):
            return self.confirm_file_move(session_id, sender_id=sender_id)

        if self._looks_like_cancel_file_move_request(clean_text):
            return self.cancel_file_move(session_id, sender_id=sender_id)

        if clean_text in {"分析这张图", "用最近图片分析驳回", "分析最近图片", "分析图片"}:
            session = self.state_store.get_session(session_id)
            image_text = self._get_last_image_text(session)
            if not image_text:
                return AgentResponse(
                    _format_error_message(
                        "图片分析缺少 OCR 文本",
                        "最近图片还没有可用于分析的 OCR 文本。",
                        ["请先发送一张包含驳回原因的截图。"],
                    ),
                    {"intent": "analyze_last_image", "missing": "ocr_text"},
                )
            return self.analyze_rejection_text(session_id, image_text, sender_id=sender_id)

        return None

    def _handle_message_with_local_fallback(
        self,
        session_id: str,
        clean_text: str,
        sender_id: str | None = None,
    ) -> AgentResponse:
        if clean_text in {"帮助", "help", "/help"}:
            return AgentResponse(HELP_TEXT, {"intent": "help"})

        packaging_catalog = self._handle_packaging_catalog_request(session_id, clean_text)
        if packaging_catalog:
            return packaging_catalog

        material_index_direct = self._handle_material_index_request(session_id, clean_text, sender_id=sender_id)
        if material_index_direct:
            return material_index_direct

        if self._looks_like_file_move_request(clean_text):
            return self.stage_file_move(session_id, text=clean_text, sender_id=sender_id)

        preference_response = self._handle_market_store_preference(session_id, clean_text, sender_id=sender_id)
        if preference_response:
            return preference_response

        capability_response = self._answer_capability_question(session_id, clean_text)
        if capability_response:
            return capability_response

        research_response = self._handle_app_store_data_platform_research(clean_text)
        if research_response:
            return research_response

        if clean_text.startswith("分析驳回"):
            reason = self._extract_payload(clean_text)
            if not reason:
                return AgentResponse(
                    _format_error_message(
                        "驳回分析缺少原因",
                        "没有收到 OPPO 驳回原因正文。",
                        ["请按“分析驳回：<OPPO驳回原因>”发送完整原因。"],
                    ),
                    {"intent": "analyze_rejection"},
                )
            return self.analyze_rejection_text(session_id, reason, sender_id=sender_id)

        if clean_text in {"整改清单", "生成整改清单", "待办清单"}:
            return self.build_remediation_checklist(session_id, sender_id=sender_id)

        if clean_text in {"查看提交配置", "查看配置", "提交配置"}:
            return self.view_submission_config()

        if clean_text.startswith("设置提交配置"):
            return self.stage_config_assignment(session_id, self._extract_payload(clean_text), sender_id=sender_id)

        if clean_text.startswith("批量设置提交配置"):
            return self.stage_config_json(session_id, self._extract_payload(clean_text), sender_id=sender_id)

        packaging_script_update = self._extract_packaging_script_update(clean_text)
        if packaging_script_update:
            return self.stage_config_assignment(
                session_id,
                f"packaging.script={packaging_script_update}",
                sender_id=sender_id,
            )

        config_followup = self._answer_config_followup_question(session_id, clean_text)
        if config_followup:
            return config_followup

        if clean_text.startswith("绑定材料"):
            label = self._extract_payload(clean_text) or clean_text.replace("绑定材料", "", 1).strip()
            return self.bind_last_upload_as_material(
                session_id,
                label,
                sender_id=sender_id,
            )

        if clean_text.startswith(("搜索竞品", "竞品搜索", "搜索应用商店", "找竞品")):
            query = self._extract_market_query(clean_text, session_id)
            if not query:
                return AgentResponse(
                    _format_error_message(
                        "应用商店查询缺少关键词",
                        "没有识别到要查询的应用名或关键词。",
                        ["请按“搜索应用商店：关键词”或“搜索竞品：关键词”发送，例如“搜索竞品：英语四级单词”。"],
                    ),
                    {"intent": "market_search", "missing": "app_name"},
                )
            research_response = self._handle_app_store_data_platform_research(clean_text, query)
            if research_response:
                return research_response
            return self.search_competitors(
                session_id,
                query,
                sender_id=sender_id,
                source_text=clean_text,
                target_stores=_extract_target_market_stores(clean_text),
            )

        generic_search_query = self._extract_generic_app_search_query(clean_text)
        if generic_search_query:
            research_response = self._handle_app_store_data_platform_research(clean_text, generic_search_query)
            if research_response:
                return research_response
            return self.search_competitors(
                session_id,
                generic_search_query,
                sender_id=sender_id,
                source_text=clean_text,
                target_stores=_extract_target_market_stores(clean_text),
            )

        if clean_text.startswith(("记录竞品下载", "记录竞品月报", "月度记录竞品")):
            query = self._extract_market_query(clean_text, session_id)
            if not query:
                return AgentResponse(
                    _format_error_message(
                        "竞品下载记录缺少关键词",
                        "没有识别到要记录的应用名或关键词。",
                        ["请按“记录竞品下载：关键词”发送，例如“记录竞品下载：英语四级单词”。"],
                    ),
                    {"intent": "market_download_snapshot", "missing": "app_name"},
                )
            return self.record_competitor_downloads(
                session_id,
                query,
                sender_id=sender_id,
                source_text=clean_text,
                target_stores=_extract_target_market_stores(clean_text),
            )

        if clean_text.startswith("记录应用"):
            app_info = self._parse_app_info(self._extract_payload(clean_text))
            self.state_store.update_session(
                session_id,
                {
                    "app_info": app_info,
                    "sender_id": sender_id,
                },
            )
            return AgentResponse(
                self._format_record_app_info(app_info),
                {"intent": "record_app", "app_info": app_info},
            )

        if clean_text in {"状态", "当前状态"}:
            session = self.state_store.get_session(session_id)
            return AgentResponse(self._format_session(session), {"intent": "status", "session": session})

        if self._looks_like_default_app_question(clean_text.lower()):
            return self.describe_default_app(session_id)

        if self._looks_like_oppo_app_list_request(clean_text.lower()):
            return self.query_oppo_app_list(session_id, sender_id=sender_id)

        if clean_text.startswith("查询审核状态") or clean_text in {"审核状态", "OPPO状态", "oppo状态"}:
            return self.query_oppo_status(clean_text, session_id=session_id, sender_id=sender_id)

        if clean_text in {"提交检查", "校验配置", "检查提交"}:
            return self.check_submission(session_id)

        if clean_text in {"准备提交"}:
            return self.submit_oppo(session_id, sender_id=sender_id, confirm=False)

        semantic_response = self._handle_semantic_intent(session_id, clean_text, sender_id=sender_id)
        if semantic_response:
            return semantic_response

        return AgentResponse(
            "我还不确定你要我做什么。发送“帮助”查看可用指令。",
            {"intent": "unknown", "text": clean_text},
        )

    def search_competitors(
        self,
        session_id: str,
        query: str,
        sender_id: str | None = None,
        *,
        source_text: str = "",
        exact_match: bool = False,
        exclude_terms: list[str] | None = None,
        include_terms: list[str] | None = None,
        target_stores: list[str] | None = None,
    ) -> AgentResponse:
        query = _clean_market_query(query)
        if _is_contextual_app_reference(query):
            query = _clean_market_query((self._default_app_info(session_id) or {}).get("app_name"))
        if not query:
            return AgentResponse(
                _format_error_message(
                    "应用商店查询缺少关键词",
                    "没有识别到有效的应用名或关键词。",
                    ["例如发送“搜索应用商店：抖音”或“搜索竞品：英语四级单词”。"],
                ),
                {"intent": "market_search", "missing": "app_name"},
            )
        result = self._normalize_market_result(
            self._search_markets(session_id, query, limit=8, target_stores=set(target_stores or []))
        )
        result, filtered_names = self._filter_market_result(
            result,
            query=query,
            source_text=source_text,
            exact_match=exact_match,
            exclude_terms=exclude_terms,
            include_terms=include_terms,
        )
        self.state_store.update_session(
            session_id,
            {
                "last_market_search": result.to_dict(),
                "last_market_filtered_names": filtered_names,
                "last_market_search_request": {
                    "query": query,
                    "source_text": source_text,
                    "exact_match": filtered_names.get("exact_match", False),
                    "exclude_terms": filtered_names.get("exclude_terms") or [],
                    "include_terms": filtered_names.get("include_terms") or [],
                    "target_stores": _normalize_store_list(target_stores or []),
                },
                "sender_id": sender_id,
            },
        )
        return AgentResponse(
            self._format_market_search(result, filtered_names=filtered_names, source_text=source_text),
            {"intent": "market_search", "result": result.to_dict(), "filtered_names": filtered_names},
        )

    def record_competitor_downloads(
        self,
        session_id: str,
        query: str,
        sender_id: str | None = None,
        *,
        source_text: str = "",
        exact_match: bool = False,
        exclude_terms: list[str] | None = None,
        include_terms: list[str] | None = None,
        target_stores: list[str] | None = None,
    ) -> AgentResponse:
        query = _clean_market_query(query)
        if _is_contextual_app_reference(query):
            query = _clean_market_query((self._default_app_info(session_id) or {}).get("app_name"))
        if not query:
            return AgentResponse(
                _format_error_message(
                    "竞品下载记录缺少关键词",
                    "没有识别到有效的应用名或关键词。",
                    ["例如发送“记录竞品下载：英语四级单词”。"],
                ),
                {"intent": "market_download_snapshot", "missing": "app_name"},
            )
        result = self._normalize_market_result(
            self._search_markets(session_id, query, limit=8, target_stores=set(target_stores or []))
        )
        result, filtered_names = self._filter_market_result(
            result,
            query=query,
            source_text=source_text,
            exact_match=exact_match,
            exclude_terms=exclude_terms,
            include_terms=include_terms,
        )
        snapshot = build_monthly_snapshot(query, result)
        session = self.state_store.get_session(session_id)
        snapshots = dict(session.get("market_download_snapshots") or {})
        snapshots[snapshot["month"]] = snapshot
        self.state_store.update_session(
            session_id,
            {
                "last_market_search": result.to_dict(),
                "last_market_filtered_names": filtered_names,
                "last_market_search_request": {
                    "query": query,
                    "source_text": source_text,
                    "exact_match": filtered_names.get("exact_match", False),
                    "exclude_terms": filtered_names.get("exclude_terms") or [],
                    "include_terms": filtered_names.get("include_terms") or [],
                    "target_stores": _normalize_store_list(target_stores or []),
                },
                "market_download_snapshots": snapshots,
                "sender_id": sender_id,
            },
        )
        return AgentResponse(
            self._format_download_snapshot(snapshot, filtered_names=filtered_names),
            {"intent": "market_download_snapshot", "snapshot": snapshot, "filtered_names": filtered_names},
        )

    def clear_session_state(self, session_id: str) -> AgentResponse:
        self.state_store.clear_session(session_id)
        return AgentResponse(
            "会话记录已清空\n\n结果：\n- 当前会话记录已清空。",
            {"intent": "clear_session_state", "session_id": session_id},
        )

    def clear_all_state(self) -> AgentResponse:
        self.state_store.clear_all()
        return AgentResponse(
            "全部会话记录已清空\n\n结果：\n- 全部会话记录已清空。",
            {"intent": "clear_all_state"},
        )

    def describe_default_app(self, session_id: str) -> AgentResponse:
        app_info = self._default_app_info(session_id)
        if not app_info:
            return AgentResponse(
                _format_error_message(
                    "当前没有默认应用",
                    "当前会话和配置里都没有可用的默认应用信息。",
                    ["可以发送“记录应用：应用名 / 包名 / 版本号”。", "或先查询审核状态。"],
                ),
                {"intent": "describe_default_app", "missing": "app_info"},
            )
        return AgentResponse(
            "当前默认应用\n\n应用信息：\n"
            f"- 应用名：{app_info.get('app_name') or '未记录'}\n"
            f"- 包名：{app_info.get('pkg_name') or '未记录'}\n"
            f"- 版本号：{app_info.get('version_code') or '未记录'}",
            {"intent": "describe_default_app", "app_info": app_info},
        )

    def query_oppo_status(
        self,
        text: str,
        *,
        session_id: str | None = None,
        sender_id: str | None = None,
    ) -> AgentResponse:
        try:
            status = self._make_oppo_agent().status(self._extract_version_code(text))
        except OppoError as exc:
            return AgentResponse(
                _format_error_message("查询 OPPO 审核状态失败", str(exc), ["检查 OPPO 配置后重试。"]),
                {"intent": "oppo_status", "error": str(exc)},
            )
        if session_id:
            self._remember_status_app_info(session_id, status, sender_id=sender_id)
        return AgentResponse(
            self._format_oppo_status(status),
            {"intent": "oppo_status", "status": status},
        )

    def query_oppo_app_list(
        self,
        session_id: str,
        *,
        sender_id: str | None = None,
        limit: int = 100,
    ) -> AgentResponse:
        try:
            pkg_names = self._oppo_app_list_candidates(session_id)
            result = self._make_oppo_agent().list_created_apps(pkg_names=pkg_names, limit=limit)
        except OppoError as exc:
            return AgentResponse(
                _format_error_message("查询 OPPO 已创建应用失败", str(exc), ["检查 OPPO 配置和 packlist 快照后重试。"]),
                {"intent": "oppo_app_list", "error": str(exc)},
            )
        if sender_id:
            result["sender_id"] = sender_id
        self.state_store.update_session(session_id, {"last_oppo_app_list": result})
        return AgentResponse(
            self._format_oppo_app_list(result),
            {"intent": "oppo_app_list", "result": result},
        )

    def check_submission(self, session_id: str) -> AgentResponse:
        try:
            oppo_agent = self._make_oppo_agent()
            validation = oppo_agent.validate()
            validation = self._attach_oppo_app_check(oppo_agent, validation)
        except OppoError as exc:
            return AgentResponse(
                _format_error_message("提交检查失败", str(exc), ["检查 OPPO 配置和本地材料后重试。"]),
                {"intent": "submission_check", "error": str(exc)},
            )
        session = self.state_store.get_session(session_id)
        return AgentResponse(
            self._format_submission_check(validation, session),
            {"intent": "submission_check", "validation": validation, "session": session},
        )

    def _attach_oppo_app_check(self, oppo_agent: Any, validation: JsonDict) -> JsonDict:
        result = dict(validation)
        if not result.get("valid"):
            return result
        if not hasattr(oppo_agent, "ensure_app_created"):
            return result
        try:
            app_info = oppo_agent.ensure_app_created()
        except OppoError as exc:
            result["app_check"] = {"created": False, "error": str(exc)}
            result["valid"] = False
            return result
        result["app_check"] = {"created": True, "app_info": app_info}
        return result

    def submit_oppo(
        self,
        session_id: str,
        *,
        sender_id: str | None = None,
        confirm: bool = False,
        wait_task: bool = True,
        wait_review: bool = False,
        force: bool = False,
    ) -> AgentResponse:
        if not confirm:
            check = self.check_submission(session_id)
            return AgentResponse(
                "OPPO 自动提审未执行\n\n原因：\n"
                "- 正式提审会上传材料并提交到 OPPO，需要明确确认。\n\n"
                "当前检查：\n"
                + check.text
                + "\n\n下一步：\n- 确认无误后发送“确认提审 OPPO”。",
                {"intent": "oppo_submit", "confirmed": False, "check": check.data},
            )
        try:
            result = self._make_oppo_agent().submit(
                wait_task=wait_task,
                wait_review=wait_review,
                force=force,
            )
        except OppoError as exc:
            return AgentResponse(
                _format_error_message(
                    "OPPO 自动提审失败",
                    str(exc),
                    ["先发送“提交检查”确认本地材料和 OPPO 应用创建状态。", "修正后再发送“确认提审 OPPO”。"],
                ),
                {"intent": "oppo_submit", "confirmed": True, "error": str(exc)},
            )
        self.state_store.update_session(
            session_id,
            {
                "last_oppo_submit": result,
                "sender_id": sender_id,
            },
        )
        return AgentResponse(
            self._format_oppo_submit_result(result),
            {"intent": "oppo_submit", "confirmed": True, "result": result},
        )

    def build_remediation_checklist(
        self,
        session_id: str,
        sender_id: str | None = None,
    ) -> AgentResponse:
        session = self.state_store.get_session(session_id)
        analysis = session.get("last_rejection_analysis") or {}
        if not analysis:
            return AgentResponse(
                _format_error_message(
                    "整改清单暂不可用",
                    "当前会话还没有驳回分析结果。",
                    ["请先发送驳回截图。", "或发送“分析驳回：<原因>”。"],
                ),
                {"intent": "remediation_checklist", "missing": "rejection_analysis"},
            )
        items = self._build_remediation_items(analysis)
        self.state_store.update_session(
            session_id,
            {
                "remediation_checklist": items,
                "sender_id": sender_id,
            },
        )
        return AgentResponse(
            self._format_remediation_checklist(items, analysis),
            {"intent": "remediation_checklist", "items": items, "analysis": analysis},
        )

    def view_submission_config(self) -> AgentResponse:
        if not self.oppo_config_path:
            return AgentResponse(
                _format_error_message("无法查看提交配置", "还没有配置 OPPO 配置文件路径。"),
                {"intent": "view_submission_config", "missing": "config_path"},
            )
        try:
            text = format_config_summary(self.oppo_config_path)
        except ConfigEditError as exc:
            return AgentResponse(
                _format_error_message("查看提交配置失败", str(exc)),
                {"intent": "view_submission_config", "error": str(exc)},
            )
        packaging_summary = self._format_packaging_config_summary()
        if packaging_summary:
            text += "\n\n" + packaging_summary
        return AgentResponse(text, {"intent": "view_submission_config"})

    def stage_config_assignment(
        self,
        session_id: str,
        payload: str,
        sender_id: str | None = None,
    ) -> AgentResponse:
        if not self.oppo_config_path:
            return AgentResponse(
                _format_error_message("无法暂存配置修改", "还没有配置 OPPO 配置文件路径。"),
                {"intent": "stage_config_update", "missing": "config_path"},
            )
        try:
            patch = build_assignment_patch(payload)
        except ConfigEditError as exc:
            return AgentResponse(
                _format_error_message("配置修改暂存失败", str(exc)),
                {"intent": "stage_config_update", "error": str(exc)},
            )
        return self._stage_config_patch(session_id, patch, sender_id=sender_id)

    def stage_config_json(
        self,
        session_id: str,
        payload: str,
        sender_id: str | None = None,
    ) -> AgentResponse:
        if not self.oppo_config_path:
            return AgentResponse(
                _format_error_message("无法暂存批量配置修改", "还没有配置 OPPO 配置文件路径。"),
                {"intent": "stage_config_update", "missing": "config_path"},
            )
        try:
            patch = build_json_patch(payload)
        except ConfigEditError as exc:
            return AgentResponse(
                _format_error_message("批量配置暂存失败", str(exc)),
                {"intent": "stage_config_update", "error": str(exc)},
            )
        return self._stage_config_patch(session_id, patch, sender_id=sender_id)

    def confirm_config_update(
        self,
        session_id: str,
        sender_id: str | None = None,
    ) -> AgentResponse:
        session = self.state_store.get_session(session_id)
        pending = session.get("pending_config_patch") or {}
        if not pending:
            return AgentResponse(
                _format_error_message("没有待保存的配置修改", "当前会话没有暂存配置修改。", ["可以先发送“设置提交配置：字段=值”。"]),
                {"intent": "confirm_config_update", "missing": "pending_config_patch"},
            )
        if not self.oppo_config_path:
            return AgentResponse(
                _format_error_message("无法保存提交配置", "还没有配置 OPPO 配置文件路径。"),
                {"intent": "confirm_config_update", "missing": "config_path"},
            )
        try:
            result = apply_config_patch_to_targets(
                self.oppo_config_path,
                pending,
                packaging_config_path=self.packaging_config_path,
            )
        except ConfigEditError as exc:
            return AgentResponse(
                _format_error_message("保存配置失败", str(exc)),
                {"intent": "confirm_config_update", "error": str(exc)},
            )
        self.state_store.update_session(
            session_id,
            {
                "pending_config_patch": {},
                "last_config_update": result,
                "sender_id": sender_id,
            },
        )
        self._reload_packaging_agent()
        check = self.check_submission(session_id)
        result_items = result.get("results") or []
        backup_names = [Path(item["backup_path"]).name for item in result_items if item.get("backup_path")]
        return AgentResponse(
            "配置已保存\n"
            "\n保存结果：\n"
            + (f"- 备份：{'、'.join(backup_names)}\n" if backup_names else "- 备份：未生成\n")
            + "\n自动提交检查：\n"
            + check.text,
            {"intent": "confirm_config_update", "result": result, "check": check.data},
        )

    def cancel_config_update(
        self,
        session_id: str,
        sender_id: str | None = None,
    ) -> AgentResponse:
        self.state_store.update_session(
            session_id,
            {
                "pending_config_patch": {},
                "sender_id": sender_id,
            },
        )
        return AgentResponse(
            "配置修改已取消\n\n结果：\n- 已取消当前会话待保存的配置修改。",
            {"intent": "cancel_config_update"},
        )

    def stage_file_move(
        self,
        session_id: str,
        *,
        source_path: str = "",
        target_dir: str = "",
        target_path: str = "",
        operation: str = "",
        overwrite: bool = False,
        text: str = "",
        sender_id: str | None = None,
    ) -> AgentResponse:
        session = self.state_store.get_session(session_id)
        try:
            plan = self._build_file_move_plan(
                session,
                source_path=source_path,
                target_dir=target_dir,
                target_path=target_path,
                operation=operation,
                overwrite=overwrite,
                text=text,
            )
        except ValueError as exc:
            return AgentResponse(
                _format_error_message(
                    "文件移动暂存失败",
                    str(exc),
                    [
                        "请明确源文件路径和目标目录，或先上传文件/完成打包后再说“把这个 APK 放到项目根目录下的 release 里面”。",
                    ],
                ),
                {"intent": "stage_file_move", "error": str(exc)},
            )
        self.state_store.update_session(
            session_id,
            {
                "pending_file_move": plan,
                "sender_id": sender_id,
            },
        )
        action = "移动" if plan["operation"] == "move" else "复制"
        overwrite_text = "是" if plan.get("overwrite") else "否"
        return AgentResponse(
            "文件操作已暂存，尚未执行\n"
            "\n计划：\n"
            f"- 操作：{action}\n"
            f"- 源文件：{plan['source_path']}\n"
            f"- 目标文件：{plan['target_path']}\n"
            f"- 覆盖已有文件：{overwrite_text}\n"
            "\n下一步：\n"
            "- 发送“确认移动文件”后才会执行。\n"
            "- 发送“取消移动文件”可放弃本次操作。",
            {"intent": "stage_file_move", "pending_file_move": plan},
        )

    def confirm_file_move(
        self,
        session_id: str,
        sender_id: str | None = None,
    ) -> AgentResponse:
        session = self.state_store.get_session(session_id)
        pending = session.get("pending_file_move") or {}
        if not pending:
            return AgentResponse(
                _format_error_message(
                    "没有待确认的文件操作",
                    "当前会话没有暂存文件移动/复制操作。",
                    ["可以先发送“把这个 APK 放到项目根目录下的 release 里面”。"],
                ),
                {"intent": "confirm_file_move", "missing": "pending_file_move"},
            )
        try:
            result = self._execute_file_move_plan(pending)
        except ValueError as exc:
            return AgentResponse(
                _format_error_message("文件操作失败", str(exc), ["检查源文件是否存在、目标目录是否在项目目录内，然后重新暂存。"]),
                {"intent": "confirm_file_move", "error": str(exc), "pending_file_move": pending},
            )
        self.state_store.update_session(
            session_id,
            {
                "pending_file_move": {},
                "last_file_move": result,
                "sender_id": sender_id,
            },
        )
        action = "移动" if result["operation"] == "move" else "复制"
        return AgentResponse(
            "文件操作已完成\n"
            "\n结果：\n"
            f"- 操作：{action}\n"
            f"- 源文件：{result['source_path']}\n"
            f"- 目标文件：{result['target_path']}",
            {"intent": "confirm_file_move", "result": result},
        )

    def cancel_file_move(
        self,
        session_id: str,
        sender_id: str | None = None,
    ) -> AgentResponse:
        self.state_store.update_session(
            session_id,
            {
                "pending_file_move": {},
                "sender_id": sender_id,
            },
        )
        return AgentResponse(
            "文件操作已取消\n\n结果：\n- 已取消当前会话待确认的文件移动/复制操作。",
            {"intent": "cancel_file_move"},
        )

    def bind_last_upload_as_material(
        self,
        session_id: str,
        label: str,
        sender_id: str | None = None,
    ) -> AgentResponse:
        if not self.oppo_config_path:
            return AgentResponse(
                _format_error_message("无法绑定材料", "还没有配置 OPPO 配置文件路径。"),
                {"intent": "bind_material", "missing": "config_path"},
            )
        session = self.state_store.get_session(session_id)
        upload = session.get("last_upload") or {}
        try:
            result = bind_uploaded_material(
                config_path=self.oppo_config_path,
                upload=upload,
                label=label,
            )
        except MaterialBindError as exc:
            return AgentResponse(
                _format_error_message("绑定材料失败", str(exc)),
                {"intent": "bind_material", "error": str(exc)},
            )
        self.state_store.update_session(
            session_id,
            {
                "last_bound_material": result,
                "sender_id": sender_id,
            },
        )
        check = self.check_submission(session_id)
        material_name = _material_name(result.get("material_type"), result.get("index"))
        return AgentResponse(
            "材料已绑定\n\n绑定结果：\n"
            f"- 类型：{material_name}\n"
            f"- 保存到：{result['target_path']}\n"
            f"- 配置项：{', '.join(result['config_patch'].keys())}\n\n"
            "自动提交检查：\n"
            + check.text,
            {"intent": "bind_material", "result": result, "check": check.data},
        )

    def index_submission_materials(
        self,
        session_id: str,
        *,
        app_name: str = "",
        pkg_name: str = "",
        materials_root: str = "",
        sender_id: str | None = None,
        source_text: str = "",
    ) -> AgentResponse:
        if not self.oppo_config_path:
            return AgentResponse(
                _format_error_message("无法索引上架资源", "还没有配置 OPPO 配置文件路径。"),
                {"intent": "index_materials", "missing": "config_path"},
            )
        parsed = self._parse_material_index_request(source_text or app_name or pkg_name)
        app_name = app_name.strip() or parsed.get("app_name", "")
        pkg_name = pkg_name.strip() or parsed.get("pkg_name", "")
        materials_root = materials_root.strip() or parsed.get("materials_root", "") or str(self._materials_root_path() or "")
        if not app_name and not pkg_name:
            remembered = self._default_app_info(session_id)
            app_name = str(remembered.get("app_name") or "")
            pkg_name = str(remembered.get("pkg_name") or "")
        if not app_name and not pkg_name:
            return AgentResponse(
                _format_error_message(
                    "索引上架资源缺少应用",
                    "没有识别到应用名或包名。",
                    ["请发送“索引上架资源：八年级语文下册”或“索引上架资源：com.pelbs.book1067”。"],
                ),
                {"intent": "index_materials", "missing": "app_name"},
            )
        if not materials_root:
            return AgentResponse(
                _format_error_message(
                    "索引上架资源缺少目录",
                    "还没有配置上架资源目录。",
                    ["可以在 packaging.materials_root 配置，或发送“索引上架资源：应用名 路径 D:\\\\Workship\\\\Pelbs\\\\AppMaket\\\\上架资源”。"],
                ),
                {"intent": "index_materials", "missing": "materials_root"},
            )
        try:
            suggestion = suggest_submission_materials(
                root=materials_root,
                app_name=app_name,
                pkg_name=pkg_name,
                packlist_snapshot=self._packaging_packlist_snapshot(),
                config_path=self.oppo_config_path,
                max_screenshots=5,
            )
        except MaterialIndexError as exc:
            return AgentResponse(
                _format_error_message("索引上架资源失败", str(exc)),
                {"intent": "index_materials", "error": str(exc)},
            )
        patch = suggestion.patch
        if not patch:
            return AgentResponse(
                _format_error_message(
                    "未找到可填充材料",
                    f"没有为“{app_name or pkg_name}”匹配到可写入提交配置的材料。",
                    ["换一个更完整的应用名或确认上架资源目录是否正确。"],
                ),
                {"intent": "index_materials", "suggestion": suggestion.to_dict(), "patch": {}},
            )
        staged = self._stage_config_patch(session_id, patch, sender_id=sender_id)
        session_patch = dict((self.state_store.get_session(session_id).get("pending_config_patch") or {}))
        return AgentResponse(
            self._format_material_index_suggestion(suggestion.to_dict(), session_patch),
            {"intent": "index_materials", "suggestion": suggestion.to_dict(), "patch": session_patch, "staged": staged.data},
        )

    def _handle_semantic_intent(
        self,
        session_id: str,
        text: str,
        sender_id: str | None = None,
    ) -> AgentResponse | None:
        lowered = text.lower()

        if self._looks_like_project_logic_request(lowered):
            return AgentResponse(PROJECT_LOGIC_TEXT, {"intent": "project_logic", "semantic": True})

        if self._looks_like_help_request(lowered):
            return AgentResponse(HELP_TEXT, {"intent": "help", "semantic": True})

        if self._looks_like_clear_all_request(lowered):
            return self.clear_all_state()

        if self._looks_like_clear_session_request(lowered):
            return self.clear_session_state(session_id)

        if self._looks_like_oppo_app_list_request(lowered):
            return self.query_oppo_app_list(session_id, sender_id=sender_id)

        if self._looks_like_oppo_status_request(lowered):
            return self.query_oppo_status(text, session_id=session_id, sender_id=sender_id)

        if self._looks_like_submission_check_request(lowered):
            return self.check_submission(session_id)

        if self._looks_like_oppo_submit_confirmation(lowered):
            return self.submit_oppo(session_id, sender_id=sender_id, confirm=True)

        if self._looks_like_submit_prepare_request(lowered):
            return self.submit_oppo(session_id, sender_id=sender_id, confirm=False)

        semantic_market_intent = self._parse_market_semantic_intent(text, session_id)
        if semantic_market_intent:
            intent, query = semantic_market_intent
            if not query:
                return AgentResponse(
                    "我理解你想做竞品分析，但还缺关键词。可以先发送“记录应用：应用名 / 包名 / 版本号”，或直接说“帮我找英语四级单词的竞品”。",
                    {"intent": intent, "missing": "app_name", "semantic": True},
                )
            if intent == "market_download_snapshot":
                return self.record_competitor_downloads(
                    session_id,
                    query,
                    sender_id=sender_id,
                    source_text=text,
                    target_stores=_extract_target_market_stores(text),
                )
            return self.search_competitors(
                session_id,
                query,
                sender_id=sender_id,
                source_text=text,
                target_stores=_extract_target_market_stores(text),
            )

        if self._looks_like_last_image_analysis_request(lowered):
            session = self.state_store.get_session(session_id)
            image_text = self._get_last_image_text(session)
            if not image_text:
                return AgentResponse(
                    _format_error_message("图片分析缺少 OCR 文本", "最近图片还没有可用于分析的 OCR 文本。", ["请先发送一张包含驳回原因的截图。"]),
                    {"intent": "analyze_last_image", "missing": "ocr_text", "semantic": True},
                )
            return self.analyze_rejection_text(session_id, image_text, sender_id=sender_id)

        if self._looks_like_rejection_analysis_request(lowered):
            reason = self._extract_payload(text) or text
            if not any(marker in reason for marker in ("驳回", "拒绝", "不通过", "相似度", "请勿重复提交")):
                return AgentResponse(
                    "我理解你想分析驳回原因，但还缺具体驳回内容。请发送“分析驳回：<审核不通过原因>”。",
                    {"intent": "analyze_rejection", "missing": "reason", "semantic": True},
                )
            return self.analyze_rejection_text(session_id, reason, sender_id=sender_id)

        if self._looks_like_remediation_request(lowered):
            return self.build_remediation_checklist(session_id, sender_id=sender_id)

        if self._looks_like_view_config_request(lowered):
            return self.view_submission_config()

        if self._looks_like_confirm_config_request(lowered):
            return self.confirm_config_update(session_id, sender_id=sender_id)

        if self._looks_like_cancel_config_request(lowered):
            return self.cancel_config_update(session_id, sender_id=sender_id)

        if self._looks_like_config_update_request(lowered):
            payload = self._extract_payload(text) or self._extract_assignment_payload(text)
            packaging_script_update = self._extract_packaging_script_update(text)
            if packaging_script_update:
                payload = f"packaging.script={packaging_script_update}"
            if not payload:
                return AgentResponse(
                    "我理解你想修改提交配置，但还缺字段和值。可以说“把 submission.version_code=101 暂存一下”。",
                    {"intent": "stage_config_update", "missing": "assignment", "semantic": True},
                )
            if payload.lstrip().startswith("{"):
                return self.stage_config_json(session_id, payload, sender_id=sender_id)
            return self.stage_config_assignment(session_id, payload, sender_id=sender_id)

        if self._looks_like_bind_material_request(lowered):
            label = self._extract_payload(text) or self._extract_material_label(text)
            return self.bind_last_upload_as_material(session_id, label, sender_id=sender_id)

        if self._looks_like_record_app_request(lowered):
            payload = self._extract_payload(text) or self._extract_record_app_payload(text)
            app_info = self._parse_app_info(payload)
            self.state_store.update_session(
                session_id,
                {
                    "app_info": app_info,
                    "sender_id": sender_id,
                },
            )
            return AgentResponse(
                self._format_record_app_info(app_info),
                {"intent": "record_app", "app_info": app_info, "semantic": True},
            )

        if self._looks_like_session_status_request(lowered):
            session = self.state_store.get_session(session_id)
            return AgentResponse(self._format_session(session), {"intent": "status", "session": session, "semantic": True})

        fallback_package_response = self._handle_packaging_fallback(session_id, text)
        if fallback_package_response:
            return fallback_package_response

        return None

    @staticmethod
    def _should_apply_llm_before_rules(text: str) -> bool:
        if _looks_like_market_followup(text):
            return False
        if text in {
            "帮助",
            "help",
            "/help",
            "清空记录",
            "清空当前记录",
            "清空当前状态",
            "重置记录",
            "重置当前记录",
            "重置当前会话",
            "清空所有记录",
            "清空全部记录",
            "重置所有记录",
            "重置全部会话",
            "状态",
            "当前状态",
            "整改清单",
            "生成整改清单",
            "待办清单",
            "查看提交配置",
            "查看配置",
            "提交配置",
            "确认保存配置",
            "保存配置",
            "确认配置",
            "取消保存配置",
            "取消配置修改",
            "放弃配置修改",
            "提交检查",
            "校验配置",
            "检查提交",
            "准备提交",
        }:
            return False
        return not text.startswith(
            (
                "分析驳回",
                "设置提交配置",
                "批量设置提交配置",
                "绑定材料",
                "搜索竞品",
                "竞品搜索",
                "搜索应用商店",
                "找竞品",
                "记录竞品下载",
                "记录竞品月报",
                "月度记录竞品",
                "记录应用",
                "查询审核状态",
            )
        )

    def _interpret_with_llm(
        self,
        session_id: str,
        text: str,
        sender_id: str | None = None,
    ) -> JsonDict | None:
        if not self.llm_client:
            return None
        context = self._llm_context(session_id)
        self._trace_event(session_id, "llm_interpret_request", {"message": text, "context": context})
        try:
            decision = self.llm_client.interpret(text, context)
        except Exception as exc:
            self._trace_event(session_id, "llm_interpret_error", {"error": str(exc)})
            return {"intent": "llm_error", "error": str(exc)}
        if not isinstance(decision, dict):
            self._trace_event(session_id, "llm_interpret_invalid", {"result": str(decision)})
            return {"intent": "llm_error", "error": "LLM decision must be a JSON object"}
        self._trace_event(session_id, "llm_interpret_response", {"decision": decision})
        self._store_llm_memories(session_id, decision, sender_id=sender_id)
        return decision

    def _response_from_llm_decision(
        self,
        session_id: str,
        text: str,
        decision: JsonDict | None,
        sender_id: str | None = None,
        *,
        allow_chat: bool,
    ) -> AgentResponse | None:
        if not decision:
            return None
        intent = str(decision.get("intent") or "unknown").strip()
        if intent == "llm_error":
            if allow_chat:
                return AgentResponse(
                    _format_error_message(
                        "大模型理解失败",
                        decision.get("error"),
                        ["可以换个说法再发一次，或发送“帮助”查看可用场景。"],
                    ),
                    {"intent": "llm_error", "error": str(decision.get("error") or "")},
                )
            return None
        confidence = _optional_confidence(decision.get("confidence"))
        if confidence is not None and confidence < 0.45:
            if allow_chat:
                reply = str(decision.get("reply") or "").strip()
                return AgentResponse(
                    _format_llm_free_reply(
                        "unknown",
                        reply or "我不太确定你的意思。可以换个说法，或发送“帮助”查看可用能力。",
                    ),
                    {"intent": "unknown", "llm": decision},
                )
            return None
        if intent in {"unknown", "disabled"}:
            return None if not allow_chat else self._llm_reply_or_none(intent, decision)
        if intent == "chat" and not allow_chat:
            return None
        if intent == "remember" and not allow_chat:
            return None
        response = self._dispatch_llm_intent(session_id, text, decision, sender_id=sender_id)
        if response:
            response.data["llm"] = decision
            return response
        return self._llm_reply_or_none(intent, decision) if allow_chat else None

    @staticmethod
    def _llm_reply_or_none(intent: str, decision: JsonDict) -> AgentResponse | None:
        reply = str(decision.get("reply") or "").strip()
        if not reply:
            return None
        return AgentResponse(_format_llm_free_reply(intent, reply), {"intent": intent, "llm": decision})

    def _llm_context(self, session_id: str) -> JsonDict:
        session = self.state_store.get_session(session_id)
        conversation = session.get("conversation_history") or []
        recent_conversation = []
        for item in conversation[-20:]:
            if not isinstance(item, dict):
                continue
            recent_conversation.append(
                {
                    "user": _shorten(item.get("user"), 160),
                    "assistant": _shorten(item.get("assistant"), 220),
                    "intent": str(item.get("intent") or ""),
                }
            )
        last_package_lookup = session.get("last_package_lookup") or {}
        lookup_matches = last_package_lookup.get("matches") or []
        compact_lookup = {}
        if last_package_lookup:
            compact_lookup = {
                "query": str(last_package_lookup.get("query") or ""),
                "page_size": int(last_package_lookup.get("page_size") or 0),
                "next_offset": int(last_package_lookup.get("next_offset") or 0),
                "match_count": len(lookup_matches),
                "sample_matches": [
                    {
                        "app_name": str(item.get("app_name") or ""),
                        "pkg_name": str(item.get("pkg_name") or ""),
                        "channel": str(item.get("channel") or ""),
                        "version_code": str(item.get("version_code") or ""),
                    }
                    for item in lookup_matches[:5]
                    if isinstance(item, dict)
                ],
            }
        last_market_search = session.get("last_market_search") or {}
        compact_market_search = {}
        if last_market_search:
            market_apps = last_market_search.get("apps") or []
            compact_market_search = {
                "query": str(last_market_search.get("query") or ""),
                "result_count": len(market_apps),
                "apps": [
                    {
                        "store": str(item.get("store") or ""),
                        "name": str(item.get("name") or ""),
                        "downloads_text": str(item.get("downloads_text") or ""),
                    }
                    for item in market_apps[:4]
                    if isinstance(item, dict)
                ],
            }
        last_package_result = session.get("last_package_result") or {}
        compact_package_result = {}
        if last_package_result:
            resolved_package = last_package_result.get("resolved_package") or {}
            compact_package_result = {
                "latest_apks": _normalize_string_list(last_package_result.get("latest_apks") or [])[:3],
                "resolved_package": {
                    "app_name": str(resolved_package.get("app_name") or ""),
                    "pkg_name": str(resolved_package.get("pkg_name") or ""),
                    "channel": str(resolved_package.get("channel") or ""),
                    "version_code": str(resolved_package.get("version_code") or ""),
                    "version_name": str(resolved_package.get("version_name") or ""),
                },
            }
        last_upload = session.get("last_upload") or {}
        compact_last_upload = {}
        if last_upload:
            compact_last_upload = {
                "file_name": str(last_upload.get("file_name") or ""),
                "resource_type": str(last_upload.get("resource_type") or ""),
                "path": str(last_upload.get("path") or ""),
            }
        app_info = session.get("app_info") or {}
        compact_status = session.get("last_oppo_status") or {}
        compact_status = {
            "pkg_name": str(compact_status.get("pkg_name") or ""),
            "version_code": str(compact_status.get("version_code") or ""),
            "review_state": str(compact_status.get("review_state") or ""),
            "task_state": str((compact_status.get("task") or {}).get("task_state") or ""),
            "task_error": _shorten((compact_status.get("task") or {}).get("err_msg"), 120),
            "app_name": str((compact_status.get("app_info") or {}).get("app_name") or ""),
            "audit_status_name": str((compact_status.get("app_info") or {}).get("audit_status_name") or ""),
        }
        return {
            "session": {
                "app_info": {
                    "app_name": str(app_info.get("app_name") or ""),
                    "pkg_name": str(app_info.get("pkg_name") or ""),
                    "version_code": str(app_info.get("version_code") or ""),
                },
                "last_package_lookup": compact_lookup,
                "last_market_search": compact_market_search,
                "last_market_search_request": _json_safe(session.get("last_market_search_request") or {}),
                "last_market_filtered_names": _json_safe(session.get("last_market_filtered_names") or {}),
                "last_package_result": compact_package_result,
                "last_upload": compact_last_upload,
                "last_oppo_status": compact_status,
                "pending_config_patch": _json_safe(session.get("pending_config_patch") or {}),
                "pending_file_move": _json_safe(session.get("pending_file_move") or {}),
            },
            "recent_conversation": recent_conversation,
            "long_term_memory": self._structured_long_term_memory(session_id),
            "default_config": {
                "app_info": self._config_app_info(),
                "image_analysis": {
                    "ocr_configured": bool(self._configured_image_analysis_url("ocr_url")),
                    "image2_configured": bool(self._configured_image_analysis_url("image2_url")),
                },
            },
            "preferences": {
                "market_stores": self._market_store_preferences(session_id),
            },
            "packaging": {
                "config_path": str(self.packaging_config_path) if self.packaging_config_path else "",
                "project_dir": str(getattr(getattr(self.packaging_agent, "settings", None), "project_dir", "") or ""),
                "script_path": str(getattr(getattr(self.packaging_agent, "settings", None), "script_path", "") or ""),
                "batch_file": str(getattr(getattr(self.packaging_agent, "settings", None), "batch_file", "") or ""),
                "packlist_scan_file": str(getattr(getattr(self.packaging_agent, "settings", None), "packlist_scan_file", "") or ""),
            },
            "supported_market_stores": [
                {"store": store, "label": label}
                for store, label in SUPPORTED_MARKET_STORES
            ],
        }

    def _handle_market_followup(
        self,
        session_id: str,
        text: str,
        sender_id: str | None = None,
    ) -> AgentResponse | None:
        if not _looks_like_market_followup(text):
            return None
        request = self._last_market_search_request(session_id)
        query = str(request.get("query") or "").strip()
        if not query:
            return None
        target_stores = _extract_target_market_stores(text)
        if _looks_like_other_market_stores_request(text):
            previous_stores = set(_normalize_store_list(request.get("target_stores") or []))
            if previous_stores:
                target_stores = [
                    store
                    for store, _ in SUPPORTED_MARKET_STORES
                    if store not in previous_stores and store not in {"qimai_data", "appark_data"}
                ]
        if not target_stores:
            target_stores = _normalize_store_list(request.get("target_stores") or [])
        if _looks_like_market_download_request(text):
            return self.record_competitor_downloads(
                session_id,
                query,
                sender_id=sender_id,
                source_text=str(request.get("source_text") or text),
                exact_match=bool(request.get("exact_match")),
                exclude_terms=_normalize_string_list(request.get("exclude_terms") or []),
                include_terms=_normalize_string_list(request.get("include_terms") or []),
                target_stores=target_stores,
            )
        return self.search_competitors(
            session_id,
            query,
            sender_id=sender_id,
            source_text=str(request.get("source_text") or text),
            exact_match=bool(request.get("exact_match")),
            exclude_terms=_normalize_string_list(request.get("exclude_terms") or []),
            include_terms=_normalize_string_list(request.get("include_terms") or []),
            target_stores=target_stores,
        )

    def _last_market_search_request(self, session_id: str) -> JsonDict:
        session = self.state_store.get_session(session_id)
        request = dict(session.get("last_market_search_request") or {})
        if request.get("query"):
            return request
        search = session.get("last_market_search") or {}
        filtered = session.get("last_market_filtered_names") or {}
        if not search.get("query"):
            return {}
        stores = []
        for status in search.get("store_statuses") or []:
            if isinstance(status, dict) and status.get("store"):
                stores.append(status.get("store"))
        return {
            "query": search.get("query"),
            "source_text": "",
            "exact_match": filtered.get("exact_match", False),
            "exclude_terms": filtered.get("exclude_terms") or [],
            "include_terms": filtered.get("include_terms") or [],
            "target_stores": _normalize_store_list(stores),
        }

    def _answer_recent_context_question(self, session_id: str, text: str) -> AgentResponse | None:
        if not _looks_like_recent_context_question(text):
            return None
        session = self.state_store.get_session(session_id)
        history = [item for item in (session.get("conversation_history") or []) if item.get("user")]
        if history:
            recent = history[-3:]
            lines = ["最近对话", "", "最近你发给我的内容："]
            for item in recent:
                lines.append(f"- {item.get('user')}")
            return AgentResponse("\n".join(lines), {"intent": "recent_context", "history": recent})
        app_info = session.get("app_info") or {}
        if app_info:
            return AgentResponse(
                "最近对话\n\n说明：\n- 当前会话没有可回放的逐轮对话记录。\n\n结构化应用信息：\n"
                f"- 应用名：{app_info.get('app_name') or '未记录应用名'}\n"
                f"- 包名：{app_info.get('pkg_name') or '未记录包名'}\n"
                f"- 版本号：{app_info.get('version_code') or '未记录版本号'}",
                {"intent": "recent_context", "missing": "conversation_history", "app_info": app_info},
            )
        return AgentResponse(
            "最近对话\n\n说明：\n- 当前会话还没有可回放的上一轮信息。",
            {"intent": "recent_context", "missing": "conversation_history"},
        )

    def _dispatch_llm_intent(
        self,
        session_id: str,
        text: str,
        decision: JsonDict,
        sender_id: str | None = None,
    ) -> AgentResponse | None:
        intent = str(decision.get("intent") or "unknown").strip()
        if intent == "help":
            return AgentResponse(HELP_TEXT, {"intent": "help"})
        if intent == "clear_session_state":
            return self.clear_session_state(session_id)
        if intent == "clear_all_state":
            return self.clear_all_state()
        if intent == "status":
            session = self.state_store.get_session(session_id)
            return AgentResponse(self._format_session(session), {"intent": "status", "session": session})
        if intent == "chat":
            return AgentResponse(
                _format_llm_free_reply(
                    "chat",
                    str(decision.get("reply") or "我在。你可以直接说要打包、查审核、分析驳回、看竞品或改配置。").strip(),
                ),
                {"intent": "chat"},
            )
        if intent == "remember":
            reply = str(decision.get("reply") or "我记住了。").strip()
            return AgentResponse(
                _format_llm_free_reply("remember", reply),
                {"intent": "remember", "memories": decision.get("memories") or []},
            )
        if intent == "record_app":
            app_info = decision.get("app_info") if isinstance(decision.get("app_info"), dict) else {}
            if not app_info:
                app_info = self._parse_app_info(self._extract_payload(text))
            self.state_store.update_session(
                session_id,
                {
                    "app_info": {
                        "app_name": str(app_info.get("app_name") or ""),
                        "pkg_name": str(app_info.get("pkg_name") or ""),
                        "version_code": str(app_info.get("version_code") or ""),
                    },
                    "sender_id": sender_id,
                },
            )
            stored = self.state_store.get_session(session_id).get("app_info") or {}
            return AgentResponse(
                self._format_record_app_info(stored),
                {"intent": "record_app", "app_info": stored},
            )
        if intent == "analyze_rejection":
            reason = str(decision.get("reason") or self._extract_payload(text) or text).strip()
            if not reason:
                return AgentResponse("请把审核驳回原因发给我，我再分析。", {"intent": intent, "missing": "reason"})
            return self.analyze_rejection_text(session_id, reason, sender_id=sender_id)
        if intent == "analyze_last_image":
            session = self.state_store.get_session(session_id)
            image_text = self._get_last_image_text(session)
            if not image_text:
                return AgentResponse(
                    _format_error_message("图片分析缺少 OCR 文本", "最近图片还没有可用于分析的 OCR 文本。", ["请先发送一张包含驳回原因的截图。"]),
                    {"intent": intent, "missing": "ocr_text"},
                )
            return self.analyze_rejection_text(session_id, image_text, sender_id=sender_id)
        if intent == "remediation_checklist":
            return self.build_remediation_checklist(session_id, sender_id=sender_id)
        if intent == "oppo_status":
            if self._looks_like_oppo_app_list_request(text.lower()):
                return self.query_oppo_app_list(session_id, sender_id=sender_id)
            version_code = str(decision.get("version_code") or "").strip()
            return self.query_oppo_status(
                f"查询审核状态：{version_code}" if version_code else text,
                session_id=session_id,
                sender_id=sender_id,
            )
        if intent == "submission_check":
            return self.check_submission(session_id)
        if intent == "oppo_submit":
            confirm = _optional_bool(decision.get("confirm")) or self._looks_like_oppo_submit_confirmation(text.lower())
            return self.submit_oppo(
                session_id,
                sender_id=sender_id,
                confirm=confirm,
                wait_task=bool(_optional_bool(decision.get("wait_task"))),
                wait_review=bool(_optional_bool(decision.get("wait_review"))),
                force=bool(_optional_bool(decision.get("force"))),
            )
        if intent == "submit_checklist":
            return self.submit_oppo(session_id, sender_id=sender_id, confirm=False)
        if intent in {"package_apk", "batch_package"}:
            package_response = self._run_packaging_intent(session_id, text, decision)
            if package_response:
                return package_response
        if intent == "package_lookup":
            return self._run_packaging_lookup(session_id, text, decision)
        if intent == "view_submission_config":
            return self.view_submission_config()
        if intent == "stage_config_update":
            if self._looks_like_file_move_request(text):
                return self.stage_file_move(session_id, text=text, sender_id=sender_id)
            assignment = str(decision.get("config_assignment") or self._extract_assignment_payload(text)).strip()
            packaging_script_update = self._extract_packaging_script_update(text)
            if packaging_script_update and (
                not assignment or "package_script_path" in assignment or "packaging.script" not in assignment
            ):
                assignment = f"packaging.script={packaging_script_update}"
            if not assignment:
                return AgentResponse("要改哪个配置？请给我类似 submission.version_code=10002 的字段和值。", {"intent": intent, "missing": "assignment"})
            if assignment.lstrip().startswith("{"):
                return self.stage_config_json(session_id, assignment, sender_id=sender_id)
            return self.stage_config_assignment(session_id, assignment, sender_id=sender_id)
        if intent == "confirm_config_update":
            return self.confirm_config_update(session_id, sender_id=sender_id)
        if intent == "cancel_config_update":
            return self.cancel_config_update(session_id, sender_id=sender_id)
        if intent == "stage_file_move":
            return self.stage_file_move(
                session_id,
                source_path=str(decision.get("source_path") or ""),
                target_dir=str(decision.get("target_dir") or ""),
                target_path=str(decision.get("target_path") or ""),
                operation=str(decision.get("operation") or ""),
                overwrite=bool(_optional_bool(decision.get("overwrite"))),
                text=text,
                sender_id=sender_id,
            )
        if intent == "confirm_file_move":
            return self.confirm_file_move(session_id, sender_id=sender_id)
        if intent == "cancel_file_move":
            return self.cancel_file_move(session_id, sender_id=sender_id)
        if intent == "bind_material":
            label = str(decision.get("material_label") or self._extract_material_label(text)).strip()
            return self.bind_last_upload_as_material(session_id, label, sender_id=sender_id)
        if intent == "index_materials":
            return self.index_submission_materials(
                session_id,
                app_name=str(decision.get("app_name") or ""),
                pkg_name=str(decision.get("pkg_name") or ""),
                materials_root=str(decision.get("materials_root") or ""),
                sender_id=sender_id,
            )
        if intent == "market_store_preference":
            disable = _normalize_store_list(decision.get("disable_stores") or decision.get("disabled_stores"))
            enable = _normalize_store_list(decision.get("enable_stores") or decision.get("enabled_stores"))
            if not disable and not enable:
                parsed = self._parse_market_store_preference(text)
                if parsed:
                    disable, enable = parsed
            if not disable and not enable:
                return AgentResponse(
                    "要调整哪个商店？例如“默认不查询 Google Play”或“恢复查询 Google Play”。",
                    {"intent": intent, "missing": "store"},
                )
            return self._apply_market_store_preference(
                session_id,
                disable_stores=disable,
                enable_stores=enable,
                sender_id=sender_id,
            )
        if intent in {"market_search", "market_download_snapshot"}:
            # 优先使用 app_name，兼容旧的 query 参数
            query = str(decision.get("app_name") or decision.get("query") or self._extract_market_semantic_query(text, session_id)).strip()
            if not query:
                return AgentResponse("要查哪个应用方向的竞品？例如“英语四级单词”。", {"intent": intent, "missing": "app_name"})
            research_response = self._handle_app_store_data_platform_research(text, query, llm_intent=intent)
            if research_response:
                return research_response
            if intent == "market_download_snapshot":
                return self.record_competitor_downloads(
                    session_id,
                    query,
                    sender_id=sender_id,
                    source_text=text,
                    exact_match=bool(decision.get("exact_match")),
                    exclude_terms=_normalize_string_list(decision.get("exclude_terms") or []),
                    include_terms=_normalize_string_list(decision.get("include_terms") or []),
                    target_stores=_normalize_store_list(decision.get("target_stores") or _extract_target_market_stores(text)),
                )
            return self.search_competitors(
                session_id,
                query,
                sender_id=sender_id,
                source_text=text,
                exact_match=bool(decision.get("exact_match")),
                exclude_terms=_normalize_string_list(decision.get("exclude_terms") or []),
                include_terms=_normalize_string_list(decision.get("include_terms") or []),
                target_stores=_normalize_store_list(decision.get("target_stores") or _extract_target_market_stores(text)),
            )
        return None

    def _store_llm_memories(
        self,
        session_id: str,
        decision: JsonDict,
        sender_id: str | None = None,
    ) -> None:
        memory_patch = self._memory_patch_from_decision(decision)
        if not memory_patch:
            return
        session = self.state_store.get_session(session_id)
        existing = [str(item).strip() for item in session.get("agent_memory") or [] if str(item).strip()]
        structured = self._structured_long_term_memory(session_id)
        memories = memory_patch.get("notes") or []
        merged = existing[:]
        for item in memories:
            if item not in merged:
                merged.append(item)
        for item in memory_patch.get("notes") or []:
            if item not in structured["notes"]:
                structured["notes"].append(item)
        structured["notes"] = structured["notes"][-30:]

        app_info = memory_patch.get("app_info") or {}
        if app_info:
            structured["app_info"].update({key: value for key, value in app_info.items() if value})

        preferences = memory_patch.get("preferences") or {}
        if preferences:
            structured["preferences"].update(preferences)

        submission = memory_patch.get("submission") or {}
        if submission:
            structured["submission"].update(submission)

        self.state_store.update_session(
            session_id,
            {
                "agent_memory": merged[-30:],
                "long_term_memory": structured,
                "sender_id": sender_id,
            },
        )

    def _structured_long_term_memory(self, session_id: str) -> JsonDict:
        session = self.state_store.get_session(session_id)
        raw = session.get("long_term_memory") or {}
        memory = {
            "notes": [str(item).strip() for item in raw.get("notes") or [] if str(item).strip()],
            "app_info": dict(raw.get("app_info") or {}),
            "submission": dict(raw.get("submission") or {}),
            "preferences": dict(raw.get("preferences") or {}),
        }
        for item in session.get("agent_memory") or []:
            note = str(item).strip()
            if note and note not in memory["notes"]:
                memory["notes"].append(note)
        session_app = session.get("app_info") or {}
        if session_app:
            memory["app_info"].update({key: value for key, value in session_app.items() if value})
        market_preferences = self._market_store_preferences(session_id)
        if market_preferences.get("disabled_stores"):
            memory["preferences"]["market_stores"] = market_preferences
        return memory

    @staticmethod
    def _memory_patch_from_decision(decision: JsonDict) -> JsonDict:
        patch: JsonDict = {}
        notes: list[str] = []
        for item in decision.get("memories") or []:
            if isinstance(item, dict):
                note = str(item.get("text") or item.get("note") or "").strip()
                category = str(item.get("category") or "").strip()
                if note:
                    notes.append(f"{category}: {note}" if category else note)
            else:
                note = str(item).strip()
                if note:
                    notes.append(note)
        if notes:
            patch["notes"] = notes

        app_info = decision.get("app_info") if isinstance(decision.get("app_info"), dict) else {}
        app_info = {
            "app_name": str(app_info.get("app_name") or "").strip(),
            "pkg_name": str(app_info.get("pkg_name") or "").strip(),
            "version_code": str(app_info.get("version_code") or "").strip(),
        }
        app_info = {key: value for key, value in app_info.items() if value}
        if app_info:
            patch["app_info"] = app_info

        preferences: JsonDict = {}
        disable_stores = _normalize_store_list(decision.get("disable_stores") or decision.get("disabled_stores"))
        enable_stores = _normalize_store_list(decision.get("enable_stores") or decision.get("enabled_stores"))
        if disable_stores or enable_stores:
            preferences["market_stores"] = {
                "disable_stores": disable_stores,
                "enable_stores": enable_stores,
            }
        raw_preferences = decision.get("preferences")
        if isinstance(raw_preferences, dict):
            preferences.update(raw_preferences)
        if preferences:
            patch["preferences"] = preferences

        submission: JsonDict = {}
        config_assignment = str(decision.get("config_assignment") or "").strip()
        if config_assignment:
            submission["pending_config_assignment"] = config_assignment
        if submission:
            patch["submission"] = submission
        return patch

    def _record_conversation_turn(
        self,
        session_id: str,
        user_text: str,
        response: AgentResponse,
        sender_id: str | None = None,
    ) -> None:
        session = self.state_store.get_session(session_id)
        history = list(session.get("conversation_history") or [])
        turn = {
            "ts": int(time.time()),
            "sender_id": sender_id or "",
            "user": _shorten(user_text, 500),
            "assistant": _shorten(response.text, 800),
            "intent": str(response.data.get("intent") or ""),
        }
        if hasattr(self.state_store, "append_conversation_turn"):
            self.state_store.append_conversation_turn(session_id, turn, keep_recent=20)
            self.state_store.update_session(session_id, {"sender_id": sender_id})
            return
        history.append(turn)
        self.state_store.update_session(session_id, {"conversation_history": history[-20:], "sender_id": sender_id})

    def _start_trace(self, session_id: str, text: str, *, sender_id: str | None = None) -> str:
        trace_id = uuid.uuid4().hex
        self._active_traces[trace_id] = {
            "trace_id": trace_id,
            "session_id": session_id,
            "sender_id": sender_id or "",
            "user_message": text,
            "events": [],
            "started_at": int(time.time()),
        }
        return trace_id

    def _trace_event(self, session_id: str, event_type: str, payload: JsonDict) -> None:
        trace = self._current_trace(session_id)
        if not trace:
            return
        trace.setdefault("events", []).append(
            {
                "ts": int(time.time()),
                "type": event_type,
                "payload": _redact_for_trace(payload),
            }
        )

    def _finish_trace(self, session_id: str, trace_id: str, response: AgentResponse) -> None:
        trace = self._active_traces.pop(trace_id, None)
        if not trace:
            return
        if response.data.get("intent") in {"clear_session_state", "clear_all_state"}:
            return
        trace["finished_at"] = int(time.time())
        trace["final_response"] = _redact_for_trace({"text": response.text, "data": response.data})
        if hasattr(self.state_store, "append_trace_event"):
            self.state_store.append_trace_event(session_id, _redact_for_trace(trace))

    def _current_trace(self, session_id: str) -> JsonDict | None:
        for trace in reversed(list(self._active_traces.values())):
            if trace.get("session_id") == session_id:
                return trace
        return None

    def _stage_config_patch(
        self,
        session_id: str,
        patch: JsonDict,
        sender_id: str | None = None,
    ) -> AgentResponse:
        session = self.state_store.get_session(session_id)
        pending = dict(session.get("pending_config_patch") or {})
        pending.update(patch)
        self.state_store.update_session(
            session_id,
            {
                "pending_config_patch": pending,
                "sender_id": sender_id,
            },
        )
        return AgentResponse(
            format_patch_summary(pending) + "\n发送“确认保存配置”写入文件，或发送“取消保存配置”。",
            {"intent": "stage_config_update", "patch": pending},
        )

    def _make_oppo_agent(self) -> Any:
        if self.oppo_agent_factory:
            return self.oppo_agent_factory()
        if not self.oppo_config_path:
            raise OppoError("未配置 OPPO 配置文件路径")
        return OppoSubmissionAgent(OppoSubmissionConfig.from_file(self.oppo_config_path))

    def _make_market_searcher(self) -> Any:
        if self.market_searcher_factory:
            return self.market_searcher_factory()
        return AppMarketSearcher(market_data_config=self._market_data_config())

    def _build_tool_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            "help",
            "查看 AutoReview 飞书 agent 支持的能力和常用指令。",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda call, context: AgentResponse(HELP_TEXT, {"intent": "help"}),
        )
        registry.register(
            "session_status",
            "查看当前会话记录、最近图片/OCR、竞品搜索、打包查询、整改待办等状态。",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self._tool_session_status,
        )
        registry.register(
            "oppo_status",
            "查询 OPPO 当前审核状态；可选 version_code。",
            {
                "type": "object",
                "properties": {"version_code": {"type": "string"}},
                "additionalProperties": True,
            },
            self._tool_oppo_status,
        )
        registry.register(
            "oppo_app_list",
            "查询 OPPO 平台当前开发者账号下已创建应用；基于本地 packlist/config 包名逐个确认。",
            {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
                "additionalProperties": True,
            },
            self._tool_oppo_app_list,
        )
        registry.register(
            "submission_check",
            "执行提交前检查，检查配置、缺文件和最近驳回重提风险。",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self._tool_submission_check,
        )
        registry.register(
            "oppo_submit",
            "确认后执行 OPPO 自动提审；会上传材料、提交新版本并等待 OPPO 提交任务结果。",
            {
                "type": "object",
                "properties": {
                    "confirm": {"type": "boolean"},
                    "wait_task": {"type": "boolean"},
                    "wait_review": {"type": "boolean"},
                    "force": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
            self._tool_oppo_submit,
        )
        registry.register(
            "view_submission_config",
            "查看当前 OPPO 提交配置的非密钥字段摘要。",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self._tool_view_submission_config,
        )
        registry.register(
            "stage_config_update",
            "暂存提交配置修改，不直接写入文件；支持 config_assignment 或 json_patch。",
            {
                "type": "object",
                "properties": {
                    "config_assignment": {"type": "string"},
                    "json_patch": {"type": "object"},
                },
                "additionalProperties": True,
            },
            self._tool_stage_config_update,
        )
        registry.register(
            "confirm_config_update",
            "确认并写入当前会话暂存的配置修改。",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self._tool_confirm_config_update,
        )
        registry.register(
            "cancel_config_update",
            "取消当前会话暂存的配置修改。",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self._tool_cancel_config_update,
        )
        registry.register(
            "stage_file_move",
            "暂存本地文件复制/移动计划；不会立即执行，必须再确认。",
            {
                "type": "object",
                "properties": {
                    "source_path": {"type": "string", "description": "源文件路径；可空，空时使用最近上传文件或最近打包 APK。"},
                    "target_dir": {"type": "string", "description": "目标目录；例如 D:\\AutoReview\\release。"},
                    "target_path": {"type": "string", "description": "目标文件完整路径；优先于 target_dir。"},
                    "operation": {"type": "string", "description": "copy 或 move；不确定时用 copy。"},
                    "overwrite": {"type": "boolean", "description": "是否覆盖已存在目标文件。"},
                },
                "additionalProperties": True,
            },
            self._tool_stage_file_move,
        )
        registry.register(
            "confirm_file_move",
            "确认并执行当前会话暂存的文件复制/移动计划。",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self._tool_confirm_file_move,
        )
        registry.register(
            "cancel_file_move",
            "取消当前会话暂存的文件复制/移动计划。",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self._tool_cancel_file_move,
        )
        registry.register(
            "bind_material",
            "把最近一次飞书上传的文件或图片绑定为 APK、图标、截图、版权证明或 ICP 证明。",
            {
                "type": "object",
                "properties": {"material_label": {"type": "string"}},
                "additionalProperties": True,
            },
            self._tool_bind_material,
        )
        registry.register(
            "index_materials",
            "扫描本地上架资源目录，按应用名或包名匹配材料，并暂存 submission 配置 patch；不会直接写入配置文件。",
            {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string"},
                    "pkg_name": {"type": "string"},
                    "materials_root": {"type": "string"},
                },
                "additionalProperties": True,
            },
            self._tool_index_materials,
        )
        registry.register(
            "analyze_rejection",
            "分析用户提供的应用商店审核驳回原因文本。",
            {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "additionalProperties": True,
            },
            self._tool_analyze_rejection,
        )
        registry.register(
            "analyze_last_image",
            "用当前会话最近一次图片 OCR 文本分析审核驳回原因。",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self._tool_analyze_last_image,
        )
        registry.register(
            "remediation_checklist",
            "基于最近一次驳回分析生成整改待办清单。",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self._tool_remediation_checklist,
        )
        registry.register(
            "package_lookup",
            "按中文应用名查询 packlist 里的包名、渠道、版本号和版本名。",
            {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string"},
                    "query": {"type": "string"},
                    "offset": {"type": "integer"},
                    "page_size": {"type": "integer"},
                    "last_page": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
            self._tool_package_lookup,
        )
        registry.register(
            "package_apk",
            "打包单个 APK。支持 app_name、pkg_name、channels、dry_run。",
            {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string"},
                    "pkg_name": {"type": "string"},
                    "channels": {"type": "array", "items": {"type": "string"}},
                    "dry_run": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
            self._tool_package_apk,
        )
        registry.register(
            "batch_package",
            "按配置文件批量打包 APK，或按指定应用名/渠道批量打包。支持 dry_run。",
            {
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean"},
                    "app_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要打包的应用名列表，如 ['三年级英语上册', '三年级英语下册']。提供时按应用名打包，不提供时按配置文件批量打包。",
                    },
                    "channels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要打包的渠道列表，如 ['1038', '1039'] 或 ['xm1038', 'xm1039']。提供时按渠道逐次打包。",
                    },
                },
                "additionalProperties": True,
            },
            self._tool_batch_package,
        )
        registry.register(
            "market_search",
            "搜索应用商店竞品或指定 APP 的公开指标。支持精确匹配、排除词和指定商店。",
            {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string", "description": "要搜索的应用名称或关键词（必需）"},
                    "exact_match": {"type": "boolean", "description": "是否精确匹配应用名"},
                    "exclude_terms": {"type": "array", "items": {"type": "string"}, "description": "要排除的关键词"},
                    "include_terms": {"type": "array", "items": {"type": "string"}, "description": "必须包含的关键词"},
                    "target_stores": {"type": "array", "items": {"type": "string"}, "description": "目标应用商店列表"},
                },
                "required": ["app_name"],  # 明确标记 app_name 为必需参数
                "additionalProperties": True,
            },
            self._tool_market_search,
        )
        registry.register(
            "market_download_snapshot",
            "搜索竞品并把当前月份的公开下载/评分指标写入会话状态。",
            {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string", "description": "要记录下载数据的应用名称或关键词（必需）"},
                    "exact_match": {"type": "boolean", "description": "是否精确匹配应用名"},
                    "exclude_terms": {"type": "array", "items": {"type": "string"}, "description": "要排除的关键词"},
                    "include_terms": {"type": "array", "items": {"type": "string"}, "description": "必须包含的关键词"},
                    "target_stores": {"type": "array", "items": {"type": "string"}, "description": "目标应用商店列表"},
                },
                "required": ["app_name"],  # 明确标记 app_name 为必需参数
                "additionalProperties": True,
            },
            self._tool_market_download_snapshot,
        )
        registry.register(
            "file_search",
            "在 AutoReview 相关配置和项目文件中全文搜索关键词或旧路径，用于排查配置残留。",
            {
                "type": "object",
                "properties": {
                    "patterns": {"type": "array", "items": {"type": "string"}},
                    "pattern": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "additionalProperties": True,
            },
            self._tool_file_search,
        )
        return registry

    def _response_from_llm_tool_call(
        self,
        session_id: str,
        text: str,
        sender_id: str | None = None,
    ) -> AgentResponse | None:
        if not self.llm_client or not hasattr(self.llm_client, "choose_tool"):
            return None
        context = self._llm_context(session_id)
        schemas = self.tool_registry.schemas()
        self._trace_event(session_id, "llm_tool_choice_request", {"message": text, "context": context, "tools": schemas})
        try:
            raw_call = self.llm_client.choose_tool(text, context, schemas)
        except Exception as exc:
            self._trace_event(session_id, "llm_tool_choice_error", {"error": str(exc)})
            return None
        if not isinstance(raw_call, dict):
            self._trace_event(session_id, "llm_tool_choice_invalid", {"result": str(raw_call)})
            return None
        self._trace_event(session_id, "llm_tool_choice_response", {"tool_call": raw_call})
        self._store_llm_memories(session_id, raw_call, sender_id=sender_id)
        try:
            tool_call = ToolCall.from_mapping(raw_call)
        except ValueError:
            return None
        tool_call = self._normalize_market_tool_call(tool_call, text)
        tool_call = self._normalize_oppo_tool_call(tool_call, text)
        tool_call = self._normalize_file_move_tool_call(tool_call, text)
        if tool_call.is_noop:
            return None
        if tool_call.confidence is not None and tool_call.confidence < 0.45:
            return None
        if not self.tool_registry.has(tool_call.name):
            return None

        context = {"session_id": session_id, "text": text, "sender_id": sender_id}
        self._trace_event(session_id, "tool_execute_request", {"tool_call": tool_call.to_dict(), "context": context})
        try:
            response = self.tool_registry.execute(tool_call, context)
        except Exception as exc:
            response = AgentResponse(
                _format_error_message("工具执行失败", str(exc), ["查看 trace 日志确认工具输入和本地异常。"]),
                {"intent": tool_call.name, "error": str(exc)},
            )
        if not isinstance(response, AgentResponse):
            response = AgentResponse(str(response or ""), {"intent": tool_call.name})

        tool_result = {
            "ok": "error" not in response.data,
            "text": response.text,
            "data": _json_safe(response.data),
        }
        self._trace_event(session_id, "tool_execute_response", {"tool_call": tool_call.to_dict(), "tool_result": tool_result})
        summary = self._summarize_tool_response(session_id, text, tool_call, tool_result)
        data = dict(response.data)
        data["tool_call"] = tool_call.to_dict()
        data["tool_result"] = tool_result
        return AgentResponse(summary or response.text, data)

    @staticmethod
    def _normalize_market_tool_call(tool_call: ToolCall, text: str) -> ToolCall:
        if tool_call.name != "market_download_snapshot":
            return tool_call
        if _looks_like_market_download_request(text):
            return tool_call
        return ToolCall(
            name="market_search",
            arguments=dict(tool_call.arguments),
            confidence=tool_call.confidence,
            reason=tool_call.reason or "用户是在查应用商店公开指标，不是要求记录月度下载快照。",
        )

    def _normalize_oppo_tool_call(self, tool_call: ToolCall, text: str) -> ToolCall:
        if tool_call.name == "oppo_status" and self._looks_like_oppo_app_list_request(text.lower()):
            return ToolCall(
                name="oppo_app_list",
                arguments={},
                confidence=tool_call.confidence,
                reason=tool_call.reason or "用户要查询 OPPO 平台已创建应用列表，不是单个应用审核状态。",
            )
        return tool_call

    def _normalize_file_move_tool_call(self, tool_call: ToolCall, text: str) -> ToolCall:
        if tool_call.name == "stage_config_update" and self._looks_like_file_move_request(text):
            return ToolCall(
                name="stage_file_move",
                arguments={},
                confidence=tool_call.confidence,
                reason=tool_call.reason or "用户要移动/复制本地文件，应先暂存文件操作并等待确认。",
            )
        return tool_call

    def _summarize_tool_response(
        self,
        session_id: str,
        text: str,
        tool_call: ToolCall,
        tool_result: JsonDict,
    ) -> str:
        if not self.llm_client or not hasattr(self.llm_client, "summarize_tool_result"):
            return ""
        context = self._llm_context(session_id)
        self._trace_event(
            session_id,
            "llm_tool_summary_request",
            {
                "message": text,
                "context": context,
                "tool_call": tool_call.to_dict(),
                "tool_result": tool_result,
            },
        )
        try:
            summary = str(
                self.llm_client.summarize_tool_result(
                    text,
                    context,
                    tool_call.to_dict(),
                    tool_result,
                )
                or ""
            ).strip()
            self._trace_event(session_id, "llm_tool_summary_response", {"reply": summary})
            if _looks_like_mojibake(summary):
                self._trace_event(session_id, "llm_tool_summary_mojibake", {"reply": summary})
                return ""
            return _format_tool_summary_reply(summary)
        except Exception as exc:
            self._trace_event(session_id, "llm_tool_summary_error", {"error": str(exc)})
            return ""

    def _tool_session_status(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        session_id = str(context.get("session_id") or "")
        session = self.state_store.get_session(session_id)
        return AgentResponse(self._format_session(session), {"intent": "status", "session": session})

    def _tool_oppo_status(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        version_code = str(call.arguments.get("version_code") or "").strip()
        text = f"查询审核状态：{version_code}" if version_code else str(context.get("text") or "查询审核状态")
        return self.query_oppo_status(
            text,
            session_id=str(context.get("session_id") or ""),
            sender_id=context.get("sender_id"),
        )

    def _tool_oppo_app_list(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        limit = _optional_int(call.arguments.get("limit")) or 100
        return self.query_oppo_app_list(
            str(context.get("session_id") or ""),
            sender_id=context.get("sender_id"),
            limit=limit,
        )

    def _tool_submission_check(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        return self.check_submission(str(context.get("session_id") or ""))

    def _tool_oppo_submit(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        text = str(context.get("text") or "")
        confirm = _optional_bool(call.arguments.get("confirm")) or self._looks_like_oppo_submit_confirmation(text.lower())
        wait_task = _optional_bool(call.arguments.get("wait_task"))
        return self.submit_oppo(
            str(context.get("session_id") or ""),
            sender_id=context.get("sender_id"),
            confirm=confirm,
            wait_task=True if wait_task is None else bool(wait_task),
            wait_review=bool(_optional_bool(call.arguments.get("wait_review"))),
            force=bool(_optional_bool(call.arguments.get("force"))),
        )

    def _tool_view_submission_config(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        return self.view_submission_config()

    def _tool_stage_config_update(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        session_id = str(context.get("session_id") or "")
        sender_id = context.get("sender_id")
        assignment = str(call.arguments.get("config_assignment") or "").strip()
        json_patch = call.arguments.get("json_patch")
        if isinstance(json_patch, dict) and json_patch:
            return self.stage_config_json(
                session_id,
                json.dumps(json_patch, ensure_ascii=False),
                sender_id=sender_id,
            )
        if not assignment:
            assignment = self._extract_assignment_payload(str(context.get("text") or "")).strip()
        packaging_script_update = self._extract_packaging_script_update(str(context.get("text") or ""))
        if packaging_script_update and (
            not assignment or "package_script_path" in assignment or "packaging.script" not in assignment
        ):
            assignment = f"packaging.script={packaging_script_update}"
        if not assignment:
            return AgentResponse(
                "要改哪个配置？请给我类似 submission.version_code=10002 的字段和值。",
                {"intent": "stage_config_update", "missing": "assignment"},
            )
        if assignment.lstrip().startswith("{"):
            return self.stage_config_json(session_id, assignment, sender_id=sender_id)
        return self.stage_config_assignment(session_id, assignment, sender_id=sender_id)

    def _tool_confirm_config_update(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        return self.confirm_config_update(
            str(context.get("session_id") or ""),
            sender_id=context.get("sender_id"),
        )

    def _tool_cancel_config_update(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        return self.cancel_config_update(
            str(context.get("session_id") or ""),
            sender_id=context.get("sender_id"),
        )

    def _tool_stage_file_move(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        return self.stage_file_move(
            str(context.get("session_id") or ""),
            source_path=str(call.arguments.get("source_path") or ""),
            target_dir=str(call.arguments.get("target_dir") or ""),
            target_path=str(call.arguments.get("target_path") or ""),
            operation=str(call.arguments.get("operation") or ""),
            overwrite=bool(_optional_bool(call.arguments.get("overwrite"))),
            text=str(context.get("text") or ""),
            sender_id=context.get("sender_id"),
        )

    def _tool_confirm_file_move(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        return self.confirm_file_move(
            str(context.get("session_id") or ""),
            sender_id=context.get("sender_id"),
        )

    def _tool_cancel_file_move(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        return self.cancel_file_move(
            str(context.get("session_id") or ""),
            sender_id=context.get("sender_id"),
        )

    def _tool_bind_material(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        label = str(call.arguments.get("material_label") or "").strip()
        if not label:
            label = self._extract_material_label(str(context.get("text") or "")).strip()
        return self.bind_last_upload_as_material(
            str(context.get("session_id") or ""),
            label,
            sender_id=context.get("sender_id"),
        )

    def _tool_index_materials(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        return self.index_submission_materials(
            str(context.get("session_id") or ""),
            app_name=str(call.arguments.get("app_name") or ""),
            pkg_name=str(call.arguments.get("pkg_name") or ""),
            materials_root=str(call.arguments.get("materials_root") or ""),
            sender_id=context.get("sender_id"),
            source_text=str(context.get("text") or ""),
        )

    def _tool_analyze_rejection(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        reason = str(call.arguments.get("reason") or "").strip()
        if not reason:
            reason = self._extract_payload(str(context.get("text") or "")).strip()
        if not reason:
            return AgentResponse("请把审核驳回原因发给我，我再分析。", {"intent": "analyze_rejection", "missing": "reason"})
        return self.analyze_rejection_text(
            str(context.get("session_id") or ""),
            reason,
            sender_id=context.get("sender_id"),
        )

    def _tool_analyze_last_image(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        session_id = str(context.get("session_id") or "")
        session = self.state_store.get_session(session_id)
        image_text = self._get_last_image_text(session)
        if not image_text:
            return AgentResponse(
                _format_error_message("图片分析缺少 OCR 文本", "最近图片还没有可用于分析的 OCR 文本。", ["请先发送一张包含驳回原因的截图。"]),
                {"intent": "analyze_last_image", "missing": "ocr_text"},
            )
        return self.analyze_rejection_text(
            session_id,
            image_text,
            sender_id=context.get("sender_id"),
            source="image",
        )

    def _tool_remediation_checklist(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        return self.build_remediation_checklist(
            str(context.get("session_id") or ""),
            sender_id=context.get("sender_id"),
        )

    def _tool_package_lookup(self, call: ToolCall, context: JsonDict) -> AgentResponse | None:
        decision = {
            "intent": "package_lookup",
            "app_name": str(call.arguments.get("app_name") or call.arguments.get("query") or ""),
            "offset": call.arguments.get("offset"),
            "page_size": call.arguments.get("page_size"),
            "last_page": bool(call.arguments.get("last_page")),
        }
        return self._run_packaging_lookup(
            str(context.get("session_id") or ""),
            str(context.get("text") or ""),
            decision,
        )

    def _tool_package_apk(self, call: ToolCall, context: JsonDict) -> AgentResponse | None:
        decision = {
            "intent": "package_apk",
            "app_name": str(call.arguments.get("app_name") or ""),
            "pkg_name": str(call.arguments.get("pkg_name") or ""),
            "channels": _normalize_string_list(call.arguments.get("channels") or []),
            "dry_run": bool(call.arguments.get("dry_run")),
        }
        return self._run_packaging_intent(
            str(context.get("session_id") or ""),
            str(context.get("text") or ""),
            decision,
        )

    def _tool_batch_package(self, call: ToolCall, context: JsonDict) -> AgentResponse | None:
        app_names = _normalize_string_list(call.arguments.get("app_names") or [])
        channels = _normalize_string_list(call.arguments.get("channels") or [])
        return self._run_packaging_intent(
            str(context.get("session_id") or ""),
            str(context.get("text") or ""),
            {
                "intent": "batch_package",
                "dry_run": bool(call.arguments.get("dry_run")),
                "app_names": app_names,
                "channels": channels,
            },
        )

    def _tool_file_search(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        raw_patterns = call.arguments.get("patterns")
        patterns = _normalize_string_list(raw_patterns if raw_patterns else call.arguments.get("pattern"))
        if not patterns:
            patterns = self._extract_file_search_patterns(str(context.get("text") or ""))
        return self._search_local_files(patterns, max_results=int(call.arguments.get("max_results") or 20))

    def _tool_market_search(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        # 兼容旧的 query 参数名，优先使用 app_name
        query = str(call.arguments.get("app_name") or call.arguments.get("query") or "").strip()
        return self.search_competitors(
            str(context.get("session_id") or ""),
            query,
            sender_id=context.get("sender_id"),
            source_text=str(context.get("text") or ""),
            exact_match=bool(call.arguments.get("exact_match")),
            exclude_terms=_normalize_string_list(call.arguments.get("exclude_terms") or []),
            include_terms=_normalize_string_list(call.arguments.get("include_terms") or []),
            target_stores=_normalize_store_list(call.arguments.get("target_stores") or []),
        )

    def _tool_market_download_snapshot(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        # 兼容旧的 query 参数名，优先使用 app_name
        query = str(call.arguments.get("app_name") or call.arguments.get("query") or "").strip()
        return self.record_competitor_downloads(
            str(context.get("session_id") or ""),
            query,
            sender_id=context.get("sender_id"),
            source_text=str(context.get("text") or ""),
            exact_match=bool(call.arguments.get("exact_match")),
            exclude_terms=_normalize_string_list(call.arguments.get("exclude_terms") or []),
            include_terms=_normalize_string_list(call.arguments.get("include_terms") or []),
            target_stores=_normalize_store_list(call.arguments.get("target_stores") or []),
        )

    def _search_markets(
        self,
        session_id: str,
        query: str,
        *,
        limit: int,
        target_stores: set[str] | None = None,
    ) -> Any:
        stores = self._allowed_market_stores(session_id, target_stores=target_stores)
        searcher = self._make_market_searcher()
        if not stores:
            return searcher.search_competitors(query, limit=limit)
        return searcher.search_competitors(query, limit=limit, stores=stores)

    def _allowed_market_stores(self, session_id: str, *, target_stores: set[str] | None = None) -> set[str] | None:
        preferences = self._market_store_preferences(session_id)
        disabled = preferences.get("disabled_stores") or []
        normalized_targets = {
            _normalize_store_name(store)
            for store in (target_stores or set())
            if _normalize_store_name(store)
        }
        if normalized_targets:
            allowed = normalized_targets - set(disabled)
            return allowed
        if not disabled:
            return None
        allowed = {store for store, _ in SUPPORTED_MARKET_STORES if store not in set(disabled)}
        return allowed or set()

    def _market_store_preferences(self, session_id: str) -> JsonDict:
        session = self.state_store.get_session(session_id)
        raw = session.get("market_store_preferences") or {}
        disabled = []
        for store in raw.get("disabled_stores") or []:
            normalized = _normalize_store_name(store)
            if normalized and normalized not in disabled:
                disabled.append(normalized)
        return {"disabled_stores": disabled}

    def _handle_market_store_preference(
        self,
        session_id: str,
        text: str,
        sender_id: str | None = None,
    ) -> AgentResponse | None:
        parsed = self._parse_market_store_preference(text)
        if not parsed:
            return None
        disable, enable = parsed
        return self._apply_market_store_preference(
            session_id,
            disable_stores=disable,
            enable_stores=enable,
            sender_id=sender_id,
        )

    def _parse_market_store_preference(self, text: str) -> tuple[list[str], list[str]] | None:
        store = _extract_market_store_name(text)
        if not store:
            return None
        lowered = text.lower()
        if not self._contains_any(lowered, ("默认", "以后", "后续", "每次", "查询", "搜索", "查")):
            return None
        disable = self._contains_any(lowered, ("不查询", "不要查", "别查", "不查", "排除", "跳过", "禁用", "去掉"))
        enable = self._contains_any(lowered, ("恢复查询", "恢复查", "重新查询", "继续查询", "启用", "取消排除", "取消禁用"))
        if not disable and not enable:
            return None
        return ([store] if disable else [], [store] if enable else [])

    def _apply_market_store_preference(
        self,
        session_id: str,
        *,
        disable_stores: list[str],
        enable_stores: list[str],
        sender_id: str | None = None,
    ) -> AgentResponse:
        preferences = self._market_store_preferences(session_id)
        disabled = set(preferences.get("disabled_stores") or [])
        for store in disable_stores:
            disabled.add(store)
        for store in enable_stores:
            disabled.discard(store)
        updated = {"disabled_stores": sorted(disabled)}
        memory = self._structured_long_term_memory(session_id)
        memory_preferences = dict(memory.get("preferences") or {})
        memory_preferences["market_stores"] = updated
        memory["preferences"] = memory_preferences
        self.state_store.update_session(
            session_id,
            {
                "market_store_preferences": updated,
                "long_term_memory": memory,
                "sender_id": sender_id,
            },
        )
        changed = disable_stores or enable_stores
        action = "不查询" if disable_stores else "恢复查询"
        labels = "、".join(_store_label(store) for store in changed)
        return AgentResponse(
            f"已记录当前会话偏好：默认{action}{labels}。这只影响当前飞书会话，不会修改默认配置文件。",
            {"intent": "market_store_preference", "preferences": updated},
        )

    def _remember_status_app_info(
        self,
        session_id: str,
        status: JsonDict,
        *,
        sender_id: str | None = None,
    ) -> None:
        app_info = status.get("app_info") or {}
        remembered = {
            "app_name": str(app_info.get("app_name") or app_info.get("name") or ""),
            "pkg_name": str(status.get("pkg_name") or app_info.get("pkg_name") or app_info.get("package_name") or ""),
            "version_code": str(status.get("version_code") or app_info.get("version_code") or ""),
        }
        if not remembered["app_name"]:
            config_app = self._config_app_info()
            if remembered["pkg_name"] and remembered["pkg_name"] == config_app.get("pkg_name"):
                remembered["app_name"] = str(config_app.get("app_name") or "")
        self.state_store.update_session(
            session_id,
            {
                "app_info": remembered,
                "last_oppo_status": status,
                "sender_id": sender_id,
            },
        )

    def _default_app_info(self, session_id: str) -> JsonDict:
        session = self.state_store.get_session(session_id)
        app_info = session.get("app_info") or {}
        if any(app_info.get(key) for key in ("app_name", "pkg_name", "version_code")):
            return dict(app_info)
        return self._config_app_info()

    def _config_app_info(self) -> JsonDict:
        if not self.oppo_config_path:
            return {}
        try:
            config = OppoSubmissionConfig.from_file(self.oppo_config_path)
        except Exception:
            return {}
        submission = config.submission or {}
        return {
            "app_name": str(submission.get("app_name") or ""),
            "pkg_name": str(submission.get("pkg_name") or "").strip(),
            "version_code": str(submission.get("version_code") or ""),
        }

    def _market_data_config(self) -> JsonDict:
        if not self.market_data_config_path or not self.market_data_config_path.exists():
            return {}
        try:
            raw = json.loads(self.market_data_config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _format_packaging_config_summary(self) -> str:
        config_path = self.packaging_config_path
        if not config_path or not config_path.exists():
            return ""
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return ""
        packaging = raw.get("packaging") or {}
        if not isinstance(packaging, dict):
            return ""
        lines = [
            f"当前打包配置（{config_path.name}）：",
            f"- 项目目录：{packaging.get('project_dir') or '未配置'}",
            f"- 打包脚本：{packaging.get('script') or packaging.get('script_path') or '未配置'}",
            f"- 批量清单：{packaging.get('batch_file') or '未配置'}",
            f"- packlist 快照：{packaging.get('packlist_scan_file') or packaging.get('packlist_snapshot') or '未配置'}",
            f"- 上架资源：{packaging.get('materials_root') or '未配置'}",
            f"- Node：{packaging.get('node_command') or 'node'}",
        ]
        return "\n".join(lines)

    def _format_material_index_suggestion(self, suggestion: JsonDict, patch: JsonDict) -> str:
        app = suggestion.get("app") or {}
        candidates = suggestion.get("candidates") or {}
        lines = ["上架资源索引", ""]
        if app:
            lines.append("应用信息：")
            lines.append(f"- 应用：{app.get('app_name') or suggestion.get('query') or '未识别'}")
            lines.append(f"- 包名：{app.get('pkg_name') or '未识别'}")
            lines.append(f"- 版本：{app.get('version_code') or ''} / {app.get('version_name') or ''}")
            lines.append("")
        lines.append("已暂存配置：")
        for key in patch:
            lines.append(f"- {key} = {_display_patch_value(patch[key])}")
        lines.append("")
        lines.append("首选材料：")
        for key, label in (
            ("icon", "图标"),
            ("screenshots", "截图"),
            ("copyright", "版权/软著"),
            ("icp", "ICP备案"),
            ("special", "补充材料"),
        ):
            items = candidates.get(key) or []
            if not items:
                continue
            first = items[0]
            lines.append(f"- {label}：{Path(str(first.get('path') or '')).name}")
        warnings = suggestion.get("warnings") or []
        if warnings:
            lines.append("")
            lines.append("提醒：")
            lines.extend(f"- {item}" for item in warnings)
        lines.append("")
        lines.append("下一步：")
        lines.append("- 发送“确认保存配置”写入文件。")
        lines.append("- 发送“取消保存配置”放弃修改。")
        return "\n".join(lines)

    def _materials_root_path(self) -> Path | None:
        configured = self._packaging_setting("materials_root")
        if configured:
            path = Path(str(configured))
            base_dir = self.packaging_config_path.parent if self.packaging_config_path else Path.cwd()
            return path if path.is_absolute() else base_dir / path
        default = Path(r"D:\Workship\Pelbs\AppMaket\上架资源")
        return default if default.exists() else None

    def _packaging_setting(self, key: str) -> Any:
        config_path = self.packaging_config_path
        if not config_path or not config_path.exists():
            return None
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        packaging = raw.get("packaging") or {}
        return packaging.get(key) if isinstance(packaging, dict) else None

    def analyze_rejection_text(
        self,
        session_id: str,
        reason: str,
        sender_id: str | None = None,
        *,
        source: str = "text",
    ) -> AgentResponse:
        analysis = analyze_rejection_reason(reason)
        self.state_store.update_session(
            session_id,
            {
                "last_rejection_reason": reason,
                "last_rejection_analysis": analysis,
                "last_rejection_source": source,
                "sender_id": sender_id,
            },
        )
        return AgentResponse(self._format_rejection_analysis(analysis), analysis)

    @staticmethod
    def _normalize_incoming_text(text: str) -> str:
        clean = str(text or "").strip()
        clean = re.sub(r"<at\b[^>]*>.*?</at>", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"@\s*(提交助手|AutoReview|autoreview)\s*", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"(?<!\S)@[A-Za-z0-9_\-\u4e00-\u9fff]{1,32}(?!\S)", "", clean)
        clean = re.sub(r"@[A-Za-z0-9_\-\u4e00-\u9fff]{1,32}\s*$", "", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean

    def _answer_capability_question(self, session_id: str, text: str) -> AgentResponse | None:
        lowered = text.lower()
        if not self._looks_like_capability_question(lowered):
            return None

        if "image2" in lowered:
            image2_url = self._configured_image_analysis_url("image2_url")
            if image2_url:
                text = "image2 已配置，会作为图片分析的辅助能力接入。发送图片后我会记录识别结果；如果同时配置了 OCR，默认优先展示 OCR。"
            else:
                text = "image2 目前未配置。当前图片主流程优先使用 OCR；需要启用 image2 时，在 feishu.image_analysis.image2_url 里填接口地址。"
            return AgentResponse(text, {"intent": "capability_question", "capability": "image2", "configured": bool(image2_url)})

        if "ocr" in lowered or "文字识别" in text or "识别图片" in text:
            ocr_url = self._configured_image_analysis_url("ocr_url")
            if ocr_url:
                text = "OCR 已接入。你可以直接发送审核截图，我会识别文字；如果像 OPPO 驳回截图，会继续做驳回分析。"
            else:
                text = "OCR 能力代码已接入，但当前配置里还没有 OCR 接口地址。请在 feishu.image_analysis.ocr_url 配好后重启飞书机器人。"
            return AgentResponse(text, {"intent": "capability_question", "capability": "ocr", "configured": bool(ocr_url)})

        if self._looks_like_market_store_scope_question(lowered):
            preferences = self._market_store_preferences(session_id)
            return AgentResponse(
                _format_supported_market_stores(set(preferences.get("disabled_stores") or [])),
                {
                    "intent": "capability_question",
                    "capability": "market_store_scope",
                    "configured": True,
                    "preferences": preferences,
                },
            )

        if self._looks_like_packaging_scope_question(lowered):
            project_dir = self.packaging_agent.settings.project_dir
            snapshot = self._packaging_packlist_snapshot()
            lines = ["支持打包和查包。"]
            if project_dir:
                lines.append(f"- 当前打包项目目录：{project_dir}")
            else:
                lines.append("- 当前还没有配置打包项目目录。")
            if snapshot:
                lines.append(f"- 当前可用的 packlist 快照：{snapshot}")
            lines.append("你可以这样用：")
            lines.append("- 查包：八年级语文下册对应什么包")
            lines.append("- 打包预演：打包 八年级语文下册 dry-run")
            lines.append("- 正式打包：打包 八年级语文下册")
            lines.append("- 按包名打包：打包 com.pelbs.book1067")
            lines.append("- 批量打包：批量打包 dry-run")
            return AgentResponse(
                "\n".join(lines),
                {"intent": "capability_question", "capability": "packaging", "configured": bool(project_dir or snapshot)},
            )

        if self._contains_any(lowered, ("竞品", "应用商店", "应用市场", "下载量")):
            return AgentResponse(
                "有应用商店查询能力，也支持竞品分析。可以发送“搜索应用商店：关键词”查询指定 APP 公开指标；发送“搜索竞品：关键词”查询同类 APP；只有发送“记录竞品下载：关键词”才会写入月度记录。"
                "\n注意：不同商店公开数据不一样，下载量和评分可能会有缺失或查询失败。",
                {"intent": "capability_question", "capability": "market_search", "configured": True},
            )

        if self._contains_any(lowered, ("提交", "审核", "oppo", "配置", "材料", "打包")):
            return AgentResponse(
                "可以。我主要能做 OPPO 审核协作：分析驳回、生成整改清单、查审核状态、提交检查、查看/暂存配置、绑定上传材料，也能辅助做竞品搜索。",
                {"intent": "capability_question", "capability": "review_workflow", "configured": True},
            )

        return None

    @staticmethod
    def _looks_like_capability_question(text: str) -> bool:
        if any(term in text for term in ("能力", "支持", "接入", "拥有", "有没有", "会不会", "有吗", "了吗")):
            return True
        if any(term in text for term in ("哪些", "那些", "哪几", "多少")) and any(
            term in text for term in ("应用商店", "应用市场", "商店", "市场", "厂家", "厂商", "渠道")
        ):
            return True
        if ReviewAgent._looks_like_packaging_scope_question(text):
            return True
        return bool(re.search(r"能不能.*(ocr|image2|识别|搜索|查询|竞品|下载量)", text, flags=re.IGNORECASE))

    @staticmethod
    def _looks_like_market_store_scope_question(text: str) -> bool:
        return any(term in text for term in ("哪些", "那些", "哪几", "多少", "列表", "厂家", "厂商", "渠道")) and any(
            term in text for term in ("应用商店", "应用市场", "商店", "市场", "厂家", "厂商", "渠道")
        )

    @staticmethod
    def _looks_like_packaging_scope_question(text: str) -> bool:
        return any(
            term in text
            for term in (
                "能打包哪些",
                "能打包那些",
                "可以打包哪些",
                "可以打包那些",
                "能查包哪些",
                "能查包那些",
                "支持打包哪些",
                "支持打包那些",
                "支持查包哪些",
                "支持查包那些",
                "打包哪些",
                "打包那些",
            )
        )

    @staticmethod
    def _looks_like_packaging_catalog_request(text: str) -> bool:
        return (
            any(term in text for term in ("打包", "查包", "包", "渠道"))
            and any(term in text for term in ("都可以", "都能", "全部", "所有", "完整", "列表"))
        ) or any(
            term in text
            for term in (
                "都可以打那些包",
                "都可以打哪些包",
                "可以打那些包",
                "可以打哪些包",
                "全部包",
                "所有包",
                "包列表",
            )
        )

    @staticmethod
    def _looks_like_packaging_pagination_request(text: str) -> bool:
        if any(term in text for term in ("打包", "测试", "配置", "package.js", "路径", "脚本", "执行")):
            return False
        return any(
            term in text
            for term in (
                "还有呢",
                "下一页",
                "继续发",
                "后面的",
                "后面还有",
                "更高年级",
                "没有回复完全",
                "没回复完全",
                "没说完",
                "接着发",
                "最后一页",
                "最后",
            )
        )

    @staticmethod
    def _extract_packaging_page_size(text: str) -> int | None:
        match = re.search(r"(?:显示|看|列出)?最后\s*(\d+)\s*个", text)
        if match:
            return max(1, int(match.group(1)))
        return None

    @staticmethod
    def _extract_file_search_patterns(text: str) -> list[str]:
        patterns: list[str] = []
        for match in re.finditer(r"([A-Za-z]:[\\/][^\s，,。；;\"'`]+)", text):
            patterns.append(match.group(1).strip())
        for token in ("development_sercer", "package.js", "AutoReview"):
            if token in text and token not in patterns:
                patterns.append(token)
        return patterns

    def _configured_image_analysis_url(self, key: str) -> str:
        if not self.oppo_config_path or not self.oppo_config_path.exists():
            return ""
        try:
            config = json.loads(self.oppo_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        feishu = config.get("feishu") if isinstance(config, dict) else {}
        if not isinstance(feishu, dict):
            return ""
        image_analysis = feishu.get("image_analysis") or {}
        if isinstance(image_analysis, dict) and image_analysis.get(key):
            return str(image_analysis.get(key) or "").strip()
        return str(feishu.get(key) or "").strip()

    @staticmethod
    def _extract_payload(text: str) -> str:
        parts = re.split(r"[:：]", text, maxsplit=1)
        if len(parts) == 2:
            return parts[1].strip()
        return ""

    def _extract_market_query(self, text: str, session_id: str) -> str:
        payload = self._extract_payload(text)
        if payload:
            return payload
        clean = re.sub(r"^(搜索竞品|竞品搜索|搜索应用商店|找竞品|记录竞品下载|记录竞品月报|月度记录竞品)", "", text).strip()
        if clean:
            return clean
        session = self.state_store.get_session(session_id)
        analysis = session.get("last_rejection_analysis") or {}
        app_info = session.get("app_info") or {}
        return str(analysis.get("similar_app") or app_info.get("app_name") or "").strip()

    def _answer_config_followup_question(self, session_id: str, text: str) -> AgentResponse | None:
        if not any(term in text for term in ("哪个文件", "那个文件", "哪里改", "哪儿改", "修改哪个文件")):
            return None
        session = self.state_store.get_session(session_id)
        pending = session.get("pending_config_patch") or {}
        if not pending:
            return None
        if any(path.startswith("packaging.") for path in pending):
            config_path = self.packaging_config_path or (self.oppo_config_path.parent / "packaging.json" if self.oppo_config_path else None)
            target_value = next((str(value) for key, value in pending.items() if key == "packaging.script"), "")
            return AgentResponse(
                "这次要改的是打包配置文件里的 `packaging.script` 字段。"
                + (f"\n- 文件：{config_path}" if config_path else "")
                + "\n- 字段：packaging.script"
                + (f"\n- 目标值：{target_value}" if target_value else "")
                + "\n如果直接在飞书里继续操作，发送“确认保存配置”就会写入。",
                {"intent": "config_followup", "scope": "packaging", "pending": pending},
            )
        return None

    def _build_file_move_plan(
        self,
        session: JsonDict,
        *,
        source_path: str = "",
        target_dir: str = "",
        target_path: str = "",
        operation: str = "",
        overwrite: bool = False,
        text: str = "",
    ) -> JsonDict:
        text = str(text or "")
        source = self._resolve_file_move_source(session, source_path=source_path, text=text)
        if not source.exists() or not source.is_file():
            raise ValueError(f"源文件不存在：{source}")
        op = self._resolve_file_move_operation(operation, text)
        target = self._resolve_file_move_target(
            source,
            target_dir=target_dir,
            target_path=target_path,
            text=text,
        )
        project_root = self._project_root()
        if not self._path_is_inside(target, project_root):
            raise ValueError(f"目标路径必须位于项目目录内：{project_root}")
        return {
            "operation": op,
            "source_path": str(source),
            "target_path": str(target),
            "project_root": str(project_root),
            "overwrite": bool(overwrite),
            "created_at": int(time.time()),
        }

    def _execute_file_move_plan(self, plan: JsonDict) -> JsonDict:
        operation = str(plan.get("operation") or "copy").strip().lower()
        if operation not in {"copy", "move"}:
            raise ValueError(f"不支持的文件操作：{operation}")
        source = Path(str(plan.get("source_path") or ""))
        target = Path(str(plan.get("target_path") or ""))
        project_root = Path(str(plan.get("project_root") or self._project_root()))
        if not source.exists() or not source.is_file():
            raise ValueError(f"源文件不存在：{source}")
        if not self._path_is_inside(target, project_root):
            raise ValueError(f"目标路径必须位于项目目录内：{project_root}")
        if target.exists() and not bool(plan.get("overwrite")):
            raise ValueError(f"目标文件已存在：{target}。如需覆盖，请重新暂存并说明“覆盖”。")
        target.parent.mkdir(parents=True, exist_ok=True)
        if operation == "move":
            shutil.move(str(source), str(target))
        else:
            shutil.copy2(source, target)
        return {
            "operation": operation,
            "source_path": str(source),
            "target_path": str(target),
            "overwrite": bool(plan.get("overwrite")),
        }

    def _resolve_file_move_source(self, session: JsonDict, *, source_path: str = "", text: str = "") -> Path:
        explicit_path = str(source_path or "").strip().strip("\"'")
        if not explicit_path:
            candidates = self._extract_file_paths_from_text(text)
            explicit_path = candidates[0] if candidates else ""
        if explicit_path:
            return Path(explicit_path)

        upload = session.get("last_upload") or {}
        upload_path = Path(str(upload.get("path") or "")) if upload.get("path") else None
        if upload_path and upload_path.suffix.lower() in {".apk", ".aab"}:
            return upload_path

        package_result = session.get("last_package_result") or {}
        latest_apks = package_result.get("latest_apks") or []
        if latest_apks:
            return Path(str(latest_apks[0]))

        if upload_path:
            return upload_path
        raise ValueError("没有找到可移动的源文件。")

    def _resolve_file_move_target(
        self,
        source: Path,
        *,
        target_dir: str = "",
        target_path: str = "",
        text: str = "",
    ) -> Path:
        clean_target_path = str(target_path or "").strip().strip("\"'")
        if clean_target_path:
            target = Path(clean_target_path)
            if (target.exists() and target.is_dir()) or not target.suffix:
                target = self._normalize_release_dir(target)
                return target / source.name
            return target

        clean_target_dir = str(target_dir or "").strip().strip("\"'")
        if not clean_target_dir:
            clean_target_dir = self._infer_file_move_target_dir(text)
        if not clean_target_dir:
            raise ValueError("没有识别到目标目录。")
        target_dir_path = self._normalize_release_dir(Path(clean_target_dir))
        return target_dir_path / source.name

    def _infer_file_move_target_dir(self, text: str) -> str:
        clean = str(text or "").strip()
        lowered = clean.lower()
        project_root = self._project_root()
        if "项目根目录" in clean and ("release" in lowered or "realse" in lowered):
            return str(project_root / "release")
        if "release" in lowered or "realse" in lowered:
            return str(project_root / "release")
        match = re.search(r"(?:放到|移动到|复制到|拷贝到|挪到|到)\s*([A-Za-z]:[\\/][^，,。；;\r\n]+)", clean)
        if match:
            target = match.group(1).strip().strip("\"'")
            target = re.sub(r"(里面|里|目录|下|中)$", "", target).strip()
            return target
        return ""

    def _resolve_file_move_operation(self, operation: str, text: str) -> str:
        clean_operation = str(operation or "").strip().lower()
        if clean_operation in {"copy", "move"}:
            return clean_operation
        lowered = str(text or "").lower()
        if any(term in lowered for term in ("复制", "拷贝", "copy")):
            return "copy"
        if any(term in lowered for term in ("移动", "挪到", "剪切", "move")):
            return "move"
        return "copy"

    def _project_root(self) -> Path:
        if self.oppo_config_path:
            return self.oppo_config_path.parent.parent.resolve()
        if self.packaging_config_path:
            return self.packaging_config_path.parent.parent.resolve()
        return Path.cwd().resolve()

    @staticmethod
    def _normalize_release_dir(path: Path) -> Path:
        if path.name.lower() == "realse":
            return path.with_name("release")
        return path

    @staticmethod
    def _path_is_inside(path: Path, root: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_file_paths_from_text(text: str) -> list[str]:
        matches = re.findall(
            r"([A-Za-z]:[\\/][^\"'<>|\r\n]+?\.(?:apk|aab|png|jpe?g|pdf|zip|xlsx?|json|txt))",
            str(text or ""),
            flags=re.IGNORECASE,
        )
        return [item.strip().strip("\"'，,。；;") for item in matches]

    @staticmethod
    def _extract_generic_app_search_query(text: str) -> str:
        clean = (text or "").strip()
        if not clean.startswith(("搜索", "查找")):
            return ""
        if clean.startswith(("搜索竞品", "搜索应用商店", "查找竞品")):
            return ""
        query = re.sub(r"^(搜索|查找)(应用|app|APP)?", "", clean).strip()
        query = re.sub(r"^[：:，,。！？?\s]+", "", query).strip()
        return _clean_market_query(query)

    @staticmethod
    def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
        return any(term in text for term in terms)

    def _looks_like_help_request(self, text: str) -> bool:
        return self._contains_any(text, ("帮助", "怎么用", "如何使用", "能做什么", "指令", "命令", "使用说明"))

    def _looks_like_project_logic_request(self, text: str) -> bool:
        if self._contains_any(text, ("项目逻辑", "逻辑说明", "整体逻辑", "配置逻辑", "工具总结")):
            return True
        meta_terms = ("记忆", "memory", "skill", "工具调用", "工具判断", "工具怎么", "调用怎么", "路由")
        ask_terms = ("怎么处理", "怎么判断", "怎么写", "看一下", "帮我看", "说明", "总结", "你的")
        return self._contains_any(text, meta_terms) and self._contains_any(text, ask_terms)

    def _looks_like_clear_session_request(self, text: str) -> bool:
        return self._contains_any(text, ("清空", "清除", "删除", "重置")) and self._contains_any(
            text,
            ("当前记录", "当前状态", "当前会话", "这次记录", "本会话记录", "记忆", "上下文"),
        )

    def _looks_like_clear_all_request(self, text: str) -> bool:
        return self._contains_any(text, ("清空", "清除", "删除", "重置")) and self._contains_any(
            text,
            ("所有记录", "全部记录", "所有会话", "全部会话", "全局记录"),
        )

    def _looks_like_session_status_request(self, text: str) -> bool:
        return self._contains_any(text, ("当前状态", "现在状态", "会话状态", "当前记录", "输出记录", "输出当前记录", "记录了什么", "进度", "现在到哪"))

    def _looks_like_default_app_question(self, text: str) -> bool:
        return self._contains_any(text, ("默认应用", "当前应用", "这个应用")) and self._contains_any(
            text,
            ("是什么", "哪个", "是谁", "什么", "现在"),
        )

    def _looks_like_oppo_status_request(self, text: str) -> bool:
        return self._contains_any(text, ("审核状态", "审核进度", "oppo状态", "oppo 状态", "查审核", "查询审核"))

    def _looks_like_oppo_app_list_request(self, text: str) -> bool:
        return self._contains_any(text, ("oppo", "开放平台")) and self._contains_any(
            text,
            ("创建的应用", "已创建应用", "创建应用", "应用列表", "所有应用", "全部应用"),
        )

    def _looks_like_submission_check_request(self, text: str) -> bool:
        return self._contains_any(
            text,
            ("提交检查", "检查提交", "提交前检查", "校验配置", "能不能提交", "是否可以提交", "能否提交", "发布检查"),
        )

    def _looks_like_submit_prepare_request(self, text: str) -> bool:
        return self._contains_any(text, ("准备提交", "提交前要做", "提交前需要", "发版前", "上线前"))

    def _looks_like_oppo_submit_confirmation(self, text: str) -> bool:
        lowered = str(text or "").lower()
        if "oppo" not in lowered and "开放平台" not in lowered:
            return False
        return self._contains_any(
            lowered,
            (
                "确认提审",
                "确认提交",
                "正式提审",
                "正式提交",
                "立即提审",
                "立即提交",
                "马上提审",
                "马上提交",
                "开始提审",
                "开始提交",
                "提交到oppo",
                "提交到 oppo",
            ),
        )

    def _looks_like_last_image_analysis_request(self, text: str) -> bool:
        return self._contains_any(text, ("图片", "截图", "这张图", "最近图片")) and self._contains_any(
            text,
            ("分析", "识别", "看看", "看下"),
        )

    def _looks_like_rejection_analysis_request(self, text: str) -> bool:
        return self._contains_any(text, ("驳回", "拒绝", "不通过", "审核失败")) and self._contains_any(
            text,
            ("分析", "原因", "为什么", "看看", "怎么回事"),
        )

    def _looks_like_remediation_request(self, text: str) -> bool:
        return self._contains_any(text, ("整改清单", "待办清单", "整改待办", "怎么改", "怎么整改", "如何整改", "修复建议", "处理清单"))

    def _looks_like_view_config_request(self, text: str) -> bool:
        return self._contains_any(text, ("配置", "提交配置")) and self._contains_any(
            text,
            ("查看", "看看", "看下", "展示", "显示", "当前"),
        )

    def _looks_like_config_update_request(self, text: str) -> bool:
        return self._contains_any(text, ("配置", "字段", "submission.", "credentials.", "feishu.")) and self._contains_any(
            text,
            ("设置", "修改", "改成", "更新", "暂存", "="),
        )

    def _looks_like_confirm_config_request(self, text: str) -> bool:
        return self._contains_any(text, ("确认", "保存", "写入")) and self._contains_any(text, ("配置", "修改", "变更"))

    def _looks_like_cancel_config_request(self, text: str) -> bool:
        return self._contains_any(text, ("取消", "放弃", "不要保存")) and self._contains_any(text, ("配置", "修改", "变更"))

    def _looks_like_bind_material_request(self, text: str) -> bool:
        return self._contains_any(text, ("绑定", "关联", "作为")) and self._contains_any(
            text,
            ("材料", "apk", "图标", "截图", "版权", "icp", "证明", "上传"),
        )

    def _looks_like_file_move_request(self, text: str) -> bool:
        lowered = str(text or "").lower()
        if self._looks_like_confirm_file_move_request(text) or self._looks_like_cancel_file_move_request(text):
            return False
        if self._contains_any(lowered, ("设置提交配置", "批量设置提交配置", "submission.", "packaging.", "绑定材料")):
            return False
        if not self._contains_any(lowered, ("apk", ".aab", ".png", ".jpg", ".jpeg", ".pdf", "文件", "这个包")):
            return False
        return self._contains_any(
            lowered,
            ("放到", "移动到", "复制到", "拷贝到", "挪到", "移动", "复制", "拷贝", "release", "realse"),
        )

    def _looks_like_confirm_file_move_request(self, text: str) -> bool:
        return self._contains_any(str(text or ""), ("确认移动文件", "确认复制文件", "确认文件移动", "确认文件操作"))

    def _looks_like_cancel_file_move_request(self, text: str) -> bool:
        return self._contains_any(str(text or ""), ("取消移动文件", "取消复制文件", "取消文件移动", "取消文件操作"))

    def _looks_like_record_app_request(self, text: str) -> bool:
        return self._contains_any(text, ("记录", "保存", "登记")) and self._contains_any(
            text,
            ("应用信息", "应用名", "包名", "版本号", "版本", "app信息"),
        )

    def _parse_market_semantic_intent(self, text: str, session_id: str) -> tuple[str, str] | None:
        if _looks_like_app_store_data_platform_research(text):
            return None
        lowered = text.lower()
        market_terms = ("竞品", "对标", "同类", "同类型", "类似", "应用商店", "应用", "app", "软件")
        search_terms = ("找", "搜索", "查", "看看", "有哪些", "分析", "调研", "研究")
        record_terms = ("记录", "保存", "月度", "每月", "月报")
        has_market_context = any(term in lowered for term in market_terms)
        if not has_market_context:
            return None
        query = self._extract_market_semantic_query(text, session_id)
        if any(term in lowered for term in record_terms):
            return "market_download_snapshot", query
        if any(term in lowered for term in search_terms):
            return "market_search", query
        return None

    def _extract_market_semantic_query(self, text: str, session_id: str) -> str:
        payload = self._extract_payload(text)
        if payload:
            return payload
        quoted = re.search(r"[“\"']([^”\"']+)[”\"']", text)
        if quoted:
            return quoted.group(1).strip()
        clean = text
        clean = re.sub(r"(帮我|请|麻烦|给我|把|一下|看看|看下|查下|搜下|找下)", "", clean)
        clean = re.sub(r"(搜索|查找|查询|找|分析|调研|研究|有哪些|有没有|统计|保存|记录)", "", clean)
        clean = re.sub(r"(应用商店|各大应用市场|应用市场|市场|APP|app|应用|软件)", "", clean)
        clean = re.sub(r"(竞品|对标产品|对标|同类型|同类|类似|相似)", "", clean)
        clean = re.sub(r"(下载数据|下载量|下载|月度|每月|月报|指标|数据)", "", clean)
        clean = re.sub(r"(这个|该|当前|默认|它|关于|相关|方面|的)", "", clean)
        clean = re.sub(r"[：:，,。！？?\s]+", " ", clean).strip()
        if clean:
            return clean
        session = self.state_store.get_session(session_id)
        analysis = session.get("last_rejection_analysis") or {}
        app_info = self._default_app_info(session_id)
        return str(analysis.get("similar_app") or app_info.get("app_name") or "").strip()

    @staticmethod
    def _handle_app_store_data_platform_research(
        text: str,
        query: str = "",
        *,
        llm_intent: str = "",
    ) -> AgentResponse | None:
        if not _looks_like_app_store_data_platform_research(text, query):
            return None
        return AgentResponse(
            _format_app_store_data_platform_research(),
            {
                "intent": "app_store_data_platform_research",
                "query": _clean_market_query(query),
                "redirected_from": llm_intent or "market_search",
            },
        )

    @staticmethod
    def _extract_assignment_payload(text: str) -> str:
        match = re.search(r"([A-Za-z_][\w.]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|\S+))", text.strip())
        return match.group(1).strip() if match else ""

    @staticmethod
    def _extract_material_label(text: str) -> str:
        lowered = text.lower()
        labels = [
            ("apk", "APK"),
            ("安装包", "APK"),
            ("图标", "图标"),
            ("截图1", "截图1"),
            ("截图2", "截图2"),
            ("截图3", "截图3"),
            ("截图4", "截图4"),
            ("截图5", "截图5"),
            ("截图", "截图1"),
            ("版权", "版权证明"),
            ("icp", "ICP证明"),
            ("备案", "ICP证明"),
            ("证明", "版权证明"),
        ]
        for needle, label in labels:
            if needle in lowered:
                return label
        return ""

    @staticmethod
    def _extract_record_app_payload(text: str) -> str:
        clean = re.sub(r"(帮我|请|麻烦|记录|保存|登记|一下|应用信息|app信息)", "", text, flags=re.IGNORECASE)
        clean = re.sub(r"(应用名|包名|版本号|版本)", "", clean)
        clean = re.sub(r"[：:，,\s]+", " ", clean).strip()
        return clean

    @staticmethod
    def _extract_version_code(text: str) -> str | None:
        payload = ReviewAgent._extract_payload(text)
        if not payload:
            parts = text.split()
            payload = parts[-1] if len(parts) > 1 else ""
        return payload.strip() or None

    @staticmethod
    def _parse_app_info(payload: str) -> JsonDict:
        pieces = [piece.strip() for piece in re.split(r"[/,，\s]+", payload or "") if piece.strip()]
        return {
            "app_name": pieces[0] if len(pieces) > 0 else "",
            "pkg_name": pieces[1] if len(pieces) > 1 else "",
            "version_code": pieces[2] if len(pieces) > 2 else "",
        }

    @staticmethod
    def _format_record_app_info(app_info: JsonDict) -> str:
        return (
            "应用信息已记录\n"
            "\n应用信息：\n"
            f"- 应用名：{app_info.get('app_name') or '未提供'}\n"
            f"- 包名：{app_info.get('pkg_name') or '未提供'}\n"
            f"- 版本号：{app_info.get('version_code') or '未提供'}"
        )

    @staticmethod
    def _format_rejection_analysis(analysis: JsonDict) -> str:
        lines = ["驳回分析"]
        conclusion = "可以尝试提交" if analysis.get("can_resubmit_same_apk") else "不建议原包直接重提"
        lines.append("")
        lines.append("结论：")
        lines.append(f"- 结论：{conclusion}")
        details_added = False
        if analysis.get("similarity_score") is not None:
            if not details_added:
                lines.append("")
                lines.append("识别信息：")
                details_added = True
            lines.append(f"- APK 相似度：{analysis['similarity_score']}")
        if analysis.get("similar_app"):
            if not details_added:
                lines.append("")
                lines.append("识别信息：")
                details_added = True
            lines.append(f"- 疑似相似应用：{analysis['similar_app']}")
        if analysis.get("required_actions"):
            lines.append("")
            lines.append("需要做：")
            lines.extend(f"- {item}" for item in analysis["required_actions"][:3])
        if analysis.get("evidence_targets"):
            targets = [target.replace("OPPO backend: ", "") for target in analysis["evidence_targets"]]
            lines.append("")
            lines.append("上传位置：" + " / ".join(targets))
        return "\n".join(lines)

    @staticmethod
    def _format_session(session: JsonDict) -> str:
        if not session:
            return "当前会话还没有记录应用信息。可以发送“记录应用：应用名 / 包名 / 版本号”。"
        app_info = session.get("app_info") or {}
        analysis = session.get("last_rejection_analysis") or {}
        image_analysis = session.get("last_image_analysis") or {}
        image_line = ""
        if image_analysis:
            image_line = f"\n- 最近图片识别：{image_analysis.get('summary') or '已记录'}"
        remediation = session.get("remediation_checklist") or []
        remediation_line = f"\n- 整改待办：{len(remediation)} 项" if remediation else ""
        market_search = session.get("last_market_search") or {}
        market_line = ""
        if market_search:
            market_line = (
                f"\n- 最近应用商店查询：{market_search.get('query') or '已记录'}"
                f"（{len(market_search.get('apps') or [])} 个结果）"
            )
        snapshots = session.get("market_download_snapshots") or {}
        snapshot_line = f"\n- 竞品月度记录：{len(snapshots)} 个月" if snapshots else ""
        preferences = session.get("market_store_preferences") or {}
        disabled_stores = [_store_label(store) for store in preferences.get("disabled_stores") or []]
        preference_line = f"\n- 应用商店偏好：不查询 {'、'.join(disabled_stores)}" if disabled_stores else ""
        pending_file_move = session.get("pending_file_move") or {}
        pending_file_move_line = ""
        if pending_file_move:
            pending_file_move_line = f"\n- 待确认文件操作：{pending_file_move.get('source_path')} -> {pending_file_move.get('target_path')}"
        memory = session.get("agent_memory") or []
        structured_memory = session.get("long_term_memory") or {}
        memory_count = len(memory) or len(structured_memory.get("notes") or [])
        preference_count = len((structured_memory.get("preferences") or {}))
        memory_line = f"\n- 长期记忆：{memory_count} 条" if memory_count else ""
        structured_line = f"\n- 结构化偏好：{preference_count} 类" if preference_count else ""
        return (
            "当前会话状态\n"
            "\n应用信息：\n"
            f"- 应用名：{app_info.get('app_name') or '未记录'}\n"
            f"- 包名：{app_info.get('pkg_name') or '未记录'}\n"
            f"- 版本号：{app_info.get('version_code') or '未记录'}\n"
            "\n进度概览：\n"
            f"- 是否建议同包重提："
            f"{'未知' if not analysis else ('是' if analysis.get('can_resubmit_same_apk') else '否')}"
            f"{image_line}"
            f"{remediation_line}"
            f"{market_line}"
            f"{snapshot_line}"
            f"{preference_line}"
            f"{pending_file_move_line}"
            f"{memory_line}"
            f"{structured_line}"
        )

    @staticmethod
    def _build_submit_checklist(session: JsonDict) -> str:
        app_info = session.get("app_info") or {}
        analysis = session.get("last_rejection_analysis") or {}
        missing = []
        if not app_info.get("pkg_name"):
            missing.append("包名")
        if not app_info.get("version_code"):
            missing.append("版本号")
        if analysis and not analysis.get("can_resubmit_same_apk"):
            missing.append("确认是否强制同包重提，或先修改 APK/补充申诉材料")
        if analysis and "missing_icp_proof" in analysis.get("categories", []):
            missing.append("ICP 备案网站证明")

        if not missing:
            return "提交前检查：当前会话没有发现明显缺口。确认无误后发送“确认提审 OPPO”。"
        return "提交前检查发现待补项：\n" + "\n".join(f"- {item}" for item in missing)

    @staticmethod
    def _format_oppo_status(status: JsonDict) -> str:
        app_info = status.get("app_info") or {}
        task = status.get("task") or {}
        state_label = {
            "approved": "审核通过",
            "published": "已发布",
            "rejected": "审核不通过",
            "reviewing": "审核中",
            "unknown": "未知",
        }.get(str(status.get("review_state")), str(status.get("review_state") or "未知"))
        audit_text = app_info.get("audit_status_name") or app_info.get("state_name") or state_label
        lines = ["OPPO 审核状态", "", "应用信息：", f"- 应用：{status.get('pkg_name')}", f"- 版本：{status.get('version_code')}", "", "审核结果：", f"- 状态：{audit_text}"]
        if status.get("app_created") is False:
            lines.append("- 应用详情：未确认已在当前 OPPO 开发者账号创建")
        if task.get("error"):
            lines.append(f"- 提交任务：查询失败（{_shorten(task['error'])}）")
        elif task:
            task_text = _format_oppo_task_state(task)
            if task_text:
                lines.append(f"- 提交任务：{task_text}")
            if task.get("err_msg"):
                lines.append(f"- 任务错误：{_shorten(task['err_msg'])}")
        if status.get("review_state") == "rejected":
            reason = extract_rejection_reason(app_info)
            if reason:
                lines.append(f"- 驳回原因：{_shorten(reason)}")
        return "\n".join(lines)

    @staticmethod
    def _format_oppo_submit_result(result: JsonDict) -> str:
        app_info = result.get("app_info") or {}
        release = result.get("release") or {}
        task = result.get("task") or {}
        review = result.get("review") or {}
        task_state = str(task.get("task_state") or "")
        if task_state == "3":
            status_text = "任务失败"
        elif task_state == "2":
            status_text = "提交任务完成"
        elif task_state == "1":
            status_text = "OPPO 处理中"
        else:
            status_text = "接口已接收"
        lines = [
            "OPPO 自动提审",
            "",
            "提交结果：",
            f"- 状态：{status_text}",
            f"- 包名：{result.get('pkg_name') or app_info.get('pkg_name') or '未知'}",
            f"- 版本：{result.get('version_code') or '未知'}",
        ]
        if app_info.get("app_name"):
            lines.append(f"- 应用：{app_info['app_name']}")
        if app_info.get("app_id"):
            lines.append(f"- OPPO 应用 ID：{app_info['app_id']}")
        task_id = release.get("task_id") or release.get("id")
        if task_id:
            lines.append(f"- 提交任务 ID：{task_id}")
        if task:
            task_text = _format_oppo_task_state(task)
            if task_text:
                lines.append(f"- 任务状态：{task_text}")
            if task.get("err_msg"):
                lines.append(f"- 任务错误：{_shorten(task['err_msg'])}")
        if review:
            review_state = review.get("state")
            if review_state:
                lines.append(f"- 审核状态：{review_state}")
        return "\n".join(lines)

    @staticmethod
    def _format_oppo_app_list(result: JsonDict) -> str:
        apps = result.get("apps") or []
        errors = result.get("errors") or []
        lines = [
            "OPPO 已创建应用查询",
            "",
            "查询方式：",
            "- 按当前会话/config/packlist 中的包名逐个查询 OPPO 应用信息接口。",
            "- 只把返回 app_id、应用名、开发者 ID、创建时间等身份字段的记录计为已创建。",
            "",
            "结果：",
            f"- 已确认创建：{len(apps)} 个",
            f"- 已查询包名：{result.get('queried_count') or 0} / {result.get('total_candidates') or 0}",
        ]
        if result.get("truncated"):
            lines.append("- 说明：候选包名较多，本次只查询了前一部分。")
        if errors:
            lines.append(f"- 未匹配或查询失败：{len(errors)} 个")
        if not apps:
            lines.extend(["", "应用列表：", "- 暂未确认到已创建应用。"])
            return "\n".join(lines)

        lines.extend(["", "应用列表："])
        for index, app in enumerate(apps[:30], start=1):
            name = app.get("app_name") or app.get("pkg_name") or "未命名应用"
            details = [str(name)]
            if app.get("pkg_name"):
                details.append(str(app["pkg_name"]))
            if app.get("audit_status_name"):
                details.append(f"状态：{app['audit_status_name']}")
            if app.get("app_id"):
                details.append(f"OPPO应用ID：{app['app_id']}")
            if app.get("app_create_time"):
                details.append(f"创建时间：{app['app_create_time']}")
            lines.append(f"{index}. " + "，".join(details))
        if len(apps) > 30:
            lines.append(f"... 还有 {len(apps) - 30} 个未展示。")
        return "\n".join(lines)

    @staticmethod
    def _format_submission_check(validation: JsonDict, session: JsonDict) -> str:
        lines = ["提交检查", "", "检查结果：", f"- 配置文件：{'通过' if validation.get('valid') else '不通过'}"]
        missing_fields = validation.get("missing_required_fields") or []
        missing_files = validation.get("missing_files") or []
        app_check = validation.get("app_check") or {}
        if app_check:
            if app_check.get("created"):
                app_info = app_check.get("app_info") or {}
                label = app_info.get("app_name") or app_info.get("name") or app_info.get("pkg_name") or "已创建"
                lines.append(f"- OPPO 应用：已确认创建（{label}）")
            else:
                lines.append(f"- OPPO 应用：未确认创建（{_shorten(app_check.get('error'))}）")
        if missing_fields:
            lines.append("- 缺字段：" + "、".join(str(item) for item in missing_fields[:8]))
        if missing_files:
            lines.append("- 缺文件：" + "、".join(str(item) for item in missing_files[:5]))
        analysis = session.get("last_rejection_analysis") or {}
        if analysis and not analysis.get("can_resubmit_same_apk"):
            lines.append("")
            lines.append("风险提示：")
            lines.append("- 风险：最近驳回分析显示不建议原包直接重提")
        checklist = session.get("remediation_checklist") or ReviewAgent._build_remediation_items(analysis)
        if checklist:
            lines.append(f"- 整改待办：{len(checklist)} 项，发送“整改清单”查看")
        lines.append("")
        lines.append("结论：")
        app_ready = not app_check or bool(app_check.get("created"))
        if validation.get("valid") and app_ready and not (analysis and not analysis.get("can_resubmit_same_apk")):
            lines.append("结论：可以进入人工确认提交步骤。")
        else:
            lines.append("结论：先补齐缺口或处理风险，再提交。")
        return "\n".join(lines)

    def _handle_packaging_fallback(self, session_id: str, text: str) -> AgentResponse | None:
        if self._looks_like_packaging_request(text.lower()):
            parsed = parse_package_request(text)
            if not parsed["app_name"] and not parsed["pkg_name"] and not parsed["channels"] and not parsed["batch"]:
                session = self.state_store.get_session(session_id)
                lookup = session.get("last_package_lookup") or {}
                matches = lookup.get("matches") or []
                if self._contains_any(text, ("这个应用", "这个包", "刚才这个", "上面这个")) and len(matches) == 1:
                    parsed["pkg_name"] = str(matches[0].get("pkg_name") or "")
                else:
                    return None
            intent = "batch_package" if parsed["batch"] else "package_apk"
            return self._run_packaging_intent(session_id, text, {"intent": intent, **parsed})

        if self._looks_like_packaging_lookup(text):
            query = self._extract_packaging_lookup_query(text)
            if not query:
                return None
            return self._run_packaging_lookup(session_id, text, {"intent": "package_lookup", "query": query})
        return None

    def _handle_packaging_followup(self, session_id: str, text: str) -> AgentResponse | None:
        if not self._looks_like_packaging_pagination_request(text):
            return None
        session = self.state_store.get_session(session_id)
        lookup = session.get("last_package_lookup") or {}
        matches = lookup.get("matches") or []
        if not matches:
            return None
        page_size = self._extract_packaging_page_size(text) or int(lookup.get("page_size") or 10)
        next_offset = int(lookup.get("next_offset") or 0)
        offset = next_offset
        if _looks_like_last_packaging_page_request(text):
            offset = max(0, len(matches) - page_size)
        return self._render_packaging_lookup_page(
            session_id,
            query=str(lookup.get("query") or ""),
            matches=matches,
            offset=offset,
            page_size=page_size,
            heading=str(lookup.get("heading") or ""),
        )

    def _handle_packaging_catalog_request(self, session_id: str, text: str) -> AgentResponse | None:
        lowered = text.lower()
        if not self._looks_like_packaging_catalog_request(lowered):
            return None
        try:
            entries = self._all_packaging_entries()
        except Exception as exc:
            return AgentResponse(
                _format_error_message("读取可打包列表失败", str(exc), ["检查 packaging.project_dir 或 packlist 快照配置。"]),
                {"intent": "package_lookup", "error": str(exc)},
            )
        if not entries:
            return AgentResponse(
                _format_error_message("没有可用打包包信息", "当前 packlist 里还没有可用的打包包信息。", ["检查 packlist.xls 或 packlist-scan.json。"]),
                {"intent": "package_lookup", "matches": []},
            )
        return self._render_packaging_lookup_page(
            session_id,
            query="全部",
            matches=[entry.to_dict() for entry in entries],
            offset=0,
            page_size=10,
            heading=f"当前可打包包列表（共 {len(entries)} 个）：",
        )

    def _run_packaging_intent(self, session_id: str, text: str, decision: JsonDict) -> AgentResponse | None:
        parsed = parse_package_request(text)
        dry_run = bool(decision.get("dry_run")) or bool(parsed.get("dry_run"))
        app_names = _normalize_string_list(decision.get("app_names") or [])
        channels = _normalize_string_list(
            decision.get("channels") or parsed.get("channels") or []
        )
        try:
            if decision.get("intent") == "batch_package" or parsed.get("batch"):
                if channels:
                    results: list[JsonDict] = []
                    for channel in channels:
                        try:
                            result = self.packaging_agent.package_one(
                                channels=[channel],
                                dry_run=dry_run,
                            )
                            results.append({"ok": True, **result})
                        except Exception as exc:
                            results.append({"ok": False, "name": channel, "error": str(exc)})
                    return AgentResponse(
                        format_batch_package_result(results, dry_run=dry_run),
                        {"intent": "batch_package", "result": results, "dry_run": dry_run},
                    )
                if app_names:
                    result = self.packaging_agent.package_batch_by_app_names(
                        app_names, dry_run=dry_run, continue_on_error=True,
                    )
                else:
                    result = self.packaging_agent.package_batch(dry_run=dry_run, continue_on_error=True)
                self.state_store.update_session(
                    session_id,
                    {"last_batch_package_result": _json_safe(result)},
                )
                return AgentResponse(
                    format_batch_package_result(result, dry_run=dry_run),
                    {"intent": "batch_package", "result": result, "dry_run": dry_run},
                )
            result = self.packaging_agent.package_one(
                app_name=str(decision.get("app_name") or parsed.get("app_name") or ""),
                pkg_name=str(decision.get("pkg_name") or parsed.get("pkg_name") or ""),
                channels=_normalize_string_list(decision.get("channels") or parsed.get("channels") or []),
                dry_run=dry_run,
            )
            self.state_store.update_session(
                session_id,
                {"last_package_result": _json_safe(result)},
            )
            return AgentResponse(
                format_package_result(result, dry_run=dry_run),
                {"intent": "package_apk", "result": result, "dry_run": dry_run},
            )
        except Exception as exc:
            return AgentResponse(
                _format_error_message("打包失败", str(exc), ["检查 packaging.project_dir、packaging.script 和 packlist 配置。", "修正后重新发送打包指令。"]),
                {"intent": "package_apk" if not parsed.get("batch") else "batch_package", "error": str(exc)},
            )

    def _looks_like_packaging_request(self, text: str) -> bool:
        return self._contains_any(
            text,
            ("打包", "构建", "编译apk", "编译 apk", "package-apk", "batch-package", "package apk"),
        )

    def _handle_material_index_request(
        self,
        session_id: str,
        text: str,
        sender_id: str | None = None,
    ) -> AgentResponse | None:
        if not self._looks_like_material_index_request(text):
            return None
        parsed = self._parse_material_index_request(text)
        return self.index_submission_materials(
            session_id,
            app_name=parsed.get("app_name", ""),
            pkg_name=parsed.get("pkg_name", ""),
            materials_root=parsed.get("materials_root", ""),
            sender_id=sender_id,
            source_text=text,
        )

    def _looks_like_material_index_request(self, text: str) -> bool:
        return self._contains_any(
            text,
            (
                "索引上架资源",
                "查找上架资源",
                "查找上架材料",
                "索引上架材料",
                "填充上架材料",
                "匹配上架材料",
                "找上架材料",
                "找上架资源",
            ),
        )

    def _parse_material_index_request(self, text: str) -> JsonDict:
        clean = str(text or "").strip()
        pkg_match = re.search(r"([A-Za-z][\w]*(?:\.[A-Za-z][\w]*){2,})", clean)
        path_match = re.search(r"(?:路径|目录|root|materials_root)\s*[:：]?\s*([A-Za-z]:[\\/][^，,。；;]+)", clean, flags=re.IGNORECASE)
        payload = self._extract_payload(clean)
        if not payload:
            payload = re.sub(
                r"^(帮我|请|麻烦|可以|把|为)?\s*(索引上架资源|查找上架资源|查找上架材料|索引上架材料|填充上架材料|匹配上架材料|找上架材料|找上架资源)\s*",
                "",
                clean,
            )
        materials_root = path_match.group(1).strip().strip("\"'") if path_match else ""
        app_name = payload.strip()
        if pkg_match:
            app_name = app_name.replace(pkg_match.group(1), "").strip()
        if materials_root:
            app_name = app_name.replace(materials_root, "").strip()
        app_name = re.sub(r"(路径|目录|root|materials_root)\s*[:：]?\s*$", "", app_name, flags=re.IGNORECASE).strip()
        app_name = re.sub(r"[：:，,。！？?\s]+$", "", app_name).strip()
        return {
            "app_name": app_name,
            "pkg_name": pkg_match.group(1) if pkg_match else "",
            "materials_root": materials_root,
        }

    def _looks_like_packaging_lookup(self, text: str) -> bool:
        return self._contains_any(
            text,
            (
                "查包",
                "查渠道",
                "对应什么包",
                "是什么包",
                "是什么渠道",
                "包名",
                "渠道名",
                "有哪些包",
                "那些包",
                "哪些包",
                "什么年级的包",
                "都有那些年级的包",
                "都有哪些年级的包",
                "那些年级的包",
                "哪些年级的包",
                "都有那些包",
                "都有哪些包",
            ),
        )

    def _run_packaging_lookup(self, session_id: str, text: str, decision: JsonDict) -> AgentResponse | None:
        explicit_query = str(decision.get("app_name") or decision.get("query") or "").strip()
        query = explicit_query or self._extract_packaging_lookup_query(text)
        offset = _optional_int(decision.get("offset"))
        page_size = _optional_int(decision.get("page_size"))
        if _is_all_packaging_query(query):
            try:
                entries = self._all_packaging_entries()
            except Exception as exc:
                return AgentResponse(
                    _format_error_message("读取可打包列表失败", str(exc), ["检查 packaging.project_dir 或 packlist 快照配置。"]),
                    {"intent": "package_lookup", "error": str(exc)},
                )
            if not entries:
                return AgentResponse(
                    _format_error_message("没有可用打包包信息", "当前 packlist 里还没有可用的打包包信息。", ["检查 packlist.xls 或 packlist-scan.json。"]),
                    {"intent": "package_lookup", "matches": []},
                )
            return self._render_packaging_lookup_page(
                session_id,
                query="全部",
                matches=[entry.to_dict() for entry in entries],
                offset=offset or 0,
                page_size=page_size or self._extract_packaging_page_size(text) or 10,
                heading=f"当前可打包包列表（共 {len(entries)} 个）：",
            )
        if not query:
            return AgentResponse(
                _format_error_message(
                    "查包缺少应用名",
                    "没有识别到要查询的应用名。",
                    ["请告诉我应用名，比如“八年级语文下册”或“帮我查四年级英语上册对应什么包”。"],
                ),
                {"intent": "package_lookup", "missing": "app_name"},
            )
        try:
            matches = self._resolve_packaging_lookup(query)
        except Exception as exc:
            return AgentResponse(
                _format_error_message("查包失败", str(exc), ["检查 packaging.project_dir 或 packlist 快照配置。"]),
                {"intent": "package_lookup", "error": str(exc)},
            )
        if not matches:
            return AgentResponse(
                _format_error_message("未找到对应包", f"没找到和“{query}”对应的包。", ["换一个更完整的应用名再查。", "也可以发送“都可以打哪些包”查看列表。"]),
                {"intent": "package_lookup", "query": query, "matches": []},
            )
        resolved_page_size = page_size or self._extract_packaging_page_size(text) or 10
        resolved_offset = offset or 0
        if decision.get("last_page") or (not explicit_query and _looks_like_last_packaging_page_request(text)):
            resolved_offset = max(0, len(matches) - resolved_page_size)
        return self._render_packaging_lookup_page(
            session_id,
            query=query,
            matches=[entry.to_dict() for entry in matches],
            offset=resolved_offset,
            page_size=resolved_page_size,
        )

    def _extract_packaging_lookup_query(self, text: str) -> str:
        payload = self._extract_payload(text)
        if payload:
            return payload
        clean = re.sub(r"^(帮我|请|麻烦|能不能|可以|我要|我想|帮我查|帮我找)\s*", "", text)
        clean = re.sub(r"(你一个个查|查一下|查下|查询一下|查询|查一查|帮我查|帮我找|看一下|看下)", "", clean)
        clean = re.sub(
            r"(对应什么包|是什么包|是什么渠道|查包|查渠道|这个包|这个应用|包名|渠道名|都有那些包|都有哪些包|有哪些包|那些包|哪些包|什么年级的包|都有那些年级的包|都有哪些年级的包|那些年级的包|哪些年级的包)",
            "",
            clean,
        )
        clean = re.sub(r"[：:，,。！？?\s]+", " ", clean).strip()
        return clean

    @staticmethod
    def _extract_packaging_script_update(text: str) -> str:
        clean = str(text or "").strip()
        if not clean or "package.js" not in clean.lower():
            return ""
        if not any(term in clean for term in ("路径", "脚本", "package.js", "打包脚本")):
            return ""
        match = re.search(r"([A-Za-z]:[\\/][^\s，,。；;\"'`]+package\.js)", clean, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        assignment = re.search(r"(?:packaging\.script|script|script_path|package_script_path)\s*=\s*(.+)$", clean, flags=re.IGNORECASE)
        if assignment:
            value = assignment.group(1).strip().strip("\"'")
            if value.lower().endswith("package.js"):
                return value
        return ""

    def _packaging_project_dir(self) -> Path:
        if self.packaging_agent.settings.project_dir:
            return self.packaging_agent.settings.project_dir
        raise OppoError("还没有配置 packaging.project_dir，暂时不能查包。")

    def _resolve_packaging_lookup(self, query: str):
        try:
            entries = scan_packlist(self._packaging_project_dir())
        except Exception as primary_exc:
            snapshot = self._packaging_packlist_snapshot()
            if not snapshot:
                raise primary_exc
            entries = scan_packlist_snapshot(snapshot)

        matches = resolve_packlist_app_name_entries(entries, query)
        if matches:
            return matches

        return resolve_packlist_channel_entries(entries, query)

    def _all_packaging_entries(self):
        try:
            return scan_packlist(self._packaging_project_dir())
        except Exception as primary_exc:
            snapshot = self._packaging_packlist_snapshot()
            if not snapshot:
                raise primary_exc
            return scan_packlist_snapshot(snapshot)

    def _oppo_app_list_candidates(self, session_id: str = "") -> list[str]:
        candidates: list[str] = []
        session_app = self.state_store.get_session(session_id).get("app_info") if session_id else {}
        if isinstance(session_app, dict) and session_app.get("pkg_name"):
            candidates.append(str(session_app["pkg_name"]))
        config_app = self._config_app_info()
        if config_app.get("pkg_name"):
            candidates.append(str(config_app["pkg_name"]))
        try:
            candidates.extend(str(entry.pkg_name or "") for entry in self._all_packaging_entries())
        except Exception:
            pass
        seen: set[str] = set()
        result: list[str] = []
        for pkg_name in candidates:
            clean = str(pkg_name or "").strip()
            if clean and clean not in seen:
                seen.add(clean)
                result.append(clean)
        if not result:
            raise OppoError("没有可用于查询的包名；请先配置 packlist 快照或 submission.pkg_name")
        return result

    def _render_packaging_lookup_page(
        self,
        session_id: str,
        *,
        query: str,
        matches: list[JsonDict],
        offset: int,
        page_size: int,
        heading: str = "",
    ) -> AgentResponse:
        total = len(matches)
        safe_page_size = max(1, page_size)
        safe_offset = max(0, min(offset, total))
        page = matches[safe_offset : safe_offset + safe_page_size]
        if not page:
            return AgentResponse(
                "已经没有更多结果了。",
                {"intent": "package_lookup", "query": query, "matches": matches, "page_size": safe_page_size, "next_offset": total},
            )

        lines = ["查包结果", "", "查询条件：", f"- 关键词：{query}", f"- 总数：{total}", "", "包列表："]
        for item in page:
            lines.extend(
                [
                    f"- 应用名：{item.get('app_name') or ''}",
                    f"  包名：{item.get('pkg_name') or ''}",
                    f"  渠道：{item.get('channel') or ''}",
                    f"  版本号：{item.get('version_code') or ''}",
                    f"  版本名：{item.get('version_name') or ''}",
                ]
            )

        next_offset = safe_offset + len(page)
        lines.append("")
        lines.append("分页：")
        if total == 1:
            lines.append("如果要直接打包，可以说“打包这个应用”或“打包这个包”。")
        elif next_offset < total:
            lines.append(f"已显示 {safe_offset + 1}-{next_offset} / 共 {total} 个，发送“还有呢”或“下一页”继续。")
        else:
            lines.append(f"已全部显示，共 {total} 个。")

        data = {
            "intent": "package_lookup",
            "query": query,
            "matches": matches,
            "page_size": safe_page_size,
            "next_offset": next_offset,
        }
        session_patch = {
            "last_package_lookup": {
                "query": query,
                "matches": matches,
                "page_size": safe_page_size,
                "next_offset": next_offset,
                "heading": heading or f"查到 {query} 对应的包：",
            }
        }
        self.state_store.update_session(session_id, session_patch)
        return AgentResponse("\n".join(lines), data)

    def _search_local_files(self, patterns: list[str], *, max_results: int = 20) -> AgentResponse:
        clean_patterns = [pattern for pattern in patterns if pattern]
        if not clean_patterns:
            return AgentResponse(
                _format_error_message("全文搜索缺少关键词", "没有识别到要搜索的旧路径或关键词。"),
                {"intent": "file_search", "missing": "patterns"},
            )
        roots = self._file_search_roots()
        matches: list[JsonDict] = []
        searched_files = 0
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if len(matches) >= max_results:
                    break
                if not path.is_file() or not _is_searchable_file(path):
                    continue
                searched_files += 1
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                lines = text.splitlines()
                for line_no, line in enumerate(lines, start=1):
                    if any(pattern in line for pattern in clean_patterns):
                        matches.append(
                            {
                                "path": str(path),
                                "line": line_no,
                                "text": _shorten(line.strip(), 180),
                            }
                        )
                        break
                if len(matches) >= max_results:
                    break
        lines = ["全文搜索结果", "", "搜索条件："]
        lines.extend(f"- {pattern}" for pattern in clean_patterns)
        lines.append("")
        lines.append("搜索范围：")
        lines.extend(f"- {root}" for root in roots)
        lines.append("")
        lines.append("匹配结果：")
        if matches:
            for index, item in enumerate(matches, start=1):
                lines.append(f"{index}. {item['path']}:{item['line']}")
                lines.append(f"   - {item['text']}")
        else:
            lines.append("- 未找到匹配。")
        lines.append("")
        lines.append(f"说明：已扫描约 {searched_files} 个文本文件，最多返回 {max_results} 条。")
        return AgentResponse(
            "\n".join(lines),
            {"intent": "file_search", "patterns": clean_patterns, "roots": [str(root) for root in roots], "matches": matches},
        )

    def _file_search_roots(self) -> list[Path]:
        roots: list[Path] = []
        for path in (self.oppo_config_path, self.packaging_config_path):
            if path:
                roots.append(path.parent)
        project_dir = self.packaging_agent.settings.project_dir
        if project_dir:
            roots.append(project_dir)
        deduped: list[Path] = []
        for root in roots:
            resolved = Path(root).resolve()
            if resolved not in deduped:
                deduped.append(resolved)
        return deduped

    def _packaging_packlist_snapshot(self) -> Path | None:
        configured = self.packaging_agent.settings.packlist_scan_file
        if configured:
            return configured
        if self.oppo_config_path:
            fallback = self.oppo_config_path.parent.parent / "packlist-scan.json"
            if fallback.exists():
                return fallback
        return None

    def _filter_market_result(
        self,
        result: AppMarketSearchResult,
        *,
        query: str,
        source_text: str = "",
        exact_match: bool = False,
        exclude_terms: list[str] | None = None,
        include_terms: list[str] | None = None,
    ) -> tuple[AppMarketSearchResult, JsonDict]:
        resolved_exact = exact_match or _looks_like_exact_app_request(source_text)
        excludes = _unique_texts((exclude_terms or []) + _infer_market_exclude_terms(source_text))
        includes = _unique_texts(include_terms or [])
        query_name = _normalize_market_app_name(query)
        filtered_apps: list[AppMarketListing] = []
        dropped: list[str] = []
        for app in result.apps:
            normalized_name = _normalize_market_app_name(app.name)
            if resolved_exact and query_name and normalized_name != query_name:
                dropped.append(app.name)
                continue
            if includes and not any(term in normalized_name for term in includes):
                dropped.append(app.name)
                continue
            if excludes and any(term in normalized_name for term in excludes):
                dropped.append(app.name)
                continue
            filtered_apps.append(app)
        filtered = AppMarketSearchResult(
            query=result.query,
            apps=filtered_apps,
            errors=result.errors,
            store_statuses=result.store_statuses,
        )
        filter_info = {
            "exact_match": resolved_exact,
            "exclude_terms": excludes,
            "include_terms": includes,
            "dropped_names": _unique_texts(dropped),
        }
        return filtered, filter_info

    @staticmethod
    def _build_remediation_items(analysis: JsonDict) -> list[str]:
        if not analysis:
            return []
        categories = set(analysis.get("categories") or [])
        items: list[str] = []
        if "apk_similarity_or_template" in categories:
            items.append("充分修改 APK，降低模板/马甲包相似度，或准备独立应用申诉材料。")
        if "do_not_repeat_submit_without_changes" in categories:
            items.append("不要原包直接重提；确认代码、资源、交互或材料已有实质改动。")
        if "missing_icp_proof" in categories:
            items.append("补充公司自有、与应用一致的 ICP 备案网站证明，网页需可访问并展示备案号。")
        if not items:
            items.extend(str(item) for item in analysis.get("required_actions") or [])
        return items

    @staticmethod
    def _format_remediation_checklist(items: list[str], analysis: JsonDict) -> str:
        lines = ["整改清单", "", "待办项："]
        lines.extend(f"{index}. {item}" for index, item in enumerate(items, start=1))
        targets = [target.replace("OPPO backend: ", "") for target in analysis.get("evidence_targets") or []]
        if targets:
            lines.append("")
            lines.append("上传位置：" + " / ".join(targets))
        return "\n".join(lines)

    @staticmethod
    def _normalize_market_result(result: Any) -> AppMarketSearchResult:
        if isinstance(result, AppMarketSearchResult):
            return result
        if isinstance(result, dict):
            apps = []
            for app in result.get("apps") or []:
                if hasattr(app, "to_dict"):
                    apps.append(app)
                else:
                    from autoreview.market.research import AppMarketListing

                    apps.append(
                        AppMarketListing(
                            store=str(app.get("store") or ""),
                            app_id=str(app.get("app_id") or ""),
                            name=str(app.get("name") or ""),
                            developer=str(app.get("developer") or ""),
                            package_name=str(app.get("package_name") or ""),
                            category=str(app.get("category") or ""),
                            url=str(app.get("url") or ""),
                            rating=app.get("rating"),
                            rating_count=app.get("rating_count"),
                            downloads=app.get("downloads"),
                            downloads_text=str(app.get("downloads_text") or ""),
                            rank=app.get("rank"),
                            raw_metrics=dict(app.get("raw_metrics") or {}),
                        )
                    )
            return AppMarketSearchResult(
                query=str(result.get("query") or ""),
                apps=apps,
                errors=[str(item) for item in result.get("errors") or []],
                store_statuses=[dict(item) for item in result.get("store_statuses") or [] if isinstance(item, dict)],
            )
        return AppMarketSearchResult(query="", apps=[], errors=["unsupported market search result"])

    @staticmethod
    def _format_market_search(
        result: AppMarketSearchResult,
        *,
        filtered_names: JsonDict | None = None,
        source_text: str = "",
    ) -> str:
        title = "应用商店竞品搜索" if _looks_like_competitor_market_request(source_text) else "应用商店查询"
        lines = [f"{title}", f"- 关键词：{result.query}"]
        filtered_names = filtered_names or {}
        if not result.apps:
            lines.append("")
            lines.append("查询结果：")
            if filtered_names.get("exact_match"):
                lines.append(f"- 未找到与“{result.query}”精确匹配的结果。")
            else:
                lines.append("- 未找到结果。可以换更具体的关键词或应用名。")
            dropped = filtered_names.get("dropped_names") or []
            if dropped:
                lines.append("- 已排除：" + "、".join(str(item) for item in dropped[:6]))
        else:
            lines.append("")
            lines.append("查询结果：")
            for index, app in enumerate(result.apps[:8], start=1):
                lines.extend(_format_market_listing(index, app.to_dict()))
        if result.store_statuses:
            lines.append("")
            lines.append("查询状态：")
            lines.extend(_format_store_status(item) for item in result.store_statuses)
        if result.errors:
            lines.append("")
            lines.append("部分商店查询失败：" + "；".join(result.errors[:3]))
        lines.append("")
        lines.append("提示：发送“记录竞品下载：同一关键词”可把本月公开指标写入状态。")
        return "\n".join(lines)

    @staticmethod
    def _format_download_snapshot(snapshot: JsonDict, *, filtered_names: JsonDict | None = None) -> str:
        lines = ["竞品下载数据已记录", f"- 月份：{snapshot['month']}", f"- 关键词：{snapshot.get('query') or ''}"]
        filtered_names = filtered_names or {}
        apps = snapshot.get("apps") or []
        if not apps:
            lines.append("")
            lines.append("记录结果：")
            if filtered_names.get("exact_match"):
                lines.append(f"- 未记录到与“{snapshot.get('query') or ''}”精确匹配的应用结果。")
            else:
                lines.append("- 未记录到应用结果。")
            dropped = filtered_names.get("dropped_names") or []
            if dropped:
                lines.append("- 已排除：" + "、".join(str(item) for item in dropped[:6]))
        else:
            lines.append("")
            lines.append("记录结果：")
            for index, app in enumerate(apps[:8], start=1):
                lines.extend(_format_market_listing(index, app))
        if snapshot.get("store_statuses"):
            lines.append("")
            lines.append("查询状态：")
            lines.extend(_format_store_status(item) for item in snapshot["store_statuses"])
        if snapshot.get("errors"):
            lines.append("")
            lines.append("部分商店查询失败：" + "；".join(str(item) for item in snapshot["errors"][:3]))
        lines.append("")
        lines.append("说明：下载量只记录商店公开披露的数据；不公开精确下载量的商店会留空。")
        return "\n".join(lines)

    @staticmethod
    def _get_last_image_text(session: JsonDict) -> str:
        image_analysis = session.get("last_image_analysis") or {}
        if image_analysis.get("ocr_text"):
            return str(image_analysis["ocr_text"])
        analysis = image_analysis.get("analysis") or {}
        ocr = analysis.get("ocr") or {}
        data = ocr.get("data") or {}
        for key in ("full_text", "text", "ocr_text", "image_txt"):
            if data.get(key):
                return str(data[key])
        rows = data.get("rows")
        if isinstance(rows, list):
            pieces = []
            for row in rows:
                if isinstance(row, dict):
                    text = row.get("Content") or row.get("OcrText") or row.get("text")
                    if text:
                        pieces.append(str(text))
            return "\n".join(pieces).strip()
        return ""


def _shorten(value: Any, limit: int = 120) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _format_oppo_task_state(task: JsonDict) -> str:
    named = task.get("task_state_name") or task.get("state_name")
    if named:
        return str(named)
    return {
        "1": "处理中",
        "2": "任务完成",
        "3": "任务失败",
    }.get(str(task.get("task_state") or ""), str(task.get("task_state") or ""))


def _material_name(material_type: Any, index: Any = None) -> str:
    labels = {
        "apk": "APK",
        "icon": "图标",
        "screenshot": f"截图{int(index or 0) + 1}",
        "copyright": "版权证明",
        "icp": "ICP/特殊类证明",
    }
    return labels.get(str(material_type), str(material_type or "未知"))


SUPPORTED_MARKET_STORES = (
    ("qimai_data", "七麦数据"),
    ("appark_data", "Appark"),
    ("apple_app_store", "Apple App Store"),
    ("google_play", "Google Play"),
    ("oppo_app_market", "OPPO 软件商店"),
    ("xiaomi_app_store", "小米应用商店"),
    ("vivo_app_store", "vivo 应用商店"),
    ("huawei_appgallery", "华为 AppGallery"),
    ("honor_app_market", "荣耀应用市场"),
)


def _store_label(store: Any) -> str:
    labels = dict(SUPPORTED_MARKET_STORES)
    return labels.get(str(store or ""), str(store or "未知商店"))


def _format_supported_market_stores(disabled_stores: set[str] | None = None) -> str:
    disabled = disabled_stores or set()
    active = [(store, label) for store, label in SUPPORTED_MARKET_STORES if store not in disabled]
    inactive = [(store, label) for store, label in SUPPORTED_MARKET_STORES if store in disabled]
    lines = ["目前应用商店查询会尝试查询这些来源："]
    lines.extend(f"- {label}" for _, label in active)
    if inactive:
        lines.append("当前会话已按你的偏好排除：")
        lines.extend(f"- {label}" for _, label in inactive)
    lines.append("说明：这些都是公开页面/公开接口查询，不同商店可见数据不同；OPPO、vivo、华为等入口可能因公开页面限制而跳过或拿不到结果。")
    return "\n".join(lines)


def _extract_market_store_name(text: str) -> str:
    lowered = str(text or "").lower()
    aliases = {
        "qimai_data": ("qimai", "七麦", "七麦数据"),
        "appark_data": ("appark", "appmark", "appark.ai"),
        "apple_app_store": ("apple app store", "app store", "苹果", "ios"),
        "google_play": ("google play", "google", "play商店", "谷歌"),
        "oppo_app_market": ("oppo", "欢太", "heytap"),
        "xiaomi_app_store": ("xiaomi", "小米"),
        "vivo_app_store": ("vivo",),
        "huawei_appgallery": ("huawei", "appgallery", "华为"),
        "honor_app_market": ("honor", "荣耀"),
    }
    for store, names in aliases.items():
        if any(name in lowered for name in names):
            return store
    return ""


def _normalize_store_name(value: Any) -> str:
    raw = str(value or "").strip()
    known = {store for store, _ in SUPPORTED_MARKET_STORES}
    if raw in known:
        return raw
    return _extract_market_store_name(raw)


def _normalize_store_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    raw_items = value if isinstance(value, list) else [value]
    stores = []
    for item in raw_items:
        store = _normalize_store_name(item)
        if store and store not in stores:
            stores.append(store)
    return stores


def _extract_target_market_stores(text: Any) -> list[str]:
    lowered = str(text or "").lower()
    scoped_aliases = {
        "apple_app_store": ("apple app store", "app store", "苹果"),
        "google_play": ("google play", "谷歌"),
        "oppo_app_market": ("oppo", "欢太", "heytap"),
        "xiaomi_app_store": ("xiaomi", "小米"),
        "vivo_app_store": ("vivo",),
        "huawei_appgallery": ("huawei", "appgallery", "华为"),
        "honor_app_market": ("honor", "荣耀"),
    }
    context_terms = ("应用商店", "应用市场", "软件商店", "商店", "市场", "店里", "里面", "平台")
    for store, aliases in scoped_aliases.items():
        for alias in aliases:
            if alias in lowered and any(term in lowered for term in context_terms):
                return [store]
    return []


def _normalize_string_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    raw_items = value if isinstance(value, list) else [value]
    items: list[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def _unique_texts(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _display_patch_value(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= 100 else text[:99] + "..."


def _normalize_market_app_name(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\b(app|APP)\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s：:，,。！？?、；;“”\"'`~!@#$%^&*()\[\]{}<>|\\/]+", "", text)
    return text.strip()


def _looks_like_exact_app_request(text: Any) -> bool:
    clean = str(text or "").lower()
    return any(
        marker in clean
        for marker in (
            "只要",
            "只是",
            "就只是",
            "不要其他",
            "其他的不要",
            "其他app不用看",
            "其他 app 不用看",
            "指名",
            "指定",
            "精确",
            "准确",
            "这个app",
            "这个 app",
        )
    )


def _looks_like_market_followup(text: Any) -> bool:
    clean = str(text or "").lower()
    market_terms = ("应用商店", "应用市场", "软件商店", "商店", "市场", "下载量", "下载数据", "指标")
    followup_terms = ("其他", "其它", "别的", "换", "再搜", "再查", "继续", "之前", "刚才", "上面", "前面")
    return any(term in clean for term in market_terms) and any(term in clean for term in followup_terms)


def _looks_like_other_market_stores_request(text: Any) -> bool:
    clean = str(text or "").lower()
    return any(term in clean for term in ("其他", "其它", "别的", "其余", "剩下"))


def _looks_like_market_download_request(text: Any) -> bool:
    clean = str(text or "").lower()
    return any(term in clean for term in ("记录", "保存", "月报", "月度"))


def _looks_like_competitor_market_request(text: Any) -> bool:
    clean = str(text or "").lower()
    return any(term in clean for term in ("竞品", "对标", "同类", "同类型", "类似", "相似", "赛道", "有哪些产品"))


def _looks_like_recent_context_question(text: Any) -> bool:
    clean = str(text or "").lower()
    context_terms = ("之前", "刚才", "上次", "前面", "上一轮")
    ask_terms = ("发给你", "给你的", "说了什么", "说的是", "信息是什么", "内容是什么")
    return any(term in clean for term in context_terms) and any(term in clean for term in ask_terms)


def _infer_market_exclude_terms(text: Any) -> list[str]:
    clean = str(text or "")
    terms: list[str] = []
    known_variants = ("极速版", "火山版", "商城版", "HD", "hd", "国际版", "青春版")
    for term in known_variants:
        if term in clean and any(marker in clean for marker in ("不要", "不包含", "不是", "别发", "别查", "排除")):
            terms.append(term)
    return terms


def _clean_market_query(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"[\s：:，,。！？?、；;“”\"'`~!@#$%^&*()\[\]{}<>|\\/]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not re.search(r"[\w\u4e00-\u9fff]", text):
        return ""
    return text


def _is_contextual_app_reference(value: Any) -> bool:
    text = _clean_market_query(value)
    return text in {"这个", "这个应用", "当前", "当前应用", "默认", "默认应用", "它", "该应用"}


APP_STORE_DATA_PLATFORM_NAMES = (
    "七麦",
    "点点数据",
    "点点",
    "蝉大师",
    "易观千帆",
    "questmobile",
    "sensor tower",
    "data.ai",
    "apptweak",
    "mobileaction",
    "appfigures",
    "apptopia",
    "similarweb",
)


APP_STORE_DATA_PLATFORM_HINTS = (
    "数据平台",
    "第三方数据",
    "统计数据",
    "商店统计",
    "应用统计",
    "应用数据",
    "榜单数据",
    "aso平台",
    "aso 平台",
    "aso工具",
    "aso 工具",
    "下载收入",
    "收入预估",
    "市场情报",
    "移动应用情报",
)


def _looks_like_app_store_data_platform_research(text: Any, query: Any = "") -> bool:
    combined = f"{text or ''} {query or ''}".lower()
    if any(name in combined for name in APP_STORE_DATA_PLATFORM_NAMES):
        return any(term in combined for term in ("平台", "数据", "统计", "aso", "类似", "竞品", "这种"))
    return any(hint in combined for hint in APP_STORE_DATA_PLATFORM_HINTS)


def _format_app_store_data_platform_research() -> str:
    return "\n".join(
        [
            "这个需求更像“应用商店数据/ASO 平台调研”，不是去应用商店里搜 APP 竞品。",
            "常见同类平台可以先看：",
            "- 七麦数据：国内 ASO、榜单、关键词、竞品和应用商店数据。",
            "- 点点数据：App/Game 数据、下载收入预估、榜单、SDK 和竞品分析。",
            "- 蝉大师：国内 ASO 和应用增长相关数据平台。",
            "- QuestMobile / 易观千帆：更偏移动互联网用户规模、画像和行业趋势。",
            "- Sensor Tower / AppTweak / MobileAction / Appfigures / Similarweb：海外应用市场情报、ASO、下载收入和广告分析。",
            "说明：当前“竞品搜索”工具只查应用商店公开页面；这类平台调研应走网页资料搜索或大模型普通答复。",
        ]
    )


def _format_market_metrics(app: JsonDict) -> str:
    downloads_text = str(app.get("downloads_text") or "").strip()
    downloads = app.get("downloads")
    if downloads_text:
        download_part = f"下载量 {downloads_text}"
    elif downloads is not None:
        download_part = f"下载量 {downloads}"
    else:
        download_part = "下载量未公开"
    rating = app.get("rating")
    rating_count = app.get("rating_count")
    if rating is None:
        return download_part
    count_part = f"，{rating_count} 条评分" if rating_count is not None else ""
    return f"{download_part}，评分 {rating}{count_part}"


def _format_market_listing(index: int, app: JsonDict) -> list[str]:
    store = _store_label(app.get("store"))
    name = str(app.get("name") or app.get("app_id") or "未命名应用")
    developer = str(app.get("developer") or "").strip()
    category = str(app.get("category") or "").strip()
    lines = [f"{index}. {name}", f"   - 商店：{store}", f"   - 指标：{_format_market_metrics(app)}"]
    if developer:
        lines.append(f"   - 开发者：{developer}")
    if category:
        lines.append(f"   - 分类：{category}")
    return lines


def _looks_like_structured_reply(text: Any) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    first_line = value.splitlines()[0].strip()
    known_titles = {
        "回复",
        "已记录",
        "还没理解清楚",
        "处理结果",
        "最近对话",
        "当前状态",
        "当前默认应用",
        "应用商店查询",
        "应用商店竞品搜索",
        "竞品下载数据已记录",
        "打包结果",
        "批量打包结果",
        "打包应用查询",
        "提交检查",
        "整改清单",
        "驳回原因分析结果",
        "OPPO 审核状态",
        "提交配置",
        "配置修改已暂存",
        "材料已绑定",
    }
    if first_line in known_titles:
        return True
    if first_line.startswith(("处理结果：", "说明：", "结果：")):
        return True
    section_markers = (
        "\n\n说明：",
        "\n\n结果：",
        "\n\n记录内容：",
        "\n\n下一步：",
        "\n\n查询结果：",
        "\n\n查询状态：",
        "\n\n应用信息：",
        "\n\n检查结果：",
        "\n\n处理建议：",
        "\n\n包列表：",
        "\n\n失败原因：",
    )
    return any(marker in value for marker in section_markers)


def _format_llm_free_reply(intent: str, reply: Any) -> str:
    text = str(reply or "").strip()
    if not text:
        return ""
    if _looks_like_mojibake(text):
        text = "大模型返回内容编码异常，已忽略这次自由回复。可以直接发送“帮助”或使用具体业务指令。"
    if _looks_like_structured_reply(text):
        return text
    if intent == "remember":
        return "\n".join(["已记录", "", "记录内容：", f"- {_shorten(text, 800)}"])
    if intent in {"unknown", "disabled"}:
        return "\n".join(
            [
                "还没理解清楚",
                "",
                "说明：",
                f"- {_shorten(text, 800)}",
                "",
                "下一步：",
                "- 可以换个说法，或发送“帮助”查看可用场景。",
            ]
        )
    return "\n".join(["回复", "", "说明：", f"- {_shorten(text, 800)}"])


def _format_tool_summary_reply(summary: Any) -> str:
    text = str(summary or "").strip()
    if not text:
        return ""
    if _looks_like_mojibake(text):
        return ""
    if _looks_like_structured_reply(text):
        return text
    if "\n" in text:
        return "\n".join(["处理结果", "", text])
    return "\n".join(["处理结果", "", "说明：", f"- {_shorten(text, 1000)}"])


def _looks_like_mojibake(text: Any) -> bool:
    value = str(text or "")
    if not value:
        return False
    replacement_count = value.count("\ufffd") + value.count("�")
    if replacement_count >= 2:
        return True
    suspicious = ("����", "���", "锟斤拷", "鈥", "û����", "������")
    if any(item in value for item in suspicious):
        return True
    asciiish = sum(1 for ch in value if ord(ch) < 128)
    chinese = sum(1 for ch in value if "\u4e00" <= ch <= "\u9fff")
    high = sum(1 for ch in value if ord(ch) >= 128)
    return high >= 12 and chinese == 0 and asciiish < len(value) * 0.5


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "确认", "是", "要"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否", "不要"}:
        return False
    return None


def _looks_like_last_packaging_page_request(text: Any) -> bool:
    clean = str(text or "")
    return "最后" in clean or "末尾" in clean


def _is_all_packaging_query(value: Any) -> bool:
    clean = re.sub(r"[：:，,。！？?\s]+", "", str(value or ""))
    return clean in {"全部", "所有", "全量", "列表", "全部包", "所有包", "可打包列表", "packlist列表"}


def _is_searchable_file(path: Path) -> bool:
    if any(part in {"node_modules", ".git", ".gradle", "build", ".venv", "__pycache__"} for part in path.parts):
        return False
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".apk", ".zip", ".jar", ".so", ".dex", ".class"}:
        return False
    return path.suffix.lower() in {
        ".json",
        ".txt",
        ".md",
        ".py",
        ".js",
        ".ts",
        ".java",
        ".kt",
        ".gradle",
        ".properties",
        ".xml",
        ".yml",
        ".yaml",
        ".bat",
        ".ps1",
        ".sh",
        ".cfg",
        ".ini",
    }


def _format_error_message(title: str, reason: Any, next_steps: list[str] | None = None) -> str:
    lines = [title, "", "原因：", f"- {_shorten(reason, 500) or '未知原因'}"]
    steps = next_steps or []
    if steps:
        lines.append("")
        lines.append("下一步：")
        lines.extend(f"- {step}" for step in steps)
    return "\n".join(lines)


def _format_store_status(status: JsonDict) -> str:
    store = _store_label(status.get("store"))
    result_count = int(status.get("result_count") or 0)
    state = str(status.get("status") or "")
    message = str(status.get("message") or "").strip()
    if state == "ok":
        return f"- {store}：{result_count} 个结果"
    if state == "skipped":
        return f"- {store}：{message or '已跳过'}"
    if state == "failed":
        return f"- {store}：{message or '查询失败'}"
    return f"- {store}：{message or '未解析到匹配结果'}"


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def _redact_for_trace(value: Any, *, max_string: int = 4000) -> Any:
    sensitive_markers = (
        "api_key",
        "client_secret",
        "app_secret",
        "token",
        "authorization",
        "x-api-key",
        "ocr_api_key",
        "secret",
        "password",
    )
    if isinstance(value, dict):
        redacted: JsonDict = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in sensitive_markers):
                redacted[str(key)] = "***REDACTED***"
            else:
                redacted[str(key)] = _redact_for_trace(item, max_string=max_string)
        return redacted
    if isinstance(value, list):
        return [_redact_for_trace(item, max_string=max_string) for item in value]
    if isinstance(value, tuple):
        return [_redact_for_trace(item, max_string=max_string) for item in value]
    if isinstance(value, str):
        return _shorten(value, max_string)
    return _json_safe(value)


def _optional_confidence(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
