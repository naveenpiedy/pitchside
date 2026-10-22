from pitchside.etl.parse import overs_to_balls, parse_match, phase_of


def by_seq(rows, innings_no=1):
    return {r["seq"]: r for r in rows if r["innings_no"] == innings_no}


def test_ball_state_is_captured_before_each_ball(mini_match):
    d = by_seq(parse_match("1", mini_match, "test").deliveries)

    # 4 off the bat, then a wide: the wide is not legal and not faced.
    assert d[2]["is_legal"] is False and d[2]["is_ball_faced"] is False
    assert d[2]["score_before"] == 4 and d[2]["legal_balls_before"] == 1
    # No-ball: not legal, but faced, and the 4 off it is the batter's.
    assert d[3]["is_ball_faced"] and not d[3]["is_legal"] and d[3]["is_four"]
    assert d[3]["bowler_runs"] == 5
    # Leg-byes are not charged to the bowler.
    assert d[4]["bowler_runs"] == 0 and d[4]["runs_total"] == 1
    # The striker's own running score.
    assert d[4]["batter_runs_before"] == 8 and d[4]["batter_balls_before"] == 2


def test_wickets_and_batting_positions(mini_match):
    rows = parse_match("1", mini_match, "test")
    d = by_seq(rows.deliveries)

    # Non-striker run out: a wicket, but not the striker's and not the bowler's.
    assert d[5]["is_wicket"] and not d[5]["is_batter_out"] and not d[5]["is_bowler_wicket"]
    # Retired hurt doesn't count as a wicket.
    assert not d[8]["is_wicket"] and d[9]["wickets_before"] == 1
    assert d[9]["is_bowler_wicket"] and d[9]["is_batter_out"]
    # The non-boundary 4 is not a four.
    assert d[7]["runs_batter"] == 4 and not d[7]["is_four"]

    order = {(b["innings_no"], b["player_id"]): b for b in rows.batting_order}
    assert [order[(1, p)]["position"] for p in ("a1", "a2", "a3", "a4")] == [1, 2, 3, 4]
    assert order[(1, "a3")]["entry_score"] == 12 and order[(1, "a3")]["entry_wickets"] == 1

    wk = {w["player_out_id"]: w for w in rows.wickets}
    assert wk["a2"]["counts_as_out"] is False and wk["a2"]["wicket_number"] is None
    assert wk["a4"]["wicket_number"] == 2 and wk["a4"]["fielders"] == ["B3"]


def test_innings_totals_targets_and_super_over(mini_match):
    rows = parse_match("1", mini_match, "test")
    inns = {i["innings_no"]: i for i in rows.innings}
    assert (inns[1]["runs"], inns[1]["wickets"], inns[1]["legal_balls"], inns[1]["extras"]) == (22, 2, 7, 3)
    assert inns[3]["is_super_over"]

    chase = by_seq(rows.deliveries, innings_no=2)[1]
    assert chase["balls_remaining"] == 30  # DLS target over 5 overs
    assert chase["runs_required"] == 23
    assert by_seq(rows.deliveries, innings_no=3)[1]["balls_remaining"] == 6

    m = rows.matches[0]
    assert m["result"] == "tie" and m["winner"] == "Blues" and m["player_of_match"] == ["a1"]


def test_helpers():
    assert overs_to_balls(16.3, 6) == 99
    assert overs_to_balls(20, 6) == 120
    assert [phase_of(o, 20) for o in (0, 5, 6, 15, 16, 19)] == [
        "powerplay", "powerplay", "middle", "middle", "death", "death"]
