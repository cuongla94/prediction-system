from __future__ import annotations

from scripts.send_notifications import AlertCandidate, find_alerts_needing_notification


def _candidate(id_: int, is_actionable: bool, edge: float = 0.15) -> AlertCandidate:
    return AlertCandidate(
        id=id_,
        city="NYC",
        bracket_label="85–86°",
        edge=edge,
        model_probability=0.40,
        kalshi_url="https://kalshi.com/markets/kxhighny/x/kxhighny-26jul18",
        is_actionable=is_actionable,
    )


def test_actionable_alert_not_yet_notified_is_included():
    candidates = [(_candidate(1, is_actionable=True), "TICKER-A")]
    result = find_alerts_needing_notification(candidates, already_notified_today=set())
    assert [a.id for a in result] == [1]


def test_non_actionable_alert_is_excluded():
    candidates = [(_candidate(1, is_actionable=False), "TICKER-A")]
    result = find_alerts_needing_notification(candidates, already_notified_today=set())
    assert result == []


def test_already_notified_ticker_is_excluded():
    candidates = [(_candidate(1, is_actionable=True), "TICKER-A")]
    result = find_alerts_needing_notification(candidates, already_notified_today={"TICKER-A"})
    assert result == []


def test_mixed_candidates_only_returns_new_actionable_ones():
    candidates = [
        (_candidate(1, is_actionable=True), "TICKER-A"),  # new, actionable -> included
        (_candidate(2, is_actionable=True), "TICKER-B"),  # already notified -> excluded
        (_candidate(3, is_actionable=False), "TICKER-C"),  # not actionable -> excluded
    ]
    result = find_alerts_needing_notification(candidates, already_notified_today={"TICKER-B"})
    assert [a.id for a in result] == [1]


def test_side_derived_from_edge_sign():
    assert _candidate(1, True, edge=0.1).side == "YES"
    assert _candidate(1, True, edge=-0.1).side == "NO"
    assert _candidate(1, True, edge=0.0).side == "FLAT"
