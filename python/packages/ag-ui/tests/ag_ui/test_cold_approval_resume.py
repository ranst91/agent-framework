# Copyright (c) Microsoft. All rights reserved.

"""Native approvals survive independent endpoint processes with a JSON snapshot store."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _process(root: Path, action: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("cold_approval_worker.py")), str(root), action],
        capture_output=True,
        text=True,
        check=True,
        env=os.environ.copy(),
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("decision", ["approve", "decline"])
def test_native_approval_cold_resume(tmp_path: Path, decision: str) -> None:
    paused = _process(tmp_path, "pause")
    interrupt = paused[-1]["outcome"]["interrupts"][0]
    assert interrupt["id"] == "native-occurrence"
    assert "__ag_ui_approval_authority" not in json.dumps(paused)
    resumed = _process(tmp_path, decision)
    assert not [event for event in resumed if event["type"] == "RUN_ERROR"]
    expected = (
        '{"status":"pending_approval","city":"Seattle"}'
        if decision == "approve"
        else "Error: Tool call invocation was rejected by user."
    )
    snapshot = json.loads((tmp_path / "snapshot.json").read_text())
    results = [message for message in snapshot["messages"] if message.get("tool_call_id") == "provider-call"]
    assert [message["content"] for message in results] == [expected]
    assert not snapshot["interrupt"]
    live_results = [event for event in resumed if event["type"] == "TOOL_CALL_RESULT"]
    if decision == "approve":
        assert [(event["toolCallId"], event["content"]) for event in live_results] == [("provider-call", expected)]
        assert resumed.index(live_results[0]) < next(
            index for index, event in enumerate(resumed) if event["type"] == "TEXT_MESSAGE_CONTENT"
        )
    retry = _process(tmp_path, decision)
    assert not [event for event in retry if event["type"] == "RUN_ERROR"]
    hydrated = _process(tmp_path, "hydrate")
    assert "outcome" not in hydrated[-1]
    replay = next(event["messages"] for event in hydrated if event["type"] == "MESSAGES_SNAPSHOT")
    assert [
        (message["toolCallId"], message["content"])
        for message in replay
        if message.get("toolCallId") == "provider-call"
    ] == [("provider-call", expected)]
    execution_file = tmp_path / "executions.txt"
    assert (execution_file.read_text() if execution_file.exists() else "") == (
        "Seattle\n" if decision == "approve" else ""
    )


def test_cold_resume_claim_write_failure_executes_nothing(tmp_path: Path) -> None:
    _process(tmp_path, "pause")
    failed = _process(tmp_path, "fail-claim")
    assert [event for event in failed if event["type"] == "RUN_ERROR"]
    assert not (tmp_path / "executions.txt").exists()
    resumed = _process(tmp_path, "approve")
    assert not [event for event in resumed if event["type"] == "RUN_ERROR"]
    assert (tmp_path / "executions.txt").read_text() == "Seattle\n"


@pytest.mark.parametrize("action", ["crash-approve", "fail-result"])
def test_cold_resume_uncertain_execution_cannot_run_twice(tmp_path: Path, action: str) -> None:
    _process(tmp_path, "pause")
    _process(tmp_path, action)
    assert (tmp_path / "executions.txt").read_text() == "Seattle\n"
    retry = _process(tmp_path, "approve")
    assert [event for event in retry if event["type"] == "RUN_ERROR"]
    assert (tmp_path / "executions.txt").read_text() == "Seattle\n"


@pytest.mark.parametrize("action", ["approve", "other-scope"])
def test_replay_data_cannot_recreate_authority(tmp_path: Path, action: str) -> None:
    _process(tmp_path, "pause")
    if action == "approve":
        path = tmp_path / "snapshot.json"
        snapshot = json.loads(path.read_text())
        private = snapshot.pop("session_state")
        snapshot["session_state"] = None
        snapshot["state"] = private  # Client/shared state is never an authority source.
        path.write_text(json.dumps(snapshot))
    response = _process(tmp_path, action)
    assert [event for event in response if event["type"] == "RUN_ERROR"]
    assert not (tmp_path / "executions.txt").exists()


@pytest.mark.parametrize("decision", ["approve", "decline"])
def test_native_sdk_session_cold_resume(tmp_path: Path, decision: str) -> None:
    """SDK AgentSession persistence is a separate path from AG-UI snapshot authority."""
    _process(tmp_path, "sdk-pause")
    response = _process(tmp_path, "sdk-" + decision)
    results = [content for content in response if content["type"] == "function_result"]
    expected = (
        '{"status":"pending_approval","city":"Seattle"}'
        if decision == "approve"
        else "Error: Tool call invocation was rejected by user."
    )
    assert [(content["call_id"], content["result"]) for content in results] == [("provider-call", expected)]
    before = json.loads((tmp_path / "session.json").read_text())
    _process(tmp_path, "sdk-" + decision)
    after = json.loads((tmp_path / "session.json").read_text())
    assert before["session_id"] == after["session_id"]
    for saved in (before, after):
        persisted = [
            content
            for message in saved["state"]["in_memory"]["messages"]
            for content in message["contents"]
            if content["type"] == "function_result"
        ]
        assert [(content["call_id"], content["result"]) for content in persisted] == [("provider-call", expected)]
    execution_file = tmp_path / "executions.txt"
    assert (execution_file.read_text() if execution_file.exists() else "") == (
        "Seattle\n" if decision == "approve" else ""
    )
