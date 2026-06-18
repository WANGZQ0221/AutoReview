"""Review collaboration agent.

This layer turns chat messages into review workflow actions. Store-specific
automation remains in the OPPO package.
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
import json
import re
import time
import uuid
from typing import Any, Callable

from autoreview.market import AppMarketSearchResult, AppMarketSearcher, build_monthly_snapshot
from autoreview.packaging.agent import (
    PackagingAgent,
    format_batch_package_result,
    format_package_result,
    parse_package_request,
)
from autoreview.packaging.packlist import (
    resolve_packlist_app_name,
    resolve_packlist_app_name_entries,
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

6. 驳回分析与整改
- “分析驳回：<原因>”
- 发送驳回截图后说“分析这张图”
- “整改清单”

7. 配置与材料
- “查看提交配置”
- “设置提交配置：字段=值”
- “确认保存配置”
- 发送文件后说“绑定材料：APK/图标/截图1/版权证明/ICP证明”

8. 图片 OCR / image2
- 直接发送图片
- “分析最近图片”"""


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
        market_followup = self._handle_market_followup(session_id, clean_text, sender_id=sender_id)
        if market_followup:
            return market_followup

        recent_context_response = self._answer_recent_context_question(session_id, clean_text)
        if recent_context_response:
            return recent_context_response

        packaging_followup = self._handle_packaging_followup(session_id, clean_text)
        if packaging_followup:
            return packaging_followup

        packaging_catalog = self._handle_packaging_catalog_request(session_id, clean_text)
        if packaging_catalog:
            return packaging_catalog

        tool_response = self._response_from_llm_tool_call(session_id, clean_text, sender_id=sender_id)
        if tool_response:
            return tool_response

        llm_decision = self._interpret_with_llm(session_id, clean_text, sender_id=sender_id)
        if self._should_apply_llm_before_rules(clean_text):
            llm_response = self._response_from_llm_decision(
                session_id,
                clean_text,
                llm_decision,
                sender_id=sender_id,
                allow_chat=True,
            )
            if llm_response:
                return llm_response

        if clean_text in {"帮助", "help", "/help"}:
            return AgentResponse(HELP_TEXT, {"intent": "help"})

        preference_response = self._handle_market_store_preference(session_id, clean_text, sender_id=sender_id)
        if preference_response:
            return preference_response

        capability_response = self._answer_capability_question(session_id, clean_text)
        if capability_response:
            return capability_response

        research_response = self._handle_app_store_data_platform_research(clean_text)
        if research_response:
            return research_response

        if clean_text in {"清空记录", "清空当前记录", "清空当前状态", "重置记录", "重置当前记录", "重置当前会话"}:
            return self.clear_session_state(session_id)

        if clean_text in {"清空所有记录", "清空全部记录", "重置所有记录", "重置全部会话"}:
            return self.clear_all_state()

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

        if clean_text in {"确认保存配置", "保存配置", "确认配置"}:
            return self.confirm_config_update(session_id, sender_id=sender_id)

        if clean_text in {"取消保存配置", "取消配置修改", "放弃配置修改"}:
            return self.cancel_config_update(session_id, sender_id=sender_id)

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
                    {"intent": "market_search", "missing": "query"},
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
                    {"intent": "market_download_snapshot", "missing": "query"},
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

        if clean_text.startswith("查询审核状态") or clean_text in {"审核状态", "OPPO状态", "oppo状态"}:
            return self.query_oppo_status(clean_text, session_id=session_id, sender_id=sender_id)

        if clean_text in {"提交检查", "校验配置", "检查提交"}:
            return self.check_submission(session_id)

        if clean_text in {"准备提交"}:
            session = self.state_store.get_session(session_id)
            checklist = self._build_submit_checklist(session)
            return AgentResponse(checklist, {"intent": "submit_checklist", "session": session})

        semantic_response = self._handle_semantic_intent(session_id, clean_text, sender_id=sender_id)
        if semantic_response:
            return semantic_response

        fallback_package_response = self._handle_packaging_fallback(session_id, clean_text)
        if fallback_package_response:
            return fallback_package_response

        llm_response = self._response_from_llm_decision(
            session_id,
            clean_text,
            llm_decision,
            sender_id=sender_id,
            allow_chat=True,
        )
        if llm_response:
            return llm_response

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
                {"intent": "market_search", "missing": "query"},
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
                {"intent": "market_download_snapshot", "missing": "query"},
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

    def check_submission(self, session_id: str) -> AgentResponse:
        try:
            validation = self._make_oppo_agent().validate()
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

    def _handle_semantic_intent(
        self,
        session_id: str,
        text: str,
        sender_id: str | None = None,
    ) -> AgentResponse | None:
        lowered = text.lower()

        if self._looks_like_help_request(lowered):
            return AgentResponse(HELP_TEXT, {"intent": "help", "semantic": True})

        if self._looks_like_clear_all_request(lowered):
            return self.clear_all_state()

        if self._looks_like_clear_session_request(lowered):
            return self.clear_session_state(session_id)

        if self._looks_like_oppo_status_request(lowered):
            return self.query_oppo_status(text, session_id=session_id, sender_id=sender_id)

        if self._looks_like_submission_check_request(lowered):
            return self.check_submission(session_id)

        if self._looks_like_submit_prepare_request(lowered):
            session = self.state_store.get_session(session_id)
            checklist = self._build_submit_checklist(session)
            return AgentResponse(checklist, {"intent": "submit_checklist", "session": session, "semantic": True})

        semantic_market_intent = self._parse_market_semantic_intent(text, session_id)
        if semantic_market_intent:
            intent, query = semantic_market_intent
            if not query:
                return AgentResponse(
                    "我理解你想做竞品分析，但还缺关键词。可以先发送“记录应用：应用名 / 包名 / 版本号”，或直接说“帮我找英语四级单词的竞品”。",
                    {"intent": intent, "missing": "query", "semantic": True},
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
        return {
            "session": session,
            "recent_conversation": session.get("conversation_history") or [],
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
            version_code = str(decision.get("version_code") or "").strip()
            return self.query_oppo_status(
                f"查询审核状态：{version_code}" if version_code else text,
                session_id=session_id,
                sender_id=sender_id,
            )
        if intent == "submission_check":
            return self.check_submission(session_id)
        if intent == "submit_checklist":
            session = self.state_store.get_session(session_id)
            return AgentResponse(self._build_submit_checklist(session), {"intent": "submit_checklist", "session": session})
        if intent in {"package_apk", "batch_package"}:
            package_response = self._run_packaging_intent(session_id, text, decision)
            if package_response:
                return package_response
        if intent == "package_lookup":
            return self._run_packaging_lookup(session_id, text, decision)
        if intent == "view_submission_config":
            return self.view_submission_config()
        if intent == "stage_config_update":
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
        if intent == "bind_material":
            label = str(decision.get("material_label") or self._extract_material_label(text)).strip()
            return self.bind_last_upload_as_material(session_id, label, sender_id=sender_id)
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
            query = str(decision.get("query") or self._extract_market_semantic_query(text, session_id)).strip()
            if not query:
                return AgentResponse("要查哪个应用方向的竞品？例如“英语四级单词”。", {"intent": intent, "missing": "query"})
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
            "submission_check",
            "执行提交前检查，检查配置、缺文件和最近驳回重提风险。",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self._tool_submission_check,
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
            "按配置文件批量打包 APK。支持 dry_run。",
            {
                "type": "object",
                "properties": {"dry_run": {"type": "boolean"}},
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
                    "query": {"type": "string"},
                    "exact_match": {"type": "boolean"},
                    "exclude_terms": {"type": "array", "items": {"type": "string"}},
                    "include_terms": {"type": "array", "items": {"type": "string"}},
                    "target_stores": {"type": "array", "items": {"type": "string"}},
                },
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
                    "query": {"type": "string"},
                    "exact_match": {"type": "boolean"},
                    "exclude_terms": {"type": "array", "items": {"type": "string"}},
                    "include_terms": {"type": "array", "items": {"type": "string"}},
                    "target_stores": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": True,
            },
            self._tool_market_download_snapshot,
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
        return AgentResponse(_format_tool_summary_reply(summary) or response.text, data)

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

    def _tool_submission_check(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        return self.check_submission(str(context.get("session_id") or ""))

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

    def _tool_bind_material(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        label = str(call.arguments.get("material_label") or "").strip()
        if not label:
            label = self._extract_material_label(str(context.get("text") or "")).strip()
        return self.bind_last_upload_as_material(
            str(context.get("session_id") or ""),
            label,
            sender_id=context.get("sender_id"),
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
            "app_name": str(call.arguments.get("app_name") or ""),
            "query": str(call.arguments.get("query") or ""),
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
        return self._run_packaging_intent(
            str(context.get("session_id") or ""),
            str(context.get("text") or ""),
            {"intent": "batch_package", "dry_run": bool(call.arguments.get("dry_run"))},
        )

    def _tool_market_search(self, call: ToolCall, context: JsonDict) -> AgentResponse:
        query = str(call.arguments.get("query") or "").strip()
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
        query = str(call.arguments.get("query") or "").strip()
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
            f"- Node：{packaging.get('node_command') or 'node'}",
        ]
        return "\n".join(lines)

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
        return any(
            term in text
            for term in (
                "还有呢",
                "下一页",
                "继续",
                "后面的",
                "后面还有",
                "更高年级",
                "没有回复完全",
                "没回复完全",
                "没说完",
                "接着发",
            )
        )

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

    def _looks_like_submission_check_request(self, text: str) -> bool:
        return self._contains_any(
            text,
            ("提交检查", "检查提交", "提交前检查", "校验配置", "能不能提交", "是否可以提交", "能否提交", "发布检查"),
        )

    def _looks_like_submit_prepare_request(self, text: str) -> bool:
        return self._contains_any(text, ("准备提交", "提交前要做", "提交前需要", "发版前", "上线前"))

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
            return "提交前检查：当前会话没有发现明显缺口。下一步可接入 OPPO submit 工具。"
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
        if task.get("error"):
            lines.append(f"- 提交任务：查询失败（{_shorten(task['error'])}）")
        elif task:
            task_text = task.get("task_state_name") or task.get("state_name") or task.get("task_state")
            if task_text:
                lines.append(f"- 提交任务：{task_text}")
        if status.get("review_state") == "rejected":
            reason = extract_rejection_reason(app_info)
            if reason:
                lines.append(f"- 驳回原因：{_shorten(reason)}")
        return "\n".join(lines)

    @staticmethod
    def _format_submission_check(validation: JsonDict, session: JsonDict) -> str:
        lines = ["提交检查", "", "检查结果：", f"- 配置文件：{'通过' if validation.get('valid') else '不通过'}"]
        missing_fields = validation.get("missing_required_fields") or []
        missing_files = validation.get("missing_files") or []
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
        if validation.get("valid") and not (analysis and not analysis.get("can_resubmit_same_apk")):
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
        return self._render_packaging_lookup_page(
            session_id,
            query=str(lookup.get("query") or ""),
            matches=matches,
            offset=int(lookup.get("next_offset") or 0),
            page_size=int(lookup.get("page_size") or 10),
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
        try:
            if decision.get("intent") == "batch_package" or parsed.get("batch"):
                result = self.packaging_agent.package_batch(dry_run=dry_run, continue_on_error=True)
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
        query = str(decision.get("app_name") or decision.get("query") or self._extract_packaging_lookup_query(text)).strip()
        if not query:
            return AgentResponse(
                _format_error_message(
                    "查包缺少应用名",
                    "没有识别到要查询的应用名。",
                    ["请告诉我应用名，比如“八年级语文下册”或“帮我查四年级英语上册对应什么包”。"],
                ),
                {"intent": "package_lookup", "missing": "query"},
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
        return self._render_packaging_lookup_page(
            session_id,
            query=query,
            matches=[entry.to_dict() for entry in matches],
            offset=0,
            page_size=10,
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
            return resolve_packlist_app_name(self._packaging_project_dir(), query)
        except Exception as primary_exc:
            snapshot = self._packaging_packlist_snapshot()
            if not snapshot:
                raise primary_exc
            entries = scan_packlist_snapshot(snapshot)
            return resolve_packlist_app_name_entries(entries, query)

    def _all_packaging_entries(self):
        try:
            return scan_packlist(self._packaging_project_dir())
        except Exception as primary_exc:
            snapshot = self._packaging_packlist_snapshot()
            if not snapshot:
                raise primary_exc
            return scan_packlist_snapshot(snapshot)

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
    if _looks_like_structured_reply(text):
        return text
    if "\n" in text:
        return "\n".join(["处理结果", "", text])
    return "\n".join(["处理结果", "", "说明：", f"- {_shorten(text, 1000)}"])


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
