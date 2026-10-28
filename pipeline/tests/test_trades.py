"""Draft-pick ownership chains and outcomes."""
from pipeline.trades import draft_results, pick_chains, resolve_trades

PLAYERS = {
    "p1": {"full_name": "Davante Adams", "position": "WR"},
    "p2": {"full_name": "Denzel Boston", "position": "WR"},
    "p3": {"full_name": "Ted Hurst", "position": "WR"},
    "p4": {"full_name": "Someone Else", "position": "RB"},
}


def trade(tid, time, rosters, adds=None, picks=(), season="2026"):
    return {"transaction_id": tid, "type": "trade", "status": "complete", "status_updated": time,
            "roster_ids": rosters, "adds": adds or {}, "drops": {}, "leg": 1, "season": season,
            "draft_picks": [dict(season=s, round=r, roster_id=orig, owner_id=to, previous_owner_id=frm)
                            for s, r, orig, frm, to in picks]}


# Rosters: 1 = SilkyJohnson, 2 = Team Nips, 3 = Kim Jong Drew, 4 = a fourth team.
DRAFT_2026 = {
    "season": "2026", "status": "complete", "start_time": 100,
    "slot_to_roster_id": {"1": 2, "2": 1, "3": 3, "4": 4},
    "picks": [
        # Team Nips' 1st (slot 1) was used by Kim Jong Drew after two trades.
        {"round": 1, "draft_slot": 1, "roster_id": 3, "player_id": "p2"},
        {"round": 3, "draft_slot": 2, "roster_id": 2, "player_id": "p3"},
        {"round": 1, "draft_slot": 2, "roster_id": 1, "player_id": "p4"},
    ],
}

TRADES = [
    # SilkyJohnson (1) sends Davante Adams and his 2026 3rd to Team Nips (2) for Nips' 2026 1st.
    trade("t1", 1000, [1, 2], adds={"p1": 2},
          picks=[("2026", 3, 1, 1, 2), ("2026", 1, 2, 2, 1)]),
    # SilkyJohnson flips that 1st to Kim Jong Drew (3).
    trade("t2", 2000, [1, 3], picks=[("2026", 1, 2, 1, 3)]),
    # Team Nips trades a 2028 2nd (their own) to team 4, which trades it on to team 3: no draft yet.
    trade("t3", 3000, [2, 4], picks=[("2028", 2, 2, 2, 4)]),
    trade("t4", 4000, [4, 3], picks=[("2028", 2, 2, 4, 3)]),
]


def card(cards, tid):
    return next(c for c in cards if c["transaction_id"] == tid)


def side(c, rid):
    return next(s for s in c["sides"] if s["roster_id"] == rid)


def test_draft_results_match_by_original_roster_via_draft_slot():
    results, complete, _ = draft_results([DRAFT_2026], PLAYERS)
    assert results[("2026", 1, 2)]["player"] == "Denzel Boston"
    assert results[("2026", 1, 2)]["roster_id"] == 3          # who used it
    assert results[("2026", 3, 1)]["player"] == "Ted Hurst"
    assert complete == {"2026"}


def test_draft_results_fall_back_to_traded_picks_without_slots():
    draft = dict(DRAFT_2026, slot_to_roster_id=None, picks=[
        {"round": 1, "draft_slot": 1, "roster_id": 3, "player_id": "p2"}],
        traded_picks=[{"season": "2026", "round": 1, "roster_id": 2, "owner_id": 3},
                      {"season": "2026", "round": 1, "roster_id": 3, "owner_id": 4}])
    results, _, _ = draft_results([draft], PLAYERS)
    assert ("2026", 1, 2) in results


def test_chain_is_ordered_by_time_not_input_order():
    chains = pick_chains(list(reversed(TRADES)))
    assert [e["transaction_id"] for e in chains[("2026", 1, 2)]] == ["t1", "t2"]
    assert [(e["from"], e["to"]) for e in chains[("2026", 1, 2)]] == [(2, 1), (1, 3)]


def test_pick_traded_twice_shows_later_trade_then_player():
    cards = resolve_trades(TRADES, [DRAFT_2026], PLAYERS)
    first = side(card(cards, "t1"), 1)["picks"][0]
    assert (first["season"], first["round"], first["original"]) == ("2026", 1, 2)
    assert [(e["transaction_id"], e["to"]) for e in first["later"]] == [("t2", 3)]
    assert first["outcome"]["status"] == "made"
    assert first["outcome"]["player"] == "Denzel Boston" and first["outcome"]["roster_id"] == 3
    # On the second trade's card the same pick has no further trades.
    second = side(card(cards, "t2"), 3)["picks"][0]
    assert second["later"] == [] and second["outcome"]["player"] == "Denzel Boston"


def test_pick_used_by_receiving_team():
    cards = resolve_trades(TRADES, [DRAFT_2026], PLAYERS)
    third = side(card(cards, "t1"), 2)["picks"][0]
    assert third["later"] == []
    assert third["outcome"] == {"status": "made", "player_id": "p3", "player": "Ted Hurst", "pos": "WR",
                                "roster_id": 2, "pick_no": None}
    assert [p["name"] for p in side(card(cards, "t1"), 2)["players"]] == ["Davante Adams"]


def test_unused_future_pick_is_pending_with_later_trades():
    cards = resolve_trades(TRADES, [DRAFT_2026], PLAYERS)
    pick = side(card(cards, "t3"), 4)["picks"][0]
    assert [e["transaction_id"] for e in pick["later"]] == ["t4"]
    assert pick["outcome"] == {"status": "pending", "owner": 3}


def test_league_traded_picks_override_pending_owner():
    cards = resolve_trades(TRADES, [DRAFT_2026], PLAYERS,
                           traded_picks=[{"season": "2028", "round": 2, "roster_id": 2, "owner_id": 1}])
    assert side(card(cards, "t4"), 3)["picks"][0]["outcome"]["owner"] == 1


def test_cards_newest_first_and_deduplicated():
    cards = resolve_trades(TRADES + [TRADES[0]], [DRAFT_2026], PLAYERS)
    assert [c["transaction_id"] for c in cards] == ["t4", "t3", "t2", "t1"]


def test_pick_in_finished_draft_without_a_selection_is_unused():
    t = [trade("t9", 10, [1, 2], picks=[("2026", 2, 4, 4, 1)])]
    cards = resolve_trades(t, [DRAFT_2026], PLAYERS)
    assert side(cards[0], 1)["picks"][0]["outcome"] == {"status": "unused"}
