"""Trajectory file recording for CockroachDB operator requests."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_id(value: str | None, fallback: str) -> str:
    text = value or fallback
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")
    return text[:96] or fallback


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseMessage):
        data: dict[str, Any] = {
            "type": type(value).__name__,
            "content": _json_safe(value.content),
        }
        if isinstance(value, AIMessage):
            data["tool_calls"] = _json_safe(getattr(value, "tool_calls", None) or [])
            data["response_metadata"] = _json_safe(getattr(value, "response_metadata", None) or {})
        if isinstance(value, ToolMessage):
            data["name"] = value.name
            data["tool_call_id"] = value.tool_call_id
        return data
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


def _iter_messages(value: Any):
    if isinstance(value, BaseMessage):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_messages(item)
        return
    if isinstance(value, list | tuple):
        for item in value:
            yield from _iter_messages(item)


def _tool_event_from_message(message: BaseMessage) -> list[dict[str, Any]]:
    if isinstance(message, AIMessage):
        return [
            {
                "type": "tool_call",
                "name": call.get("name"),
                "id": call.get("id"),
                "args": _json_safe(call.get("args") or {}),
            }
            for call in getattr(message, "tool_calls", None) or []
        ]
    if isinstance(message, ToolMessage):
        return [
            {
                "type": "tool_result",
                "name": message.name,
                "tool_call_id": message.tool_call_id,
                "content": _json_safe(message.content),
            }
        ]
    return []


def _history_message(message: BaseMessage) -> dict[str, Any]:
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": _json_safe(message.content)}
    if isinstance(message, AIMessage):
        data: dict[str, Any] = {"role": "assistant", "content": _json_safe(message.content)}
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            data["tool_calls"] = _json_safe(tool_calls)
        return data
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "name": message.name,
            "tool_call_id": message.tool_call_id,
            "content": _json_safe(message.content),
        }
    return {"role": getattr(message, "type", type(message).__name__), "content": _json_safe(message.content)}


class TrajectoryRecorder:
    """Collect and atomically write one JSON trajectory per A2A context."""

    def __init__(
        self,
        *,
        output_dir: str,
        task_id: str,
        context_id: str,
        user_input: str,
        agent_version: str,
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.output_dir = Path(output_dir)
        self.task_id = task_id
        self.context_id = context_id
        self.started_at = _utc_now()
        filename = f"context-{_safe_id(context_id, 'context')}.json"
        self.path = self.output_dir / filename
        self.data: dict[str, Any] = {
            "metadata": {
                "latest_task_id": task_id,
                "context_id": context_id,
                "started_at": self.started_at,
                "updated_at": None,
                "status": "running",
                "agent_version": agent_version,
            },
            "messages": [_history_message(HumanMessage(content=user_input))] if user_input else [],
            "turns": [
                {
                    "task_id": task_id,
                    "started_at": self.started_at,
                    "finished_at": None,
                    "status": "running",
                    "input": {"text": user_input},
                    "mcp_connection": None,
                    "events": [],
                    "tool_events": [],
                    "final": None,
                    "error": None,
                }
            ],
            "input": {"text": user_input},
            "mcp_connection": None,
            "events": [],
            "tool_events": [],
            "final": None,
            "error": None,
        }

    def record_mcp_connection(self, *, success: bool, tools: list[str] | None = None, error: BaseException | None = None) -> None:
        if not self.enabled:
            return
        connection = {
            "success": success,
            "tools": tools or [],
            "error": None
            if error is None
            else {"type": type(error).__name__, "message": str(error)},
        }
        self.data["mcp_connection"] = connection
        self.data["turns"][-1]["mcp_connection"] = connection

    def record_graph_update(self, event: dict[str, Any], formatted_event: str = "") -> None:
        if not self.enabled:
            return
        graph_event = {
            "timestamp": _utc_now(),
            "formatted": formatted_event,
            "raw": _json_safe(event),
        }
        self.data["events"].append(graph_event)
        self.data["turns"][-1]["events"].append(graph_event)
        for message in _iter_messages(event):
            tool_events = _tool_event_from_message(message)
            self.data["tool_events"].extend(tool_events)
            self.data["turns"][-1]["tool_events"].extend(tool_events)

    def record_messages(self, messages: list[BaseMessage]) -> None:
        if not self.enabled:
            return
        self.data["messages"] = [_history_message(message) for message in messages]

    def finish(self, *, status: str, final_text: str | None = None, error: BaseException | None = None) -> None:
        if not self.enabled:
            return
        self.data["metadata"]["finished_at"] = _utc_now()
        self.data["metadata"]["updated_at"] = self.data["metadata"]["finished_at"]
        self.data["metadata"]["status"] = status
        self.data["final"] = {"text": final_text} if final_text is not None else None
        self.data["turns"][-1]["finished_at"] = self.data["metadata"]["finished_at"]
        self.data["turns"][-1]["status"] = status
        self.data["turns"][-1]["final"] = self.data["final"]
        if error is not None:
            self.data["error"] = {"type": type(error).__name__, "message": str(error)}
            self.data["turns"][-1]["error"] = self.data["error"]

    def write(self) -> Path | None:
        if not self.enabled:
            return None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            previous = json.loads(self.path.read_text(encoding="utf-8"))
            self.data["metadata"]["started_at"] = previous.get("metadata", {}).get("started_at", self.started_at)
            self.data["turns"] = [*previous.get("turns", []), *self.data["turns"]]
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp_path, self.path)
        return self.path
