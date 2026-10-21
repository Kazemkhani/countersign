from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from countersign import intent
from countersign.attestation import Attestation
from countersign.policy import Household
from countersign.service import Countersign

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class ScriptedJudge:
    """Stands in for the Bedrock judge in tests: returns whatever verdict it is told to."""

    name = "scripted"

    def __init__(self) -> None:
        self.verdict = "match"
        self.issues: list[dict[str, str]] = []

    def judge(self, utterances, p):
        return intent.IntentResult(verdict=self.verdict, issues=list(self.issues), judge=self.name,
                                   injection_suspected=any(i["type"] == "injection" for i in self.issues))


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def judge() -> ScriptedJudge:
    return ScriptedJudge()


@pytest.fixture
def household() -> Household:
    return Household.load(ROOT / "data" / "household.json")


@pytest.fixture
def svc(tmp_path, household, judge, clock) -> Countersign:
    return Countersign(tmp_path / "t.db", household, ROOT / "data" / "catalogue.json", judge, clock)


_turn = iter(range(1, 10_000))


def say(member: str, *utterances: str) -> Attestation:
    return Attestation(member_id=member, turn_id=f"t{next(_turn)}", utterances=list(utterances), issued_at=time.time())
