"""Measure the intent check on labelled cases, with exact intervals.

    python eval/run_eval.py --split dev              # iterate on the judge prompt here
    python eval/run_eval.py --split test --judge bedrock   # run ONCE, after freezing the prompt

"Flagged" means the check returned mismatch or unclear, because both make
Countersign stop and ask. We report:
  catch rate   = mismatches flagged / all mismatches     (sensitivity)
  pass rate    = matches not flagged / all matches        (specificity)
each with a two-sided 95% Clopper-Pearson interval. The cases are constructed,
not real household data, and the samples are small, so the intervals are wide.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from countersign import intent  # noqa: E402
from countersign.stats import clopper_pearson  # noqa: E402


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"], required=True)
    ap.add_argument("--judge", choices=["rules", "bedrock"], default="rules")
    ap.add_argument("--model", default=None, help="Bedrock model or inference profile id")
    args = ap.parse_args()

    cases = [json.loads(line) for line in (ROOT / "eval" / "cases.jsonl").read_text().splitlines() if line.strip()]
    cases = [c for c in cases if c["split"] == args.split]
    judge: intent.Judge = intent.RulesOnlyJudge() if args.judge == "rules" else intent.BedrockJudge(model_id=args.model)

    rows = []
    for c in cases:
        p = c["product"]
        purchase = intent.Purchase(p["title"], p["description"], p["category"], p["merchant"], p["unit_price"], c["quantity"])
        t0 = time.time()
        res = intent.check(c["said"], purchase, judge)
        rows.append({"id": c["id"], "label": c["label"], "type": c["type"], "verdict": res.verdict,
                     "flagged": res.verdict in ("mismatch", "unclear"), "issues": res.issues,
                     "seconds": round(time.time() - t0, 2)})

    mism = [r for r in rows if r["label"] == "mismatch"]
    match = [r for r in rows if r["label"] == "match"]
    caught = sum(r["flagged"] for r in mism)
    passed = sum(not r["flagged"] for r in match)
    unclear = sum(r["verdict"] == "unclear" for r in rows)
    c_lo, c_hi = clopper_pearson(caught, len(mism))
    p_lo, p_hi = clopper_pearson(passed, len(match))

    print(f"split={args.split} judge={judge.name} cases={len(rows)}")
    print(f"catch rate {caught}/{len(mism)} = {pct(caught/len(mism))}  (95% interval {pct(c_lo)} to {pct(c_hi)})")
    print(f"pass rate  {passed}/{len(match)} = {pct(passed/len(match))}  (95% interval {pct(p_lo)} to {pct(p_hi)})")
    print(f"unclear verdicts: {unclear}")
    for r in rows:
        wrong = r["flagged"] != (r["label"] == "mismatch")
        if wrong:
            print(f"  WRONG {r['id']} label={r['label']}/{r['type']} verdict={r['verdict']} issues={r['issues']}")

    out = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "split": args.split,
        "judge": judge.name,
        "judge_prompt_sha256": hashlib.sha256(intent.JUDGE_SYSTEM.encode()).hexdigest(),
        "cases_sha256": hashlib.sha256((ROOT / "eval" / "cases.jsonl").read_bytes()).hexdigest(),
        "catch": {"k": caught, "n": len(mism), "ci95": [c_lo, c_hi]},
        "pass": {"k": passed, "n": len(match), "ci95": [p_lo, p_hi]},
        "unclear": unclear,
        "rows": rows,
    }
    dest = ROOT / "eval" / "results" / f"{args.split}-{args.judge}-{int(time.time())}.json"
    dest.write_text(json.dumps(out, indent=2))
    print(f"saved {dest.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
