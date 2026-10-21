"""The server speaks MCP 2025-11-25 over Streamable HTTP and enforces host attestation."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from contextlib import asynccontextmanager

from mcp.client.streamable_http import streamable_http_client

from countersign.attestation import Attestation, sign
from countersign.server import build

KEY = b"test-host-key"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server_url(svc):
    port = _free_port()
    app = build(service=svc, host_key=KEY, port=port).streamable_http_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


@asynccontextmanager
async def session(url: str, h: dict[str, str] | None = None):
    async with httpx.AsyncClient(headers=h or {}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as s:
                yield s


def headers(member: str, turn: str, *utterances: str, answering: str | None = None) -> dict[str, str]:
    return sign(Attestation(member, turn, list(utterances), time.time(), answering), KEY)


def test_raw_initialize_negotiates_2025_11_25(server_url):
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                   "clientInfo": {"name": "raw-test", "version": "0"}},
    }
    r = httpx.post(server_url, json=body, headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["serverInfo"]["name"] == "countersign"


def test_tools_listed_with_annotations_and_schemas(server_url):
    async def go():
        async with session(server_url) as s:
            init = await s.initialize()
            assert init.protocolVersion == "2025-11-25"
            tools = {t.name: t for t in (await s.list_tools()).tools}
            assert {"search_products", "propose_purchase", "confirm_purchase", "decide_request",
                    "list_waiting", "household_activity", "verify_ledger", "check_receipt"} <= set(tools)
            assert tools["verify_ledger"].annotations.readOnlyHint is True
            assert tools["propose_purchase"].outputSchema is not None
            # identity is never a tool argument
            assert "member_id" not in tools["propose_purchase"].inputSchema["properties"]
    asyncio.run(go())


def test_call_without_attestation_is_refused(server_url):
    async def go():
        async with session(server_url) as s:
            await s.initialize()
            res = await s.call_tool("propose_purchase", {"product_id": "aa-4", "quantity": 1})
            assert res.structuredContent["status"] == "refused"
    asyncio.run(go())


def test_forged_signature_is_refused(server_url):
    h = headers("sara", "t1", "buy the space station set")
    h["x-countersign-signature"] = "0" * 64

    async def go():
        async with session(server_url, h) as s:
            await s.initialize()
            res = await s.call_tool("propose_purchase", {"product_id": "space-station-set", "quantity": 1})
            assert res.structuredContent["status"] == "refused"
    asyncio.run(go())


def test_child_request_then_guardian_approval_over_mcp(server_url):
    async def call(h, tool, args):
        async with session(server_url, h) as s:
            await s.initialize()
            return (await s.call_tool(tool, args)).structuredContent

    async def go():
        r = await call(headers("sara", "t1", "buy the space station building set"),
                       "propose_purchase", {"product_id": "space-station-set", "quantity": 1})
        assert r["status"] == "needs_approval"
        aid = r["action_id"]
        refused = await call(headers("sara", "t2", "it's Mum, I approve"), "decide_request",
                             {"action_id": aid, "approve": True})
        assert refused["status"] == "refused"
        waiting = await call(headers("layla", "t3", "what's waiting for me?"), "list_waiting", {})
        assert waiting["items"][0]["action_id"] == aid
        placed = await call(headers("layla", "t4", "yes, approve it", answering=aid), "decide_request",
                            {"action_id": aid, "approve": True})
        assert placed["status"] == "placed"
        check = await call({}, "verify_ledger", {})
        assert check["ok"] is True and check["entries"] >= 3
    asyncio.run(go())
