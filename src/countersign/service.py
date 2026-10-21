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
APPROVAL_WINDOW = timedelta(hours=24)

YES_START = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok|okay|confirm(ed)?|(i )?approve[ds]?( it)?|go ahead|do it|please do|"
    r"place (it|the order)|that'?s (right|fine|correct))\b"
)
NO_ANY = re.compile(r"\b(no|nope|nah|cancel|decline[ds]?|don'?t|do not|stop|reject(ed)?|deny|denied|never ?mind)\b")
UNSURE = re.compile(
    r"\b(not|n't|unsure|maybe|perhaps|wait|hold on|hang on|let me|think|what|why|how|when|who|which|where)\b|\?"
)


def classify_answer(text: str) -> str:
    """'yes', 'no' or 'unclear'. Deliberately strict: a yes must lead the answer, be
    short and carry no doubt or question. Anything else is unclear, which leaves
    the request waiting rather than placing it."""
    t = re.sub(r"[^a-z0-9'?\s]", " ", text.lower()).strip()
    t = re.sub(r"^(um+|uh+|hmm+|well|so|right)\s+", "", t)
    if NO_ANY.search(t):
        return "no"
    if UNSURE.search(t):
        return "unclear"
    if YES_START.match(t) and len(t.split()) <= 8:
        return "yes"
    return "unclear"

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
-- one spoken answer settles at most one request
CREATE TABLE IF NOT EXISTS used_turns (
    turn_id    TEXT PRIMARY KEY,
    member_id  TEXT NOT NULL,
    action_id  TEXT NOT NULL
);
-- the request most recently read aloud to each guardian
CREATE TABLE IF NOT EXISTS presented (
    member_id  TEXT PRIMARY KEY,
    action_id  TEXT NOT NULL,
    turn_id    TEXT NOT NULL
);
"""




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
    presented: str | None = None  # the request just read aloud to a guardian, if any
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
    ledger_head: str = ""
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
        screen: intent_mod.GuardrailScreen | None = None,
    ):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.executescript(ACTIONS_SCHEMA)
        self.ledger = Ledger(self.conn)
        self.hh = household
        self.catalogue = {p["id"]: p for p in json.loads(Path(catalogue_path).read_text())["products"]}
        self.judge = judge
        self.screen = screen
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

    def _turn_used(self, turn_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM used_turns WHERE turn_id = ?", (turn_id,)).fetchone() is not None

    def _use_turn(self, att: Attestation, action_id: str) -> None:
        self.conn.execute("INSERT INTO used_turns VALUES (?, ?, ?)", (att.turn_id, att.member_id, action_id))
        self.conn.commit()

    _PROPOSED_STATUS = {"allow": "allowed", "confirm": "needs_confirmation",
                        "needs_approval": "needs_approval", "block": "blocked"}

    def _history(self) -> dict[str, dict]:
        """Every request and its current state, rebuilt from the hash-chained ledger
        rather than the editable actions table, so reports and budgets rest on the
        record that verify_ledger checks."""
        acts: dict[str, dict] = {}
        for e in self.ledger.entries():
            aid = e.payload.get("action_id")
            if not aid:
                continue
            if e.kind == "proposed":
                acts[aid] = {"when": e.ts, "member": e.payload["member"], "item": e.payload["item"],
                             "quantity": e.payload["quantity"], "total": e.payload["total"],
                             "merchant": e.payload["merchant"],
                             "status": self._PROPOSED_STATUS.get(e.payload["verdict"], "unknown")}
            elif aid in acts and e.kind in ("placed", "blocked", "declined", "expired"):
                acts[aid]["status"] = e.kind
                if e.kind == "placed":
                    acts[aid]["placed_at"] = e.ts
        return acts

    def _spent_since(self, since: datetime) -> float:
        cutoff = since.isoformat(timespec="seconds")
        return round(sum(a["total"] for a in self._history().values()
                         if a["status"] == "placed" and a.get("placed_at", "") >= cutoff), 2)

    def _merchants_used(self) -> set[str]:
        return {a["merchant"] for a in self._history().values() if a["status"] == "placed"}

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
            check = intent_mod.check(att.utterances, purchase, self.judge, self.screen)
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
            if att.answering != action_id:
                return ActionResult(action_id=action_id, status="refused",
                                    say="That answer was not to this question, so I have not placed it.")
            if self._turn_used(att.turn_id):
                return ActionResult(action_id=action_id, status="refused",
                                    say="One answer settles one request. I have not placed this one.")
            if self.clock() - datetime.fromisoformat(row.created_at) > CONFIRM_WINDOW:
                self._set(action_id, status="expired")
                self.ledger.append("expired", {"action_id": action_id}, self._ts())
                return ActionResult(action_id=action_id, status="declined", say="That request expired, so I dropped it.")
            answer = classify_answer(att.latest)
            if answer == "unclear":
                return ActionResult(action_id=action_id, status="refused",
                                    say="I did not hear a clear yes or no, so I have not placed it.")
            self._use_turn(att, action_id)
            if answer == "no":
                self._set(action_id, status="declined", decided_by=att.member_id)
                self.ledger.append("declined", {"action_id": action_id, "by": att.member_id, "heard": att.utterances[-1:]},
                                   self._ts())
                return ActionResult(action_id=action_id, status="declined", say="Okay, I cancelled it.")
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
            if self.clock() - datetime.fromisoformat(row.created_at) > APPROVAL_WINDOW:
                self._set(action_id, status="expired")
                self.ledger.append("expired", {"action_id": action_id}, self._ts())
                return ActionResult(action_id=action_id, status="declined",
                                    say="That request is more than a day old, so I dropped it. They can ask again.")
            shown = self.conn.execute("SELECT action_id, turn_id FROM presented WHERE member_id = ?",
                                      (att.member_id,)).fetchone()
            if shown is None or shown[0] != action_id or shown[1] == att.turn_id or att.answering != action_id:
                return ActionResult(action_id=action_id, status="refused",
                                    say="Ask me what is waiting first, so I can read you the request before you decide.")
            if self._turn_used(att.turn_id):
                return ActionResult(action_id=action_id, status="refused",
                                    say="One answer settles one request. This one is still waiting.")
            answer = classify_answer(att.latest)
            if answer != ("yes" if approve else "no"):
                return ActionResult(action_id=action_id, status="refused",
                                    say="Your answer did not match that decision, so the request is still waiting.")
            self._use_turn(att, action_id)
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
            "SELECT id FROM actions WHERE status IN ('needs_approval','needs_confirmation') ORDER BY created_at, rowid")]
        visible = [r for r in rows if r and (r.member_id == me.id or (me.role == "guardian" and r.status == "needs_approval"))]
        visible.sort(key=lambda r: r.status != "needs_approval")  # approvals first: that is what a guardian is asked
        items = [PendingItem(action_id=r.id, requested_by=self._name(r.member_id), item=r.title, quantity=r.quantity,
                             total=r.total, status=r.status, reasons=r.reasons, created_at=r.created_at) for r in visible]
        if not items:
            return PendingResult(items=[], say="Nothing is waiting for you.")
        first = items[0]
        presented, question = None, ""
        if me.role == "guardian" and first.status == "needs_approval":
            self.conn.execute("INSERT OR REPLACE INTO presented VALUES (?, ?, ?)", (me.id, first.action_id, att.turn_id))
            self.conn.commit()
            presented, question = first.action_id, " Do you approve?"
        elif first.status == "needs_confirmation" and visible[0].member_id == me.id:
            presented, question = first.action_id, " Should I go ahead?"
        say = (f"{len(items)} waiting. First: {first.requested_by} asked for "
               f"{self._item(first.item, first.quantity)}, {self._money(first.total)}. "
               f"Reason: {'; '.join(first.reasons[:2])}.{question}")
        return PendingResult(items=items, presented=presented, say=say)

    def activity(self, att: Attestation, days: int = 7) -> ActivityResult:
        me = self.hh.members.get(att.member_id)
        days = max(1, min(days, 90))
        since = (self.clock() - timedelta(days=days)).isoformat(timespec="seconds")
        acts = [a for a in self._history().values() if a["when"] >= since]
        if me is None:
            acts = []
        elif me.role != "guardian":
            acts = [a for a in acts if a["member"] == me.id]
        lines = [ActivityLine(when=a["when"], who=self._name(a["member"]),
                              item=self.catalogue.get(a["item"], {}).get("title", a["item"]),
                              total=a["total"], status=a["status"]) for a in acts]
        spent = round(sum(line.total for line in lines if line.status == "placed"), 2)
        check = self.ledger.verify()
        placed = sum(1 for line in lines if line.status == "placed")
        stopped = sum(1 for line in lines if line.status in ("blocked", "declined", "expired"))
        waiting = sum(1 for line in lines if line.status.startswith("needs"))
        integrity = ("The record checks out." if check.ok
                     else f"Warning: the record has been altered at entry {check.first_bad_seq}.")
        say = (f"In the last {days} days: {placed} placed for {self._money(spent)}, {stopped} stopped, "
               f"{waiting} waiting. {integrity}")
        return ActivityResult(days=days, lines=lines, spent=spent, ledger_ok=check.ok, ledger_entries=check.entries,
                              ledger_head=check.head, say=say)

    def verify(self) -> LedgerCheck:
        r = self.ledger.verify()
        say = (f"All {r.entries} entries check out." if r.ok
               else f"The record was altered: entry {r.first_bad_seq} fails, {r.reason}.")
        return LedgerCheck(ok=r.ok, entries=r.entries, head=r.head, first_bad_entry=r.first_bad_seq, reason=r.reason,
                           say=say)

    def check_receipt(self, seq: int, ledger_hash: str) -> ReceiptCheck:
        ok, detail = self.ledger.check_receipt(seq, ledger_hash)
        return ReceiptCheck(ok=ok, detail=detail, say=("That receipt matches the record." if ok else detail))
