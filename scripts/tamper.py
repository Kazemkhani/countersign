"""Simulate someone with access to the device's storage editing the purchase record.

Drops the append-only triggers (as an attacker with file access could) and lowers
the total on one 'placed' entry. Then ask the assistant "can I trust that?" or call
verify_ledger: the chain breaks at the edited entry.

    python scripts/tamper.py [path/to/countersign.db]
"""

import json
import sqlite3
import sys
from pathlib import Path

db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "countersign.db"
conn = sqlite3.connect(db)
row = conn.execute("SELECT seq, payload FROM ledger WHERE kind = 'placed' ORDER BY seq LIMIT 1").fetchone()
if row is None:
    sys.exit("no placed orders yet; run the demo first")
seq, payload = row
data = json.loads(payload)
before = data.get("total")
data["total"] = 1.0
conn.execute("DROP TRIGGER IF EXISTS ledger_no_update")
conn.execute("UPDATE ledger SET payload = ? WHERE seq = ?",
             (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False), seq))
conn.commit()
print(f"edited ledger entry {seq}: total {before} -> 1.0")
