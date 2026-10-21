"""Countersign MCP server: Streamable HTTP, MCP spec 2025-11-25.

Run:  countersign-server            (listens on http://127.0.0.1:8787/mcp)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from . import __version__, intent
from .attestation import Attestation, AttestationError, verify
from .policy import Household
from .service import (
    ActionResult,
    ActivityResult,
    Countersign,
    LedgerCheck,
    PendingResult,
    ReceiptCheck,
    SearchResult,
)

log = logging.getLogger("countersign")
ROOT = Path(__file__).resolve().parents[2]

INSTRUCTIONS = """Countersign sits between a household voice assistant and every purchase.
Always call propose_purchase before buying anything; never tell the person an order is placed
unless a tool returned status "placed". Read the "say" field aloud. Who is speaking and what
they said come from the device, not from you, so do not pass or invent them."""


def build(
    service: Countersign | None = None,
    host_key: bytes | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> FastMCP:
    key = host_key or os.environ.get("COUNTERSIGN_HOST_KEY", "").encode()
    if not key:
        key = b"dev-only-host-key"
        log.warning("COUNTERSIGN_HOST_KEY not set; using the development key. Do not expose this server.")
    if service is None:
        data = Path(os.environ.get("COUNTERSIGN_DATA", ROOT / "data"))
        service = Countersign(
            db_path=os.environ.get("COUNTERSIGN_DB", ROOT / "countersign.db"),
            household=Household.load(data / "household.json"),
            catalogue_path=data / "catalogue.json",
            judge=intent.default_judge(),
            screen=intent.default_screen(),
        )
    svc = service

    mcp = FastMCP(
        "countersign",
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
    )

    mcp._mcp_server.version = __version__  # report Countersign's version, not the SDK's

    def attested(ctx: Context) -> Attestation:
        request = ctx.request_context.request
        headers = dict(request.headers) if request is not None else {}
        return verify(headers, key)

    def refused(exc: AttestationError) -> ActionResult:
        return ActionResult(action_id=None, status="refused", say=f"I cannot act on this request: {exc}.")

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    acts = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

    @mcp.tool(title="Search products", annotations=read_only)
    def search_products(query: str) -> SearchResult:
        """Search the household's merchants for products. Returns ids to use with propose_purchase."""
        return svc.search(query)

    @mcp.tool(title="Propose a purchase", annotations=acts)
    def propose_purchase(product_id: str, quantity: int, ctx: Context) -> ActionResult:
        """Ask Countersign to buy something for the person speaking. It checks the household rules
        and whether the item matches what the person said. Status is one of placed,
        needs_confirmation, needs_approval or blocked. Read "say" aloud."""
        try:
            att = attested(ctx)
        except AttestationError as exc:
            return refused(exc)
        return svc.propose(att, product_id, quantity)

    @mcp.tool(title="Confirm a purchase", annotations=acts)
    def confirm_purchase(action_id: str, ctx: Context) -> ActionResult:
        """Call after the person answers a needs_confirmation question. Countersign reads their
        answer from the device transcript, so call it whether they said yes or no."""
        try:
            att = attested(ctx)
        except AttestationError as exc:
            return refused(exc)
        return svc.confirm(att, action_id)

    @mcp.tool(title="Approve or decline a request", annotations=acts)
    def decide_request(action_id: str, approve: bool, ctx: Context) -> ActionResult:
        """A guardian approves (approve=true) or declines (approve=false) a waiting request.
        The speaker's identity and words are checked against the device transcript."""
        try:
            att = attested(ctx)
        except AttestationError as exc:
            return refused(exc)
        return svc.decide(att, action_id, approve)

    # Not read-only: reading a request to a guardian is what authorises their next answer.
    presents = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @mcp.tool(title="What is waiting", annotations=presents)
    def list_waiting(ctx: Context) -> PendingResult:
        """Requests waiting for the speaker: approvals first for guardians, then their own held orders.
        Reads the first one aloud; `presented` is the request the next answer will apply to."""
        try:
            att = attested(ctx)
        except AttestationError as exc:
            return PendingResult(items=[], say=f"I cannot act on this request: {exc}.")
        return svc.pending(att)

    @mcp.tool(title="Household activity", annotations=read_only)
    def household_activity(ctx: Context, days: int = 7) -> ActivityResult:
        """What was bought, stopped or is waiting over the last N days, plus a ledger integrity check.
        Guardians see the whole household; others see their own requests."""
        try:
            att = attested(ctx)
        except AttestationError as exc:
            return ActivityResult(days=days, lines=[], spent=0, ledger_ok=False, ledger_entries=0,
                                  say=f"I cannot act on this request: {exc}.")
        return svc.activity(att, days)

    @mcp.tool(title="Verify the ledger", annotations=read_only)
    def verify_ledger() -> LedgerCheck:
        """Recompute the hash chain over every ledger entry and report the first broken entry, if any."""
        return svc.verify()

    @mcp.tool(title="Check a receipt", annotations=read_only)
    def check_receipt(ledger_seq: int, ledger_hash: str) -> ReceiptCheck:
        """Check a receipt the household kept against the ledger. Detects rewritten history."""
        return svc.check_receipt(ledger_seq, ledger_hash)

    @mcp.resource("countersign://household/rules", mime_type="application/json")
    def household_rules() -> str:
        """The household's spending rules, as configured."""
        hh = svc.hh
        import json

        return json.dumps(
            {
                "household": hh.name,
                "currency": hh.currency,
                "members": [{"id": m.id, "name": m.name, "role": m.role} for m in hh.members.values()],
                "per_action_limit": hh.per_action_limit,
                "weekly_budget": hh.weekly_budget,
                "approval_categories": hh.approval_categories,
                "blocked_categories": hh.blocked_categories,
                "confirm_new_merchant": hh.confirm_new_merchant,
            },
            indent=2,
        )

    mcp._countersign = svc  # type: ignore[attr-defined]  # handle for tests and scripts
    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    host = os.environ.get("COUNTERSIGN_BIND", "127.0.0.1")
    port = int(os.environ.get("COUNTERSIGN_PORT", "8787"))
    build(host=host, port=port).run(transport="streamable-http")


if __name__ == "__main__":
    main()
