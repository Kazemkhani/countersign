import sqlite3

import pytest

from countersign.ledger import Ledger


def make(tmp_path):
    conn = sqlite3.connect(tmp_path / "l.db")
    return conn, Ledger(conn)


def test_chain_verifies(tmp_path):
    _, led = make(tmp_path)
    for i in range(5):
        led.append("proposed", {"i": i, "note": "café"}, f"2026-10-22T09:00:0{i}+00:00")
    r = led.verify()
    assert r.ok and r.entries == 5 and r.head == led.head()[1]


def test_triggers_refuse_update_and_delete(tmp_path):
    conn, led = make(tmp_path)
    led.append("placed", {"total": 6.99}, "2026-10-22T09:00:00+00:00")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE ledger SET payload = '{}' WHERE seq = 1")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM ledger WHERE seq = 1")


def test_edit_is_detected_at_the_edited_entry(tmp_path):
    conn, led = make(tmp_path)
    for i in range(4):
        led.append("placed", {"total": 10 + i}, f"2026-10-22T09:00:0{i}+00:00")
    conn.execute("DROP TRIGGER ledger_no_update")
    conn.execute("""UPDATE ledger SET payload = '{"total":1}' WHERE seq = 3""")
    r = led.verify()
    assert not r.ok and r.first_bad_seq == 3


def test_receipt_catches_full_rewrite(tmp_path):
    conn, led = make(tmp_path)
    first = led.append("placed", {"total": 500}, "2026-10-22T09:00:00+00:00")
    receipt = led.append("placed", {"total": 20}, "2026-10-22T09:01:00+00:00")
    assert led.check_receipt(receipt.seq, receipt.hash)[0]
    # an attacker rewrites entry 1 and recomputes every later hash, so the chain verifies again
    conn.execute("DROP TRIGGER ledger_no_delete")
    conn.execute("DELETE FROM ledger")
    conn.commit()
    led.append("placed", {"total": 5}, first.ts)
    led.append("placed", {"total": 20}, receipt.ts)
    assert led.verify().ok
    ok, detail = led.check_receipt(receipt.seq, receipt.hash)
    assert not ok and "rewritten" in detail
