from datetime import date

import pytest

from pitchside.stats.filters import FilterError, compile_filters, parse_cli_filters


def test_ranges_lists_and_aliases():
    sql, params = compile_filters({"season": "2022-2024,2026", "over": "16+", "phase": "PP",
                                   "innings": "chasing", "batting_hand": "lhb"})
    assert "season_year BETWEEN ? AND ? OR season_year = ?" in sql
    assert "over_no + 1 >= ?" in sql
    assert params == [2022, 2024, 2026, 16, "powerplay", 2, "left"]


def test_text_dict_and_date_forms():
    sql, params = compile_filters({"venue": "Wankhede,Eden", "position": {"min": 1, "max": 3},
                                   "date": "2024-01-01..2024-06-30"})
    assert "venue ILIKE ? OR venue ILIKE ?" in sql
    assert params == ["%Wankhede%", "%Eden%", 1, 3, date(2024, 1, 1), date(2024, 6, 30)]


def test_player_filter_uses_resolver():
    sql, params = compile_filters({"bowler": "Bumrah"}, resolve_player=lambda q: "id-" + q)
    assert sql == "bowler_id IN (?)" and params == ["id-Bumrah"]


def test_empty_and_errors():
    assert compile_filters({}) == ("TRUE", [])
    assert compile_filters({"season": None}) == ("TRUE", [])
    with pytest.raises(FilterError, match="Unknown filter"):
        compile_filters({"sesaon": 2024})
    with pytest.raises(FilterError, match="choose from"):
        compile_filters({"phase": "slog"})
    with pytest.raises(FilterError, match="Bad number"):
        compile_filters({"season": "twenty"})
    with pytest.raises(FilterError):
        parse_cli_filters(["season2024"])
    assert parse_cli_filters(["season=2024", "venue=Eden Gardens"]) == {"season": "2024", "venue": "Eden Gardens"}
