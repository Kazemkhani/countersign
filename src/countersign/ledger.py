"""Append-only, hash-chained ledger in SQLite.

Every entry stores the hash of the entry before it, so editing, deleting or
reordering any past entry breaks the chain from that point on. SQLite triggers
refuse UPDATE and DELETE as a first line; the chain is what catches someone who
drops the triggers or edits the file directly.

Limit: a chain alone cannot catch a full rewrite in which every later hash is
recomputed. Receipts close most of that gap: a receipt carries the hash of its
entry, which commits to every entry before it, and the household keeps receipts
outside the server, so a rewritten history no longer matches receipts already
handed out.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq        INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
"""


def canonical(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 kept."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(prev_hash: str, seq: int, ts: str, kind: str, payload: str) -> str:
    material = canonical({"prev": prev_hash, "seq": seq, "ts": ts, "kind": kind, "payload": payload})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Entry:
    seq: int
    ts: str
    kind: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    entries: int
    head: str
    first_bad_seq: int | None = None
    reason: str | None = None


class Ledger:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.executescript(SCHEMA)

    def head(self) -> tuple[int, str]:
        row = self.conn.execute("SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else (0, GENESIS)

    def append(self, kind: str, payload: dict[str, Any], ts: str) -> Entry:
        last_seq, last_hash = self.head()
        seq = last_seq + 1
        body = canonical(payload)
        digest = entry_hash(last_hash, seq, ts, kind, body)
        self.conn.execute(
            "INSERT INTO ledger (seq, ts, kind, payload, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?)",
            (seq, ts, kind, body, last_hash, digest),
        )
        self.conn.commit()
        return Entry(seq, ts, kind, payload, last_hash, digest)

    def entries(self, since_ts: str | None = None) -> list[Entry]:
        sql = "SELECT seq, ts, kind, payload, prev_hash, hash FROM ledger"
        args: tuple[Any, ...] = ()
        if since_ts:
            sql += " WHERE ts >= ?"
            args = (since_ts,)
        rows = self.conn.execute(sql + " ORDER BY seq", args).fetchall()
        return [Entry(r[0], r[1], r[2], json.loads(r[3]), r[4], r[5]) for r in rows]

    def verify(self) -> VerifyResult:
        rows = self.conn.execute(
            "SELECT seq, ts, kind, payload, prev_hash, hash FROM ledger ORDER BY seq"
        ).fetchall()
        prev = GENESIS
        expected_seq = 1
        for seq, ts, kind, payload, prev_hash, digest in rows:
            if seq != expected_seq:
                return VerifyResult(False, len(rows), prev, expected_seq, "an entry is missing or out of order")
            if prev_hash != prev:
                return VerifyResult(False, len(rows), prev, seq, "link to the previous entry is broken")
            if entry_hash(prev_hash, seq, ts, kind, payload) != digest:
                return VerifyResult(False, len(rows), prev, seq, "entry content does not match its hash")
            prev = digest
            expected_seq += 1
        return VerifyResult(True, len(rows), prev)

    def check_receipt(self, seq: int, entry_hash_hex: str) -> tuple[bool, str]:
        """Valid if the chain verifies and entry `seq` still has the hash printed on
        the receipt. Because each hash covers the previous one, any rewrite of
        history up to that entry changes it."""
        result = self.verify()
        if not result.ok:
            return False, f"ledger fails verification at entry {result.first_bad_seq}: {result.reason}"
        row = self.conn.execute("SELECT hash FROM ledger WHERE seq = ?", (seq,)).fetchone()
        if row is None:
            return False, f"entry {seq} is not in the ledger"
        if row[0] != entry_hash_hex:
            return False, f"entry {seq} no longer matches the receipt, so history up to it was rewritten"
        return True, "receipt matches the ledger"
