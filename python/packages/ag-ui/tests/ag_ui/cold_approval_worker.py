# Copyright (c) Microsoft. All rights reserved.

"""One process in the disk-backed approval lifecycle regression."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent_framework import Agent, AgentSession, ChatResponseUpdate, Content, FunctionTool, Message
from conftest import StreamingChatClientStub
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_framework_ag_ui import AgentFrameworkAgent, add_agent_framework_fastapi_endpoint
from agent_framework_ag_ui._snapshots import AGUIThreadSnapshot


def main(root: Path, action: str) -> None:
    class DiskSnapshots:
        saves = 0

        async def get(self, *, scope: str, thread_id: str) -> AGUIThreadSnapshot | None:
            path = root / "snapshot.json"
            return AGUIThreadSnapshot(**json.loads(path.read_text())) if path.exists() else None

        async def save(self, *, scope: str, thread_id: str, snapshot: AGUIThreadSnapshot) -> None:
            self.saves += 1
            if action == "fail-claim" or (action == "fail-result" and self.saves > 1):
                raise OSError("Storage unavailable")
            temporary = root / "snapshot.tmp"
            temporary.write_text(json.dumps(asdict(snapshot)))
            temporary.replace(root / "snapshot.json")

        async def delete(self, **kwargs: Any) -> bool:
            raise AssertionError("No deletion expected")

        async def clear(self, **kwargs: Any) -> None:
            raise AssertionError("No clearing expected")

    def get_weather(city: str) -> str:
        with (root / "executions.txt").open("a") as file:
            file.write(city + "\n")
        if action == "crash-approve":
            print("[]", flush=True)
            os._exit(0)
        return json.dumps({"status": "pending_approval", "city": city}, separators=(",", ":"))

    async def stream(messages: Any, options: Any, **kwargs: Any):
        if action in {"pause", "sdk-pause"}:
            yield ChatResponseUpdate(
                contents=[
                    Content.from_function_call(
                        id="native-occurrence",
                        call_id="provider-call",
                        name="get_weather",
                        arguments={"city": "Seattle"},
                    )
                ]
            )
        else:
            yield ChatResponseUpdate(contents=[Content.from_text("Done.")])

    agent = Agent(
        client=StreamingChatClientStub(stream),
        tools=[
            FunctionTool(name="get_weather", description="Weather", func=get_weather, approval_mode="always_require")
        ],
    )
    if action.startswith("sdk-"):

        async def run_sdk() -> None:
            path = root / "session.json"
            session = AgentSession.from_dict(json.loads(path.read_text())) if path.exists() else agent.create_session()
            if action == "sdk-pause":
                incoming = "Weather?"
            else:
                request_content = Content.from_dict(json.loads((root / "request.json").read_text()))
                incoming = [
                    Message(
                        role="user",
                        contents=[request_content.to_function_approval_response(approved=action == "sdk-approve")],
                    )
                ]
            response = await agent.run(incoming, session=session, stream=True).get_final_response()
            contents = [content for message in response.messages for content in message.contents]
            if action == "sdk-pause":
                approval = next(content for content in contents if content.type == "function_approval_request")
                (root / "request.json").write_text(json.dumps(approval.to_dict()))
            path.write_text(json.dumps(session.to_dict()))
            print(json.dumps([content.to_dict() for content in contents]))

        asyncio.run(run_sdk())
        return
    app = FastAPI()
    add_agent_framework_fastapi_endpoint(
        app,
        AgentFrameworkAgent(agent, durable_approvals=True),
        path="/agent",
        snapshot_store=DiskSnapshots(),
        snapshot_scope_resolver=lambda request: "tenant-b" if action == "other-scope" else "tenant-a",
    )
    request: dict[str, Any] = {"threadId": "thread-a", "runId": action, "messages": []}
    if action == "pause":
        request["messages"] = [{"id": "question", "role": "user", "content": "Weather?"}]
    elif action != "hydrate":
        request["resume"] = [
            {"interruptId": "native-occurrence", "status": "resolved", "payload": {"approved": action != "decline"}}
        ]
    response = TestClient(app).post("/agent", json=request)
    response.raise_for_status()
    print(json.dumps([json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]))


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
