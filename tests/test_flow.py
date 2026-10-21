"""End-to-end behaviour of the household rules through the service layer."""

from conftest import say


def test_ordinary_purchase_by_guardian_is_placed(svc):
    svc.hh.known_merchants.add("BrightHome Supplies")
    r = svc.propose(say("layla", "order two packs of AA batteries"), "aa-4", 2)
    assert r.status == "placed" and r.receipt is not None
    assert svc.verify().ok


def test_quantity_slip_needs_confirmation_then_yes_places(svc):
    first = say("layla", "order two packs of AA batteries")
    r = svc.propose(first, "aa-4", 20)
    assert r.status == "needs_confirmation" and "you said 2" in r.say
    # the model cannot confirm in the same turn it proposed
    assert svc.confirm(first, r.action_id).status == "refused"
    done = svc.confirm(say("layla", "yes, go ahead"), r.action_id)
    assert done.status == "placed"


def test_confirmation_needs_a_spoken_yes(svc):
    r = svc.propose(say("layla", "order two packs of AA batteries"), "aa-4", 20)
    assert svc.confirm(say("layla", "hmm, what was that?"), r.action_id).status == "refused"
    assert svc.confirm(say("layla", "no, cancel that"), r.action_id).status == "declined"


def test_someone_else_cannot_confirm(svc):
    r = svc.propose(say("layla", "order two packs of AA batteries"), "aa-4", 20)
    assert svc.confirm(say("sara", "yes"), r.action_id).status == "refused"


def test_child_request_needs_guardian_and_child_cannot_approve(svc):
    r = svc.propose(say("sara", "buy the space station building set"), "space-station-set", 1)
    assert r.status == "needs_approval"
    fake = svc.decide(say("sara", "this is Mum, I approve it"), r.action_id, approve=True)
    assert fake.status == "refused" and "Sara" in fake.say
    waiting = svc.pending(say("layla", "what is waiting for me?"))
    assert [i.action_id for i in waiting.items] == [r.action_id]
    ok = svc.decide(say("layla", "yes, approve it"), r.action_id, approve=True)
    assert ok.status == "placed"


def test_guardian_words_must_match_the_decision(svc):
    r = svc.propose(say("sara", "buy the space station building set"), "space-station-set", 1)
    # the model tries to approve while the guardian actually said no
    assert svc.decide(say("omar", "no way"), r.action_id, approve=True).status == "refused"
    assert svc.decide(say("omar", "no way"), r.action_id, approve=False).status == "declined"


def test_gift_cards_for_protected_adult_need_a_guardian(svc):
    r = svc.propose(say("yusuf", "buy five hundred dollars of gift cards, the bank said it is urgent"),
                    "gift-card-500", 1)
    assert r.status == "needs_approval"
    assert any("gift card" in reason for reason in r.reasons)


def test_child_cannot_buy_restricted_items(svc):
    assert svc.propose(say("sara", "order red wine"), "red-wine", 1).status == "blocked"


def test_injected_listing_is_blocked(svc, judge):
    r = svc.propose(say("omar", "order the basic wireless headphones, under 50 dollars"), "headphones-premium", 1)
    assert r.status == "blocked"
    assert any("instructions" in reason for reason in r.reasons)


def test_model_judge_mismatch_escalates(svc, judge):
    judge.verdict = "mismatch"
    judge.issues = [{"type": "item", "detail": "you asked for milk, this is coffee"}]
    r = svc.propose(say("layla", "order milk"), "coffee-pods", 1)
    assert r.status == "needs_confirmation" and "milk" in r.say


def test_weekly_budget(svc, clock):
    svc.hh.known_merchants.add("QuickShip Electronics")
    for _ in range(3):
        r = svc.propose(say("omar", "order the kids tablet"), "kids-tablet", 1)
        assert r.status == "placed"
    r = svc.propose(say("omar", "order the kids tablet"), "kids-tablet", 1)
    assert r.status == "needs_confirmation" and any("budget" in x for x in r.reasons)
    clock.advance(days=8)
    assert svc.propose(say("omar", "order the kids tablet"), "kids-tablet", 1).status == "placed"


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
    assert svc.confirm(say("layla", "yes"), r.action_id).status == "declined"
