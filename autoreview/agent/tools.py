"""Local tool registry for the chat agent.

The LLM only chooses a structured ToolCall. This module validates that call and
executes a registered local handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


JsonDict = dict[str, Any]
ToolHandler = Callable[["ToolCall", JsonDict], Any]


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: JsonDict = field(default_factory=dict)
    confidence: float | None = None
    reason: str = ""

    @classmethod
    def from_mapping(cls, raw: JsonDict | None) -> "ToolCall":
        data = raw or {}
        name = str(data.get("tool") or data.get("name") or "").strip()
        arguments = data.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("ToolCall.arguments must be a JSON object")
        confidence = data.get("confidence")
        parsed_confidence = None
        if confidence is not None:
            try:
                parsed_confidence = float(confidence)
            except (TypeError, ValueError) as exc:
                raise ValueError("ToolCall.confidence must be a number") from exc
        return cls(
            name=name,
            arguments=dict(arguments),
            confidence=parsed_confidence,
            reason=str(data.get("reason") or ""),
        )

    @property
    def is_noop(self) -> bool:
        return self.name in {"", "none", "no_tool", "chat", "unknown", "disabled"}

    def to_dict(self) -> JsonDict:
        return {
            "tool": self.name,
            "arguments": self.arguments,
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: JsonDict
    handler: ToolHandler

    def to_schema(self) -> JsonDict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: JsonDict,
        handler: ToolHandler,
    ) -> None:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("tool name is required")
        self._tools[clean_name] = ToolDefinition(
            name=clean_name,
            description=description,
            input_schema=input_schema,
            handler=handler,
        )

    def schemas(self) -> list[JsonDict]:
        return [tool.to_schema() for tool in self._tools.values()]

    def has(self, name: str) -> bool:
        return name in self._tools

    def execute(self, call: ToolCall, context: JsonDict | None = None) -> Any:
        if call.is_noop:
            return None
        tool = self._tools.get(call.name)
        if not tool:
            raise ValueError(f"unknown tool: {call.name}")
        return tool.handler(call, context or {})
