"""Countersign core: propose, confirm, approve, report and verify.

Identity and the person's words always come from the host attestation, never
from tool arguments chosen by the assistant's model.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from . import intent as intent_mod
from .attestation import Attestation
from .ledger import Ledger
from .policy import Household, Verdict, evaluate

CONFIRM_WINDOW = timedelta(minutes=10)

AFFIRM = re.compile(
    r"\b(yes|yeah|yep|confirm(ed)?|go ahead|approve[ds]?|do it|place (it|the order)|ok(ay)?|sure|that'?s right|correct)\b",
    re.IGNORECASE,
)
NEGATE = re.compile(r"\b(no|nope|cancel|don'?t|do not|stop|decline|deny|reject|never ?mind)\b", re.IGNORECASE)

ACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    member_id     TEXT NOT NULL,
    product_id    TEXT NOT NULL,
    title         TEXT NOT NULL,
    merchant      TEXT NOT NULL,
    category      TEXT NOT NULL,
    quantity      INTEGER NOT NULL,
    total         REAL NOT NULL,
    status        TEXT NOT NULL,
    reasons       TEXT NOT NULL,
    propose_turn  TEXT NOT NULL,
    order_id      TEXT,
    receipt_seq   INTEGER,
    receipt_hash  TEXT,
    decided_by    TEXT
);
"""


def words_affirm(text: str) -> bool:
    return bool(AFFIRM.search(text)) and not NEGATE.search(text)


def words_negate(text: str) -> bool:
    return bool(NEGATE.search(text))


# ---------- structured outputs (become MCP outputSchema) ----------


class Product(BaseModel):
    id: str
    title: str
    price: float
    merchant: str
    category: str


class SearchResult(BaseModel):
    products: list[Product]
    say: str


class Receipt(BaseModel):
    order_id: str
    ledger_seq: int
    ledger_hash: str


class ActionResult(BaseModel):
    action_id: str | None
    status: str  # placed | needs_confirmation | needs_approval | blocked | declined | refused | error
    say: str
    reasons: list[str] = []
    total: float | None = None
    currency: str = "USD"
    receipt: Receipt | None = None


class PendingItem(BaseModel):
    action_id: str
    requested_by: str
    item: str
    quantity: int
    total: float
    status: str
    reasons: list[str]
    created_at: str


class PendingResult(BaseModel):
    items: list[PendingItem]
    say: str


class ActivityLine(BaseModel):
    when: str
    who: str
    item: str
    total: float
    status: str


class ActivityResult(BaseModel):
    days: int
    lines: list[ActivityLine]
    spent: float
    ledger_ok: bool
    ledger_entries: int
    say: str


class LedgerCheck(BaseModel):
    ok: bool
    entries: int
    head: str
    first_bad_entry: int | None
    reason: str | None
    say: str


class ReceiptCheck(BaseModel):
    ok: bool
    detail: str
    say: str


@dataclass
class _Row:
    id: str
    created_at: str
    member_id: str
    product_id: str
    title: str
    merchant: str
    category: str
    quantity: int
    total: float
    status: str
    reasons: list[str]
    propose_turn: str
    order_id: str | None
    receipt_seq: int | None
    receipt_hash: str | None
    decided_by: str | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Countersign:
    def __init__(
        self,
        db_path: str | Path,
        household: Household,
        catalogue_path: str | Path,
        judge: intent_mod.Judge,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.executescript(ACTIONS_SCHEMA)
        self.ledger = Ledger(self.conn)
        self.hh = household
        self.catalogue = {p["id"]: p for p in json.loads(Path(catalogue_path).read_text())["products"]}
        self.judge = judge
        self.clock = clock
        self.lock = threading.Lock()

    # ---------- helpers ----------

    def _ts(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    def _name(self, member_id: str) -> str:
        m = self.hh.members.get(member_id)
        return m.name if m else member_id

    def _row(self, action_id: str) -> _Row | None:
        r = self.conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        if not r:
            return None
        vals = list(r)
        vals[10] = json.loads(vals[10])
        return _Row(*vals)

    def _set(self, action_id: str, **fields: object) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE actions SET {cols} WHERE id = ?", (*fields.values(), action_id))
        self.conn.commit()

    def _spent_since(self, since: datetime) -> float:
        r = self.conn.execute(
            "SELECT COALESCE(SUM(total), 0) FROM actions WHERE status = 'placed' AND created_at >= ?",
            (since.isoformat(timespec="seconds"),),
        ).fetchone()
        return float(r[0])

    def _merchants_used(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT merchant FROM actions WHERE status = 'placed'")}

    def _place(self, row: _Row, decided_by: str | None) -> Receipt:
        order_id = "SIM-" + secrets.token_hex(4).upper()
        entry = self.ledger.append(
            "placed",
            {"action_id": row.id, "order_id": order_id, "member": row.member_id, "item": row.product_id,
             "quantity": row.quantity, "total": row.total, "merchant": row.merchant, "decided_by": decided_by},
            self._ts(),
        )
        self._set(row.id, status="placed", order_id=order_id, receipt_seq=entry.seq, receipt_hash=entry.hash,
                  decided_by=decided_by)
        return Receipt(order_id=order_id, ledger_seq=entry.seq, ledger_hash=entry.hash)

    def _money(self, v: float) -> str:
        return self.hh.money(v)

    @staticmethod
    def _item(title: str, quantity: int) -> str:
        return title if quantity == 1 else f"{title}, quantity {quantity}"

    # ---------- tools ----------

    def search(self, query: str) -> SearchResult:
        terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 1]
        scored = []
        for p in self.catalogue.values():
            hay = (p["title"] + " " + p["category"]).lower()
            score = sum(1 for t in terms if t in hay)
            if score:
                scored.append((score, p))
        scored.sort(key=lambda sp: (-sp[0], sp[1]["price"]))
        products = [Product(id=p["id"], title=p["title"], price=p["price"], merchant=p["merchant"],
                            category=p["category"]) for _, p in scored[:5]]
        say = (f"I found {len(products)} options." if products else "I could not find that.")
        return SearchResult(products=products, say=say)

    def propose(self, att: Attestation, product_id: str, quantity: int) -> ActionResult:
        with self.lock:
            cur = self.hh.currency
            p = self.catalogue.get(product_id)
            if p is None:
                return ActionResult(action_id=None, status="error", say="I could not find that product.", currency=cur)
            if quantity < 1 or quantity > 99:
                return ActionResult(action_id=None, status="error", say="That quantity is not valid.", currency=cur)
            purchase = intent_mod.Purchase(p["title"], p["description"], p["category"], p["merchant"],
                                           float(p["price"]), int(quantity))
            now = self.clock()
            spent = self._spent_since(now - timedelta(days=7))
            decision = evaluate(self.hh, att.member_id, p["category"], p["merchant"], purchase.total, spent,
                                self._merchants_used())
            check = intent_mod.check(att.utterances, purchase, self.judge)
            if check.injection_suspected:
                decision.raise_to(Verdict.BLOCK, "the product listing tries to give the assistant instructions", front=True)
            if check.verdict == "mismatch":
                # what the person said outranks the household's general limits in the read-back
                for issue in reversed([i for i in check.issues if i["type"] != "injection"]):
                    decision.raise_to(Verdict.CONFIRM, issue["detail"], front=True)
            elif check.verdict == "unclear":
                decision.raise_to(Verdict.CONFIRM, "I could not confirm this matches what was asked", front=True)

            action_id = "A-" + secrets.token_hex(3).upper()
            status = {Verdict.ALLOW: "allowed", Verdict.CONFIRM: "needs_confirmation",
                      Verdict.NEEDS_APPROVAL: "needs_approval", Verdict.BLOCK: "blocked"}[decision.verdict]
            self.conn.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (action_id, now.isoformat(timespec="seconds"), att.member_id, product_id, p["title"], p["merchant"],
                 p["category"], quantity, purchase.total, status, json.dumps(decision.reasons), att.turn_id,
                 None, None, None, None),
            )
            self.conn.commit()
            self.ledger.append(
                "proposed",
                {"action_id": action_id, "member": att.member_id, "turn": att.turn_id, "heard": att.utterances,
                 "item": product_id, "quantity": quantity, "total": purchase.total, "merchant": p["merchant"],
                 "intent": {"verdict": check.verdict, "issues": check.issues, "judge": check.judge},
                 "verdict": decision.verdict.label, "reasons": decision.reasons},
                self._ts(),
            )
            row = self._row(action_id)
            assert row is not None
            item = self._item(p["title"], quantity)
            if decision.verdict == Verdict.ALLOW:
                receipt = self._place(row, decided_by=None)
                return ActionResult(action_id=action_id, status="placed", reasons=decision.reasons,
                                    total=purchase.total, currency=cur, receipt=receipt,
                                    say=f"Done. I ordered {item} for {self._money(purchase.total)}.")
            why = "; ".join(decision.headline())
            if decision.verdict == Verdict.CONFIRM:
                return ActionResult(action_id=action_id, status="needs_confirmation", reasons=decision.reasons,
                                    total=purchase.total, currency=cur,
                                    say=f"Before I order {item} for {self._money(purchase.total)}: {why}. "
                                        f"Should I go ahead?")
            if decision.verdict == Verdict.NEEDS_APPROVAL:
                guardians = " or ".join(g.name for g in self.hh.guardians())
                self._set(action_id, status="needs_approval")
                return ActionResult(action_id=action_id, status="needs_approval", reasons=decision.reasons,
                                    total=purchase.total, currency=cur,
                                    say=f"I have asked {guardians} to approve {item}, because {why}.")
            self.ledger.append("blocked", {"action_id": action_id, "reasons": decision.reasons}, self._ts())
            return ActionResult(action_id=action_id, status="blocked", reasons=decision.reasons, total=purchase.total,
                                currency=cur, say=f"I did not order that, because {why}.")

    def confirm(self, att: Attestation, action_id: str) -> ActionResult:
        with self.lock:
            row = self._row(action_id)
            if row is None or row.status != "needs_confirmation":
                return ActionResult(action_id=action_id, status="error", say="There is nothing to confirm with that id.")
            if att.member_id != row.member_id:
                return ActionResult(action_id=action_id, status="refused",
                                    say=f"Only {self._name(row.member_id)} can confirm this.")
            if att.turn_id == row.propose_turn:
                return ActionResult(action_id=action_id, status="refused",
                                    say="I need to hear you say yes before I place this.")
            if self.clock() - datetime.fromisoformat(row.created_at) > CONFIRM_WINDOW:
                self._set(action_id, status="expired")
                self.ledger.append("expired", {"action_id": action_id}, self._ts())
                return ActionResult(action_id=action_id, status="declined", say="That request expired, so I dropped it.")
            if words_negate(att.latest):
                self._set(action_id, status="declined", decided_by=att.member_id)
                self.ledger.append("declined", {"action_id": action_id, "by": att.member_id, "heard": att.utterances[-1:]},
                                   self._ts())
                return ActionResult(action_id=action_id, status="declined", say="Okay, I cancelled it.")
            if not words_affirm(att.latest):
                return ActionResult(action_id=action_id, status="refused",
                                    say="I did not hear a clear yes, so I have not placed it.")
            self.ledger.append("confirmed", {"action_id": action_id, "by": att.member_id, "heard": att.utterances[-1:]},
                               self._ts())
            receipt = self._place(row, decided_by=att.member_id)
            return ActionResult(action_id=action_id, status="placed", total=row.total, currency=self.hh.currency,
                                receipt=receipt, say=f"Done. Order {receipt.order_id} is placed.")

    def decide(self, att: Attestation, action_id: str, approve: bool) -> ActionResult:
        with self.lock:
            row = self._row(action_id)
            if row is None or row.status != "needs_approval":
                return ActionResult(action_id=action_id, status="error", say="There is nothing waiting with that id.")
            approver = self.hh.members.get(att.member_id)
            if approver is None or approver.role != "guardian":
                self.ledger.append("approval_refused",
                                   {"action_id": action_id, "speaker": att.member_id, "heard": att.utterances[-1:]},
                                   self._ts())
                return ActionResult(action_id=action_id, status="refused",
                                    say=f"Only a guardian can decide this, and I recognised {self._name(att.member_id)}.")
            heard_yes, heard_no = words_affirm(att.latest), words_negate(att.latest)
            if approve and not heard_yes:
                return ActionResult(action_id=action_id, status="refused",
                                    say="I did not hear you approve it, so it is still waiting.")
            if not approve and not heard_no:
                return ActionResult(action_id=action_id, status="refused",
                                    say="I did not hear you decline it, so it is still waiting.")
            if not approve:
                self._set(action_id, status="declined", decided_by=att.member_id)
                self.ledger.append("declined", {"action_id": action_id, "by": att.member_id, "heard": att.utterances[-1:]},
                                   self._ts())
                return ActionResult(action_id=action_id, status="declined",
                                    say=f"Declined. I will let {self._name(row.member_id)} know.")
            self.ledger.append("approved", {"action_id": action_id, "by": att.member_id, "heard": att.utterances[-1:]},
                               self._ts())
            receipt = self._place(row, decided_by=att.member_id)
            return ActionResult(action_id=action_id, status="placed", total=row.total, currency=self.hh.currency,
                                receipt=receipt,
                                say=f"Approved. I ordered {row.title} for {self._name(row.member_id)}.")

    def pending(self, att: Attestation) -> PendingResult:
        me = self.hh.members.get(att.member_id)
        if me is None:
            return PendingResult(items=[], say="I do not recognise you in this household.")
        rows = [self._row(r[0]) for r in self.conn.execute(
            "SELECT id FROM actions WHERE status IN ('needs_approval','needs_confirmation') ORDER BY created_at")]
        visible = [r for r in rows if r and (r.member_id == me.id or (me.role == "guardian" and r.status == "needs_approval"))]
        items = [PendingItem(action_id=r.id, requested_by=self._name(r.member_id), item=r.title, quantity=r.quantity,
                             total=r.total, status=r.status, reasons=r.reasons, created_at=r.created_at) for r in visible]
        if not items:
            return PendingResult(items=[], say="Nothing is waiting for you.")
        first = items[0]
        say = (f"{len(items)} waiting. First: {first.requested_by} asked for "
               f"{self._item(first.item, first.quantity)}, {self._money(first.total)}. "
               f"Reason: {'; '.join(first.reasons[:2])}.")
        return PendingResult(items=items, say=say)

    def activity(self, att: Attestation, days: int = 7) -> ActivityResult:
        me = self.hh.members.get(att.member_id)
        days = max(1, min(days, 90))
        since = (self.clock() - timedelta(days=days)).isoformat(timespec="seconds")
        q = "SELECT id FROM actions WHERE created_at >= ? ORDER BY created_at"
        rows = [self._row(r[0]) for r in self.conn.execute(q, (since,))]
        if me is None:
            rows = []
        elif me.role != "guardian":
            rows = [r for r in rows if r and r.member_id == me.id]
        lines = [ActivityLine(when=r.created_at, who=self._name(r.member_id), item=r.title, total=r.total,
                              status=r.status) for r in rows if r]
        spent = round(sum(line.total for line in lines if line.status == "placed"), 2)
        check = self.ledger.verify()
        placed = sum(1 for line in lines if line.status == "placed")
        stopped = sum(1 for line in lines if line.status in ("blocked", "declined"))
        waiting = sum(1 for line in lines if line.status.startswith("needs"))
        integrity = ("The record checks out." if check.ok
                     else f"Warning: the record has been altered at entry {check.first_bad_seq}.")
        say = (f"In the last {days} days: {placed} placed for {self._money(spent)}, {stopped} stopped, "
               f"{waiting} waiting. {integrity}")
        return ActivityResult(days=days, lines=lines, spent=spent, ledger_ok=check.ok, ledger_entries=check.entries,
                              say=say)

    def verify(self) -> LedgerCheck:
        r = self.ledger.verify()
        say = (f"All {r.entries} entries check out." if r.ok
               else f"The record was altered: entry {r.first_bad_seq} fails, {r.reason}.")
        return LedgerCheck(ok=r.ok, entries=r.entries, head=r.head, first_bad_entry=r.first_bad_seq, reason=r.reason,
                           say=say)

    def check_receipt(self, seq: int, ledger_hash: str) -> ReceiptCheck:
        ok, detail = self.ledger.check_receipt(seq, ledger_hash)
        return ReceiptCheck(ok=ok, detail=detail, say=("That receipt matches the record." if ok else detail))
