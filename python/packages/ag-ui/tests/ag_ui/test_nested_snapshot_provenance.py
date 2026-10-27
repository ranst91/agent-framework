# Copyright (c) Microsoft. All rights reserved.

"""Nested result order and public ancestry survive a serialized cold restore."""

import json
from dataclasses import asdict

from agent_framework import Content

from agent_framework_ag_ui import AGUIThreadSnapshot, InMemoryAGUIThreadSnapshotStore
from agent_framework_ag_ui._agent_run import _build_messages_snapshot, _build_safe_point_messages_snapshot
from agent_framework_ag_ui._run_common import FlowState, _emit_tool_call, _emit_tool_result
from agent_framework_ag_ui._snapshot_session import ThreadSnapshotSession


async def test_nested_results_preserve_order_and_ancestry_after_cold_restore():
    flow = FlowState(message_id="outer-message")

    def start(call_id, name, parent=None):
        content = Content.from_function_call(call_id=call_id, name=name, arguments='{"surfaceId":"same"}')
        if parent:
            content.additional_properties["ag_ui_parent_tool_call_id"] = parent
        _emit_tool_call(content, flow)

    def finish(call_id):
        _emit_tool_result(Content.from_function_result(call_id=call_id, result="canonical:" + call_id), flow)

    start("outer", "generate_a2ui")
    start("first", "render_a2ui", "outer")
    finish("first")
    safe = _build_safe_point_messages_snapshot(flow, []).model_dump(by_alias=True, exclude_none=True)["messages"]
    assert {call["id"] for message in safe for call in message.get("toolCalls", [])} == {"outer", "first"}
    assert [message["toolCallId"] for message in safe if message["role"] == "tool"] == ["first"]
    start("second", "render_a2ui", "outer")
    finish("second")
    finish("outer")
    start("independent", "render_a2ui")
    finish("independent")
    snapshot = _build_messages_snapshot(flow, []).model_dump(by_alias=True, exclude_none=True)["messages"]
    sequence = []
    for message in snapshot:
        sequence.extend(call["id"] for call in message.get("toolCalls", []))
        if message["role"] == "tool":
            sequence.append("result:" + message["toolCallId"])
    assert sequence == [
        "outer",
        "first",
        "result:first",
        "second",
        "result:second",
        "result:outer",
        "independent",
        "result:independent",
    ]
    serialized = json.dumps(asdict(AGUIThreadSnapshot(messages=snapshot)))
    store = InMemoryAGUIThreadSnapshotStore()
    await store.save(scope="scope", thread_id="thread", snapshot=AGUIThreadSnapshot(**json.loads(serialized)))
    cold = await ThreadSnapshotSession.open(store=store, scope="scope", thread_id="thread")
    hydrated = [event async for event in cold.hydrate_events(run_id="cold")]
    replay = next(event for event in hydrated if event.type == "MESSAGES_SNAPSHOT").model_dump(
        by_alias=True, exclude_none=True
    )["messages"]
    assert replay == snapshot
    calls = {call["id"]: call for message in replay for call in message.get("toolCalls", [])}
    assert calls["first"]["parentToolCallId"] == calls["second"]["parentToolCallId"] == "outer"
    assert "parentToolCallId" not in calls["independent"]
