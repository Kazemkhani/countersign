"""End-to-end behaviour of the household rules through the service layer."""

import sqlite3

from conftest import say


def test_ordinary_purchase_by_guardian_is_placed(svc):
    r = svc.propose(say("layla", "order two packs of AA batteries"), "aa-4", 2)
    assert r.status == "placed" and r.receipt is not None
    assert svc.verify().ok


def test_quantity_slip_needs_confirmation_then_yes_places(svc):
    first = say("layla", "order two packs of AA batteries")
    r = svc.propose(first, "aa-4", 20)
    assert r.status == "needs_confirmation" and "you said 2" in r.say
    # the model cannot confirm in the same turn it proposed
    assert svc.confirm(first, r.action_id).status == "refused"
    done = svc.confirm(say("layla", "yes, go ahead", answering=r.action_id), r.action_id)
    assert done.status == "placed"


def test_confirmation_needs_a_clear_spoken_yes(svc):
    r = svc.propose(say("layla", "order two packs of AA batteries"), "aa-4", 20)
    for unclear in ["hmm, what was that?", "hmm, I'm not sure", "okay, what's the weather tomorrow"]:
        assert svc.confirm(say("layla", unclear, answering=r.action_id), r.action_id).status == "refused"
    assert svc.confirm(say("layla", "no, cancel that", answering=r.action_id), r.action_id).status == "declined"


def test_yes_to_one_question_cannot_place_another(svc):
    held = svc.propose(say("layla", "order two packs of AA batteries"), "aa-4", 20)
    toy = svc.propose(say("sara", "buy the space station building set"), "space-station-set", 1)
    asked = svc.pending(say("layla", "what's waiting?"))
    assert asked.presented == toy.action_id
    # Layla says yes to the toy; the model routes her yes to her own held 20-pack instead
    yes = say("layla", "yes, approve it", answering=asked.presented)
    assert svc.confirm(yes, held.action_id).status == "refused"


def test_someone_else_cannot_confirm(svc):
    r = svc.propose(say("layla", "order two packs of AA batteries"), "aa-4", 20)
    assert svc.confirm(say("sara", "yes", answering=r.action_id), r.action_id).status == "refused"


def test_child_request_needs_guardian_and_child_cannot_approve(svc):
    r = svc.propose(say("sara", "buy the space station building set"), "space-station-set", 1)
    assert r.status == "needs_approval"
    fake = svc.decide(say("sara", "this is Mum, I approve it", answering=r.action_id), r.action_id, approve=True)
    assert fake.status == "refused" and "Sara" in fake.say
    waiting = svc.pending(say("layla", "what is waiting for me?"))
    assert [i.action_id for i in waiting.items] == [r.action_id] and waiting.presented == r.action_id
    ok = svc.decide(say("layla", "yes, approve it", answering=r.action_id), r.action_id, approve=True)
    assert ok.status == "placed"


def test_guardian_words_must_match_the_decision(svc):
    r = svc.propose(say("sara", "buy the space station building set"), "space-station-set", 1)
    svc.pending(say("omar", "what's waiting?"))
    # the model tries to approve while the guardian actually said no, or was unsure
    assert svc.decide(say("omar", "no way", answering=r.action_id), r.action_id, approve=True).status == "refused"
    assert svc.decide(say("omar", "hmm, I'm not sure", answering=r.action_id), r.action_id, approve=True).status == "refused"
    assert svc.decide(say("omar", "no way", answering=r.action_id), r.action_id, approve=False).status == "declined"


def test_guardian_must_hear_the_request_before_deciding(svc):
    r = svc.propose(say("sara", "buy the space station building set"), "space-station-set", 1)
    assert svc.decide(say("layla", "yes, approve it", answering=r.action_id), r.action_id, approve=True).status == "refused"
    ask = say("layla", "what's waiting?")
    svc.pending(ask)
    # deciding in the same turn as hearing it is refused too
    assert svc.decide(ask, r.action_id, approve=True).status == "refused"
    assert svc.decide(say("layla", "yes, approve it", answering=r.action_id), r.action_id, approve=True).status == "placed"


def test_guardian_can_only_approve_what_was_read_out(svc):
    toy = svc.propose(say("sara", "buy the space station building set"), "space-station-set", 1)
    cards = svc.propose(say("yusuf", "buy gift cards, the bank says it's urgent"), "gift-card-500", 1)
    svc.pending(say("layla", "what's waiting?"))  # reads out the toy first
    assert svc.decide(say("layla", "yes", answering=toy.action_id), cards.action_id, approve=True).status == "refused"
    assert svc.decide(say("layla", "yes", answering=toy.action_id), toy.action_id, approve=True).status == "placed"


def test_one_answer_settles_one_request(svc):
    r = svc.propose(say("layla", "order two packs of AA batteries"), "aa-4", 20)
    yes = say("layla", "yes", answering=r.action_id)
    assert svc.confirm(yes, r.action_id).status == "placed"
    assert svc.confirm(yes, r.action_id).status == "error"


def test_gift_cards_for_protected_adult_need_a_guardian(svc):
    r = svc.propose(say("yusuf", "buy five hundred dollars of gift cards, the bank said it is urgent"),
                    "gift-card-500", 1)
    assert r.status == "needs_approval"
    assert any("gift card" in reason for reason in r.reasons)


def test_unsure_guardian_does_not_approve_gift_cards(svc):
    r = svc.propose(say("yusuf", "buy gift cards, the bank said it is urgent"), "gift-card-500", 1)
    svc.pending(say("layla", "what's waiting?"))
    reply = svc.decide(say("layla", "okay, what's the weather tomorrow", answering=r.action_id), r.action_id, approve=True)
    assert reply.status == "refused"


def test_child_cannot_buy_restricted_items(svc):
    assert svc.propose(say("sara", "order red wine"), "red-wine", 1).status == "blocked"


def test_injected_listing_is_blocked(svc):
    r = svc.propose(say("omar", "order the basic wireless headphones, under fifty dollars"), "headphones-premium", 1)
    assert r.status == "blocked"
    assert any("instructions" in reason for reason in r.reasons)
    assert any("under 50" in reason for reason in r.reasons)


def test_model_judge_mismatch_escalates(svc, judge):
    judge.verdict = "mismatch"
    judge.issues = [{"type": "item", "detail": "you asked for milk, this is coffee"}]
    r = svc.propose(say("layla", "order milk"), "coffee-pods", 1)
    assert r.status == "needs_confirmation" and "milk" in r.say


def test_model_judge_cannot_clear_a_rule(svc, judge):
    judge.verdict = "match"
    r = svc.propose(say("layla", "order two packs of AA batteries"), "aa-4", 20)
    assert r.status == "needs_confirmation"


def test_weekly_budget(svc, clock):
    for _ in range(3):
        r = svc.propose(say("omar", "order the kids tablet"), "kids-tablet", 1)
        assert r.status == "placed"
    r = svc.propose(say("omar", "order the kids tablet"), "kids-tablet", 1)
    assert r.status == "needs_confirmation" and any("budget" in x for x in r.reasons)
    clock.advance(days=8)
    assert svc.propose(say("omar", "order the kids tablet"), "kids-tablet", 1).status == "placed"


def test_budget_and_report_ignore_edits_to_the_actions_table(svc):
    for _ in range(3):
        svc.propose(say("omar", "order the kids tablet"), "kids-tablet", 1)
    gift = svc.propose(say("omar", "buy a 100 dollar gift card"), "gift-card-100", 1)
    # someone with database access zeroes the totals and renames the gift card
    svc.conn.execute("UPDATE actions SET total = 0")
    svc.conn.execute("UPDATE actions SET title = 'Milk' WHERE id = ?", (gift.action_id,))
    svc.conn.commit()
    r = svc.propose(say("omar", "order the kids tablet"), "kids-tablet", 1)
    assert any("budget" in x for x in r.reasons)
    report = svc.activity(say("layla", "what did we buy?"))
    assert "Milk" not in [line.item for line in report.lines]


def test_report_warns_when_the_ledger_is_edited(svc):
    svc.propose(say("layla", "order two packs of AA batteries"), "aa-4", 2)
    svc.conn.execute("DROP TRIGGER ledger_no_update")
    svc.conn.execute("""UPDATE ledger SET payload = replace(payload, '13.98', '1.0') WHERE kind = 'placed'""")
    svc.conn.commit()
    report = svc.activity(say("layla", "can I trust that?"))
    assert not report.ledger_ok and "altered" in report.say


def test_receipt_catches_deleted_entries(svc):
    r = svc.propose(say("layla", "order two packs of AA batteries"), "aa-4", 2)
    receipt = r.receipt
    svc.conn.execute("DROP TRIGGER ledger_no_delete")
    svc.conn.execute("DELETE FROM ledger WHERE seq >= ?", (receipt.ledger_seq,))
    svc.conn.commit()
    assert svc.verify().ok  # truncation alone is invisible to the chain...
    assert not svc.check_receipt(receipt.ledger_seq, receipt.ledger_hash).ok  # ...but not to a kept receipt


def test_unknown_speaker_blocked(svc):
    assert svc.propose(say("stranger", "order batteries"), "aa-4", 1).status == "blocked"


def test_activity_visibility(svc):
    svc.propose(say("sara", "buy the space station building set"), "space-station-set", 1)
    svc.propose(say("layla", "order milk"), "milk", 1)
    assert len(svc.activity(say("layla", "what did we buy")).lines) == 2
    assert len(svc.activity(say("sara", "what did I ask for")).lines) == 1


def test_confirmation_expires(svc, clock):
    r = svc.propose(say("layla", "order two packs of AA batteries"), "aa-4", 20)
    clock.advance(minutes=11)
    assert svc.confirm(say("layla", "yes", answering=r.action_id), r.action_id).status == "declined"


def test_approval_expires_after_a_day(svc, clock):
    r = svc.propose(say("sara", "buy the space station building set"), "space-station-set", 1)
    clock.advance(days=2)
    svc.pending(say("layla", "what's waiting?"))
    assert svc.decide(say("layla", "yes", answering=r.action_id), r.action_id, approve=True).status == "declined"
