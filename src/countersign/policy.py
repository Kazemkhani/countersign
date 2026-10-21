"""Household rules, applied deterministically before anything is bought.

Verdicts, from least to most strict:
  allow           go ahead
  confirm         the same person must say yes in a later turn
  needs_approval  a guardian must approve
  block           refused outright
The strictest rule that fires wins, and every reason is kept for the read-back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any


class Verdict(IntEnum):
    ALLOW = 0
    CONFIRM = 1
    NEEDS_APPROVAL = 2
    BLOCK = 3

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Member:
    id: str
    name: str
    role: str  # guardian | child | protected_adult


@dataclass(frozen=True)
class Household:
    name: str
    currency: str
    members: dict[str, Member]
    per_action_limit: dict[str, float]
    weekly_budget: float
    approval_categories: dict[str, list[str]]
    blocked_categories: dict[str, list[str]]
    confirm_new_merchant: bool
    known_merchants: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: str | Path) -> "Household":
        raw: dict[str, Any] = json.loads(Path(path).read_text())
        rules = raw["rules"]
        return cls(
            name=raw["name"],
            currency=raw.get("currency", "USD"),
            members={m["id"]: Member(m["id"], m["name"], m["role"]) for m in raw["members"]},
            per_action_limit={k: float(v) for k, v in rules["per_action_limit"].items()},
            weekly_budget=float(rules["weekly_budget"]),
            approval_categories=rules.get("approval_categories", {}),
            blocked_categories=rules.get("blocked_categories", {}),
            confirm_new_merchant=bool(rules.get("confirm_new_merchant", True)),
            known_merchants=set(raw.get("known_merchants", [])),
        )

    def money(self, value: float) -> str:
        return f"${value:,.2f}" if self.currency == "USD" else f"{self.currency} {value:,.2f}"

    def guardians(self) -> list[Member]:
        return [m for m in self.members.values() if m.role == "guardian"]


CATEGORY_WORDS = {
    "toys": "toy", "gift_cards": "gift card", "groceries": "grocery", "gifts": "gift",
    "age_restricted": "age-restricted", "money_transfer": "money transfer",
}


def category_word(category: str) -> str:
    return CATEGORY_WORDS.get(category, category.replace("_", " "))


@dataclass
class Decision:
    verdict: Verdict = Verdict.ALLOW
    findings: list[tuple[Verdict, str]] = field(default_factory=list)

    def raise_to(self, verdict: Verdict, reason: str, front: bool = False) -> None:
        if reason not in self.reasons:
            self.findings.insert(0 if front else len(self.findings), (verdict, reason))
        if verdict > self.verdict:
            self.verdict = verdict

    @property
    def reasons(self) -> list[str]:
        return [r for _, r in self.findings]

    def headline(self, limit: int = 2) -> list[str]:
        """The reasons worth reading aloud: strictest first."""
        ordered = sorted(self.findings, key=lambda f: -f[0])
        return [r for _, r in ordered[:limit]]


def evaluate(
    hh: Household,
    member_id: str,
    category: str,
    merchant: str,
    total: float,
    spent_this_week: float,
    merchants_used: set[str],
) -> Decision:
    d = Decision()
    member = hh.members.get(member_id)
    if member is None:
        d.raise_to(Verdict.BLOCK, "speaker is not a member of this household")
        return d
    role = member.role
    money = hh.money

    blocked = hh.blocked_categories.get(role, [])
    if category in blocked or "*" in blocked:
        d.raise_to(Verdict.BLOCK, f"{category_word(category)} purchases are not allowed for {member.name}")

    approval = hh.approval_categories.get(role, [])
    if "*" in approval:
        d.raise_to(Verdict.NEEDS_APPROVAL, f"{member.name} needs a guardian's approval for every purchase")
    elif category in approval:
        d.raise_to(Verdict.NEEDS_APPROVAL, f"{category_word(category)} purchases by {member.name} need a guardian")

    limit = hh.per_action_limit.get(role, 0.0)
    if total > limit:
        if role == "guardian":
            d.raise_to(Verdict.CONFIRM, f"{money(total)} is above the {money(limit)} single-purchase limit")
        elif "*" not in approval:  # "every purchase" already covers it
            d.raise_to(Verdict.NEEDS_APPROVAL, f"{money(total)} is above {member.name}'s {money(limit)} limit")

    if spent_this_week + total > hh.weekly_budget:
        target = Verdict.CONFIRM if role == "guardian" else Verdict.NEEDS_APPROVAL
        d.raise_to(
            target,
            f"it would take this week's spending to {money(spent_this_week + total)}, "
            f"over the {money(hh.weekly_budget)} budget",
        )

    if hh.confirm_new_merchant and merchant not in (hh.known_merchants | merchants_used):
        d.raise_to(Verdict.CONFIRM, f"the household has never bought from {merchant}")

    return d
