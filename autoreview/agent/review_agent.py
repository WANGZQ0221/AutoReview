"""Review collaboration agent.

This layer turns chat messages into review workflow actions. Store-specific
automation remains in the OPPO package.
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
import json
import re
from typing import Any, Callable

from autoreview.market import AppMarketSearchResult, AppMarketSearcher, build_monthly_snapshot
from autoreview.oppo.agent import OppoSubmissionAgent, extract_rejection_reason
from autoreview.oppo.config import OppoSubmissionConfig
from autoreview.oppo.errors import OppoError
from autoreview.oppo.rejection import analyze_rejection_reason

from .config_editor import (
    ConfigEditError,
    apply_config_patch_to_file,
    build_assignment_patch,
    build_json_patch,
    format_config_summary,
    format_patch_summary,
)
from .materials import MaterialBindError, bind_uploaded_material
from .state import JsonStateStore


JsonDict = dict[str, Any]

HELP_TEXT = """我可以协助 OPPO 审核提交流程：
1. 发送“分析驳回：<原因>”分析审核不通过原因。
2. 发送“状态”查看当前会话记录的应用和待补材料。
3. 发送“记录应用：应用名 / 包名 / 版本号”记录上下文。
4. 发送“准备提交”获取提交前检查清单。
5. 发送图片，我会优先用 OCR 识别；如果像 OPPO 驳回截图，会自动分析。
6. 发送“分析这张图”或“用最近图片分析驳回”，用最近一次图片 OCR 文本分析驳回。
7. 发送“整改清单”生成待办项。
8. 发送“查询审核状态”查询 OPPO 当前审核状态。
9. 发送“提交检查”检查配置、文件和重提风险。
10. 发送“查看提交配置”查看当前非密钥配置。
11. 发送“设置提交配置：字段=值”，再发送“确认保存配置”写入配置文件。
12. 发送文件或图片后，发送“绑定材料：APK/图标/截图1/版权证明/ICP证明”。
13. 发送“搜索竞品：关键词”搜索应用商店里的同类 APP。
14. 发送“记录竞品下载：关键词”搜索竞品并按当前月份写入会话状态。
15. 发送“清空当前记录”清空当前飞书会话状态；发送“清空所有记录”清空全部会话状态。"""


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
        oppo_agent_factory: Callable[[], Any] | None = None,
        market_searcher_factory: Callable[[], Any] | None = None,
        llm_client: Any | None = None,
    ):
        self.state_store = state_store
        self.oppo_config_path = Path(oppo_config_path) if oppo_config_path else None
        self.oppo_agent_factory = oppo_agent_factory
        self.market_searcher_factory = market_searcher_factory
        self.llm_client = llm_client

    def handle_message(self, session_id: str, text: str, sender_id: str | None = None) -> AgentResponse:
        clean_text = self._normalize_incoming_text(text)
        if not clean_text:
            return AgentResponse("我收到空消息了。发送“帮助”可以查看可用指令。", {})

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
                return AgentResponse("请按“分析驳回：<OPPO驳回原因>”发送完整原因。", {"intent": "analyze_rejection"})
            return self.analyze_rejection_text(session_id, reason, sender_id=sender_id)

        if clean_text in {"分析这张图", "用最近图片分析驳回", "分析最近图片", "分析图片"}:
            session = self.state_store.get_session(session_id)
            image_text = self._get_last_image_text(session)
            if not image_text:
                return AgentResponse(
                    "最近图片还没有可用于分析的 OCR 文本。请先发送一张包含驳回原因的截图。",
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

        if clean_text in {"确认保存配置", "保存配置", "确认配置"}:
            return self.confirm_config_update(session_id, sender_id=sender_id)

        if clean_text in {"取消保存配置", "取消配置修改", "放弃配置修改"}:
            return self.cancel_config_update(session_id, sender_id=sender_id)

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
                    "请按“搜索竞品：关键词”发送，例如“搜索竞品：英语四级单词”。",
                    {"intent": "market_search", "missing": "query"},
                )
            research_response = self._handle_app_store_data_platform_research(clean_text, query)
            if research_response:
                return research_response
            return self.search_competitors(session_id, query, sender_id=sender_id)

        generic_search_query = self._extract_generic_app_search_query(clean_text)
        if generic_search_query:
            research_response = self._handle_app_store_data_platform_research(clean_text, generic_search_query)
            if research_response:
                return research_response
            return self.search_competitors(session_id, generic_search_query, sender_id=sender_id)

        if clean_text.startswith(("记录竞品下载", "记录竞品月报", "月度记录竞品")):
            query = self._extract_market_query(clean_text, session_id)
            if not query:
                return AgentResponse(
                    "请按“记录竞品下载：关键词”发送，例如“记录竞品下载：英语四级单词”。",
                    {"intent": "market_download_snapshot", "missing": "query"},
                )
            return self.record_competitor_downloads(session_id, query, sender_id=sender_id)

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
                "已记录应用信息：\n"
                f"- 应用名：{app_info.get('app_name') or '未提供'}\n"
                f"- 包名：{app_info.get('pkg_name') or '未提供'}\n"
                f"- 版本号：{app_info.get('version_code') or '未提供'}",
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
    ) -> AgentResponse:
        query = _clean_market_query(query)
        if _is_contextual_app_reference(query):
            query = _clean_market_query((self._default_app_info(session_id) or {}).get("app_name"))
        if not query:
            return AgentResponse(
                "请提供有效的应用名或关键词，例如“搜索竞品：英语四级单词”。",
                {"intent": "market_search", "missing": "query"},
            )
        result = self._normalize_market_result(self._search_markets(session_id, query, limit=8))
        self.state_store.update_session(
            session_id,
            {
                "last_market_search": result.to_dict(),
                "sender_id": sender_id,
            },
        )
        return AgentResponse(
            self._format_market_search(result),
            {"intent": "market_search", "result": result.to_dict()},
        )

    def record_competitor_downloads(
        self,
        session_id: str,
        query: str,
        sender_id: str | None = None,
    ) -> AgentResponse:
        query = _clean_market_query(query)
        if _is_contextual_app_reference(query):
            query = _clean_market_query((self._default_app_info(session_id) or {}).get("app_name"))
        if not query:
            return AgentResponse(
                "请提供有效的应用名或关键词，例如“记录竞品下载：英语四级单词”。",
                {"intent": "market_download_snapshot", "missing": "query"},
            )
        result = self._normalize_market_result(self._search_markets(session_id, query, limit=8))
        snapshot = build_monthly_snapshot(query, result)
        session = self.state_store.get_session(session_id)
        snapshots = dict(session.get("market_download_snapshots") or {})
        snapshots[snapshot["month"]] = snapshot
        self.state_store.update_session(
            session_id,
            {
                "last_market_search": result.to_dict(),
                "market_download_snapshots": snapshots,
                "sender_id": sender_id,
            },
        )
        return AgentResponse(
            self._format_download_snapshot(snapshot),
            {"intent": "market_download_snapshot", "snapshot": snapshot},
        )

    def clear_session_state(self, session_id: str) -> AgentResponse:
        self.state_store.clear_session(session_id)
        return AgentResponse(
            "已清空当前会话记录。",
            {"intent": "clear_session_state", "session_id": session_id},
        )

    def clear_all_state(self) -> AgentResponse:
        self.state_store.clear_all()
        return AgentResponse(
            "已清空全部会话记录。",
            {"intent": "clear_all_state"},
        )

    def describe_default_app(self, session_id: str) -> AgentResponse:
        app_info = self._default_app_info(session_id)
        if not app_info:
            return AgentResponse(
                "当前还没有默认应用。可以发送“记录应用：应用名 / 包名 / 版本号”，或先查询审核状态。",
                {"intent": "describe_default_app", "missing": "app_info"},
            )
        return AgentResponse(
            "当前默认应用：\n"
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
                f"查询 OPPO 审核状态失败：{exc}",
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
                f"提交检查失败：{exc}",
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
                "还没有驳回分析结果。请先发送驳回截图，或发送“分析驳回：<原因>”。",
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
                "还没有配置 OPPO 配置文件路径，暂时不能查看提交配置。",
                {"intent": "view_submission_config", "missing": "config_path"},
            )
        try:
            text = format_config_summary(self.oppo_config_path)
        except ConfigEditError as exc:
            return AgentResponse(
                f"查看提交配置失败：{exc}",
                {"intent": "view_submission_config", "error": str(exc)},
            )
        return AgentResponse(text, {"intent": "view_submission_config"})

    def stage_config_assignment(
        self,
        session_id: str,
        payload: str,
        sender_id: str | None = None,
    ) -> AgentResponse:
        if not self.oppo_config_path:
            return AgentResponse(
                "还没有配置 OPPO 配置文件路径，暂时不能修改提交配置。",
                {"intent": "stage_config_update", "missing": "config_path"},
            )
        try:
            patch = build_assignment_patch(payload)
        except ConfigEditError as exc:
            return AgentResponse(
                f"配置修改暂存失败：{exc}",
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
                "还没有配置 OPPO 配置文件路径，暂时不能修改提交配置。",
                {"intent": "stage_config_update", "missing": "config_path"},
            )
        try:
            patch = build_json_patch(payload)
        except ConfigEditError as exc:
            return AgentResponse(
                f"批量配置暂存失败：{exc}",
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
                "没有待保存的配置修改。可以先发送“设置提交配置：字段=值”。",
                {"intent": "confirm_config_update", "missing": "pending_config_patch"},
            )
        if not self.oppo_config_path:
            return AgentResponse(
                "还没有配置 OPPO 配置文件路径，暂时不能保存提交配置。",
                {"intent": "confirm_config_update", "missing": "config_path"},
            )
        try:
            result = apply_config_patch_to_file(self.oppo_config_path, pending)
        except ConfigEditError as exc:
            return AgentResponse(
                f"保存配置失败：{exc}",
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
        return AgentResponse(
            "配置已保存。\n"
            f"- 备份：{Path(result['backup_path']).name}\n\n"
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
            "已取消待保存的配置修改。",
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
                "还没有配置 OPPO 配置文件路径，暂时不能绑定材料。",
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
                f"绑定材料失败：{exc}",
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
            "材料已绑定：\n"
            f"- 类型：{material_name}\n"
            f"- 保存到：{result['target_path']}\n"
            f"- 配置项：{', '.join(result['config_patch'].keys())}\n\n"
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
                return self.record_competitor_downloads(session_id, query, sender_id=sender_id)
            return self.search_competitors(session_id, query, sender_id=sender_id)

        if self._looks_like_last_image_analysis_request(lowered):
            session = self.state_store.get_session(session_id)
            image_text = self._get_last_image_text(session)
            if not image_text:
                return AgentResponse(
                    "最近图片还没有可用于分析的 OCR 文本。请先发送一张包含驳回原因的截图。",
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
                "已记录应用信息：\n"
                f"- 应用名：{app_info.get('app_name') or '未提供'}\n"
                f"- 包名：{app_info.get('pkg_name') or '未提供'}\n"
                f"- 版本号：{app_info.get('version_code') or '未提供'}",
                {"intent": "record_app", "app_info": app_info, "semantic": True},
            )

        if self._looks_like_session_status_request(lowered):
            session = self.state_store.get_session(session_id)
            return AgentResponse(self._format_session(session), {"intent": "status", "session": session, "semantic": True})

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
        try:
            decision = self.llm_client.interpret(text, self._llm_context(session_id))
        except Exception as exc:
            return {"intent": "llm_error", "error": str(exc)}
        if not isinstance(decision, dict):
            return {"intent": "llm_error", "error": "LLM decision must be a JSON object"}
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
                    f"我尝试调用大模型理解这句话，但失败了：{_shorten(decision.get('error'))}",
                    {"intent": "llm_error", "error": str(decision.get("error") or "")},
                )
            return None
        confidence = _optional_confidence(decision.get("confidence"))
        if confidence is not None and confidence < 0.45:
            if allow_chat:
                reply = str(decision.get("reply") or "").strip()
                return AgentResponse(
                    reply or "我不太确定你的意思。可以换个说法，或发送“帮助”查看可用能力。",
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
        return AgentResponse(reply, {"intent": intent, "llm": decision})

    def _llm_context(self, session_id: str) -> JsonDict:
        session = self.state_store.get_session(session_id)
        return {
            "session": session,
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
                str(decision.get("reply") or "我在。你可以直接说要打包、查审核、分析驳回、看竞品或改配置。").strip(),
                {"intent": "chat"},
            )
        if intent == "remember":
            reply = str(decision.get("reply") or "我记住了。").strip()
            return AgentResponse(reply, {"intent": "remember", "memories": decision.get("memories") or []})
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
                "已记录应用信息：\n"
                f"- 应用名：{stored.get('app_name') or '未提供'}\n"
                f"- 包名：{stored.get('pkg_name') or '未提供'}\n"
                f"- 版本号：{stored.get('version_code') or '未提供'}",
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
                return AgentResponse("最近图片还没有可用于分析的 OCR 文本。", {"intent": intent, "missing": "ocr_text"})
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
        if intent == "view_submission_config":
            return self.view_submission_config()
        if intent == "stage_config_update":
            assignment = str(decision.get("config_assignment") or self._extract_assignment_payload(text)).strip()
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
                return self.record_competitor_downloads(session_id, query, sender_id=sender_id)
            return self.search_competitors(session_id, query, sender_id=sender_id)
        return None

    def _store_llm_memories(
        self,
        session_id: str,
        decision: JsonDict,
        sender_id: str | None = None,
    ) -> None:
        memories = [str(item).strip() for item in decision.get("memories") or [] if str(item).strip()]
        if not memories:
            return
        session = self.state_store.get_session(session_id)
        existing = [str(item).strip() for item in session.get("agent_memory") or [] if str(item).strip()]
        merged = existing[:]
        for item in memories:
            if item not in merged:
                merged.append(item)
        self.state_store.update_session(
            session_id,
            {
                "agent_memory": merged[-30:],
                "sender_id": sender_id,
            },
        )

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
        return AppMarketSearcher()

    def _search_markets(self, session_id: str, query: str, *, limit: int) -> Any:
        stores = self._allowed_market_stores(session_id)
        searcher = self._make_market_searcher()
        if stores is None:
            return searcher.search_competitors(query, limit=limit)
        return searcher.search_competitors(query, limit=limit, stores=stores)

    def _allowed_market_stores(self, session_id: str) -> set[str] | None:
        preferences = self._market_store_preferences(session_id)
        disabled = preferences.get("disabled_stores") or []
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
        self.state_store.update_session(
            session_id,
            {
                "market_store_preferences": updated,
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

        if self._contains_any(lowered, ("竞品", "应用商店", "应用市场", "下载量")):
            return AgentResponse(
                "有竞品搜索能力。可以发送“搜索竞品：关键词”查询同类 APP；发送“记录竞品下载：关键词”会把本月能拿到的公开指标写入当前会话。"
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
        return bool(re.search(r"能不能.*(ocr|image2|识别|搜索|查询|竞品|下载量)", text, flags=re.IGNORECASE))

    @staticmethod
    def _looks_like_market_store_scope_question(text: str) -> bool:
        return any(term in text for term in ("哪些", "那些", "哪几", "多少", "列表", "厂家", "厂商", "渠道")) and any(
            term in text for term in ("应用商店", "应用市场", "商店", "市场", "厂家", "厂商", "渠道")
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
        record_terms = ("记录", "保存", "月度", "每月", "月报", "下载量", "下载数据", "指标")
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
    def _format_rejection_analysis(analysis: JsonDict) -> str:
        lines = ["驳回分析："]
        conclusion = "可以尝试提交" if analysis.get("can_resubmit_same_apk") else "不建议原包直接重提"
        lines.append(f"- 结论：{conclusion}")
        if analysis.get("similarity_score") is not None:
            lines.append(f"- APK 相似度：{analysis['similarity_score']}")
        if analysis.get("similar_app"):
            lines.append(f"- 疑似相似应用：{analysis['similar_app']}")
        if analysis.get("required_actions"):
            lines.append("需要做：")
            lines.extend(f"- {item}" for item in analysis["required_actions"][:3])
        if analysis.get("evidence_targets"):
            targets = [target.replace("OPPO backend: ", "") for target in analysis["evidence_targets"]]
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
                f"\n- 最近竞品搜索：{market_search.get('query') or '已记录'}"
                f"（{len(market_search.get('apps') or [])} 个结果）"
            )
        snapshots = session.get("market_download_snapshots") or {}
        snapshot_line = f"\n- 竞品月度记录：{len(snapshots)} 个月" if snapshots else ""
        preferences = session.get("market_store_preferences") or {}
        disabled_stores = [_store_label(store) for store in preferences.get("disabled_stores") or []]
        preference_line = f"\n- 应用商店偏好：不查询 {'、'.join(disabled_stores)}" if disabled_stores else ""
        memory = session.get("agent_memory") or []
        memory_line = f"\n- 长期记忆：{len(memory)} 条" if memory else ""
        return (
            "当前会话状态：\n"
            f"- 应用名：{app_info.get('app_name') or '未记录'}\n"
            f"- 包名：{app_info.get('pkg_name') or '未记录'}\n"
            f"- 版本号：{app_info.get('version_code') or '未记录'}\n"
            f"- 是否建议同包重提："
            f"{'未知' if not analysis else ('是' if analysis.get('can_resubmit_same_apk') else '否')}"
            f"{image_line}"
            f"{remediation_line}"
            f"{market_line}"
            f"{snapshot_line}"
            f"{preference_line}"
            f"{memory_line}"
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
        lines = [
            "OPPO 审核状态：",
            f"- 应用：{status.get('pkg_name')}",
            f"- 版本：{status.get('version_code')}",
            f"- 状态：{audit_text}",
        ]
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
        lines = ["提交检查："]
        lines.append(f"- 配置文件：{'通过' if validation.get('valid') else '不通过'}")
        missing_fields = validation.get("missing_required_fields") or []
        missing_files = validation.get("missing_files") or []
        if missing_fields:
            lines.append("- 缺字段：" + "、".join(str(item) for item in missing_fields[:8]))
        if missing_files:
            lines.append("- 缺文件：" + "、".join(str(item) for item in missing_files[:5]))
        analysis = session.get("last_rejection_analysis") or {}
        if analysis and not analysis.get("can_resubmit_same_apk"):
            lines.append("- 风险：最近驳回分析显示不建议原包直接重提")
        checklist = session.get("remediation_checklist") or ReviewAgent._build_remediation_items(analysis)
        if checklist:
            lines.append(f"- 整改待办：{len(checklist)} 项，发送“整改清单”查看")
        if validation.get("valid") and not (analysis and not analysis.get("can_resubmit_same_apk")):
            lines.append("结论：可以进入人工确认提交步骤。")
        else:
            lines.append("结论：先补齐缺口或处理风险，再提交。")
        return "\n".join(lines)

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
        lines = ["整改清单："]
        lines.extend(f"{index}. {item}" for index, item in enumerate(items, start=1))
        targets = [target.replace("OPPO backend: ", "") for target in analysis.get("evidence_targets") or []]
        if targets:
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
    def _format_market_search(result: AppMarketSearchResult) -> str:
        lines = [f"应用商店竞品搜索：{result.query}"]
        if not result.apps:
            lines.append("- 未找到结果。可以换更具体的关键词或应用名。")
        for app in result.apps[:8]:
            metrics = _format_market_metrics(app.to_dict())
            developer = f" / {app.developer}" if app.developer else ""
            category = f" / {app.category}" if app.category else ""
            lines.append(f"- [{_store_label(app.store)}] {app.name}{developer}{category}：{metrics}")
        if result.store_statuses:
            lines.append("已查询：")
            lines.extend(_format_store_status(item) for item in result.store_statuses)
        if result.errors:
            lines.append("部分商店查询失败：" + "；".join(result.errors[:3]))
        lines.append("提示：发送“记录竞品下载：同一关键词”可把本月公开指标写入状态。")
        return "\n".join(lines)

    @staticmethod
    def _format_download_snapshot(snapshot: JsonDict) -> str:
        lines = [f"已记录 {snapshot['month']} 竞品下载数据：{snapshot.get('query') or ''}"]
        apps = snapshot.get("apps") or []
        if not apps:
            lines.append("- 未记录到应用结果。")
        for app in apps[:8]:
            lines.append(
                f"- [{_store_label(app.get('store'))}] {app.get('name') or app.get('app_id')}: "
                f"{_format_market_metrics(app)}"
            )
        if snapshot.get("store_statuses"):
            lines.append("已查询：")
            lines.extend(_format_store_status(item) for item in snapshot["store_statuses"])
        if snapshot.get("errors"):
            lines.append("部分商店查询失败：" + "；".join(str(item) for item in snapshot["errors"][:3]))
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
    lines = ["目前竞品搜索会尝试查询这些应用商店："]
    lines.extend(f"- {label}" for _, label in active)
    if inactive:
        lines.append("当前会话已按你的偏好排除：")
        lines.extend(f"- {label}" for _, label in inactive)
    lines.append("说明：这些都是公开页面/公开接口查询，不同商店可见数据不同；OPPO、vivo、华为等入口可能因公开页面限制而跳过或拿不到结果。")
    return "\n".join(lines)


def _extract_market_store_name(text: str) -> str:
    lowered = str(text or "").lower()
    aliases = {
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


def _optional_confidence(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
