"""Host attestation: who is speaking and what they actually said.

The assistant's language model never tells Countersign who the speaker is or
what the speaker asked for. The host (the voice front end that ran speaker
recognition and speech-to-text) signs those facts with a key it shares with
Countersign and sends them as HTTP headers on every MCP request. A tool call
whose arguments disagree with the attested facts loses.

Headers:
  X-Countersign-Attestation: base64url(JSON {member_id, turn_id, utterances, issued_at, answering})
  X-Countersign-Signature:   hex HMAC-SHA256 of the raw base64url string

`answering` is the id of the request whose question the device last read aloud
to this person (from Countersign's own `say` text), or null. A yes only counts
for that request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field

HEADER_ATTESTATION = "x-countersign-attestation"
HEADER_SIGNATURE = "x-countersign-signature"
MAX_AGE_SECONDS = 300


class AttestationError(Exception):
    pass


@dataclass(frozen=True)
class Attestation:
    member_id: str
    turn_id: str
    utterances: list[str] = field(default_factory=list)  # most recent last
    issued_at: float = 0.0
    answering: str | None = None

    @property
    def latest(self) -> str:
        return self.utterances[-1] if self.utterances else ""


def sign(att: Attestation, key: bytes) -> dict[str, str]:
    body = json.dumps(
        {"member_id": att.member_id, "turn_id": att.turn_id, "utterances": att.utterances,
         "issued_at": att.issued_at, "answering": att.answering},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    token = base64.urlsafe_b64encode(body).decode("ascii")
    sig = hmac.new(key, token.encode("ascii"), hashlib.sha256).hexdigest()
    return {HEADER_ATTESTATION: token, HEADER_SIGNATURE: sig}


def verify(headers: dict[str, str], key: bytes, now: float | None = None) -> Attestation:
    lower = {k.lower(): v for k, v in headers.items()}
    token = lower.get(HEADER_ATTESTATION)
    sig = lower.get(HEADER_SIGNATURE)
    if not token or not sig:
        raise AttestationError("no host attestation on this request")
    expected = hmac.new(key, token.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise AttestationError("host attestation signature is invalid")
    try:
        data = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AttestationError("host attestation is malformed") from exc
    issued = float(data.get("issued_at", 0))
    if abs((now if now is not None else time.time()) - issued) > MAX_AGE_SECONDS:
        raise AttestationError("host attestation has expired")
    utterances = [str(u) for u in data.get("utterances", [])][-5:]
    answering = data.get("answering")
    return Attestation(str(data["member_id"]), str(data["turn_id"]), utterances, issued,
                       str(answering) if answering else None)
