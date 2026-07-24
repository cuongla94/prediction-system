from __future__ import annotations

import audit.checks_strategy_integrity as sia
from audit.checks_strategy_integrity import (
    check_bracket_boundary_parsing,
    check_edge_calculation_consistency,
    check_no_duplicate_paper_trades,
    check_observation_conditioning_lead_days_guard,
    check_reproduce_high_edge_zero_wins,
    check_station_timezone_convention,
    check_strategy_version_freshness,
)
from audit.report import Status


class FakeCursor:
    """Same pattern as tests/test_audit.py — canned rows per execute() call."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self._current: list = []

    def execute(self, _sql, _params=None):
        self._current = self._responses.pop(0) if self._responses else []

    def fetchall(self):
        return self._current


# --- check_edge_calculation_consistency -------------------------------------


def test_edge_consistency_passes_when_everything_recomputes_the_same():
    # model_p=0.70, market_p=0.50 -> edge=+0.20, fee well under a 0.20 threshold.
    cur = FakeCursor([[("TICK-1", 0.70, 0.50, 0.20, 0.05, True)]])
    finding = check_edge_calculation_consistency(cur)
    assert finding.status is Status.PASS


def test_edge_consistency_flags_a_recomputed_edge_mismatch():
    # Stored edge (0.05) doesn't match what model_p - market_p actually gives (0.20)
    # -- exactly what a sign inversion or stale formula would produce.
    cur = FakeCursor([[("TICK-2", 0.70, 0.50, 0.05, 0.05, True)]])
    finding = check_edge_calculation_consistency(cur)
    assert finding.status is Status.FLAG
    assert "TICK-2" in finding.evidence[0]


def test_edge_consistency_flags_an_is_actionable_threshold_mismatch():
    # edge=0.20, threshold=0.30 -> abs(edge) is NOT > threshold, so is_actionable
    # should be False, but the stored row says True.
    cur = FakeCursor([[("TICK-3", 0.70, 0.50, 0.20, 0.30, True)]])
    finding = check_edge_calculation_consistency(cur)
    assert finding.status is Status.FLAG


def test_edge_consistency_is_unknown_when_nothing_settled_yet():
    cur = FakeCursor([[]])
    finding = check_edge_calculation_consistency(cur)
    assert finding.status is Status.UNKNOWN


# --- check_bracket_boundary_parsing ------------------------------------------


def test_bracket_parsing_passes_on_real_valid_rules_text():
    cur = FakeCursor([[("TICK-4", "Payout if the temperature is between 79 and 80 degrees.", 79.0, 80.0)]])
    finding = check_bracket_boundary_parsing(cur)
    assert finding.status is Status.PASS


def test_bracket_parsing_flags_a_real_mismatch():
    # "less than" language paired with a floor+cap bracket is inconsistent --
    # check_boundary_language should reject this for real, not a mocked failure.
    cur = FakeCursor([[("TICK-5", "Payout if the temperature is less than 80 degrees.", 79.0, 80.0)]])
    finding = check_bracket_boundary_parsing(cur)
    assert finding.status is Status.FLAG
    assert "TICK-5" in finding.evidence[0]


def test_bracket_parsing_is_unknown_when_no_rows():
    cur = FakeCursor([[]])
    finding = check_bracket_boundary_parsing(cur)
    assert finding.status is Status.UNKNOWN


# --- check_station_timezone_convention ---------------------------------------


def test_station_timezone_convention_passes_on_the_real_stations_dict():
    # No mocking -- this pins the real, current weather/stations.py data.
    finding = check_station_timezone_convention()
    assert finding.status is Status.PASS


def test_station_timezone_convention_flags_a_dst_aware_zone(monkeypatch):
    from dataclasses import replace

    from weather.stations import STATIONS

    bad_series = next(iter(STATIONS))
    bad_stations = dict(STATIONS)
    bad_stations[bad_series] = replace(bad_stations[bad_series], standard_time_timezone="America/New_York")
    monkeypatch.setattr(sia, "STATIONS", bad_stations)

    finding = check_station_timezone_convention()
    assert finding.status is Status.FLAG
    assert "America/New_York" in finding.evidence[0]


# --- check_observation_conditioning_lead_days_guard --------------------------


def test_lead_days_guard_passes_on_the_real_generate_alerts_source():
    # No mocking -- pins the real, current guard in scripts/generate_alerts.py.
    finding = check_observation_conditioning_lead_days_guard()
    assert finding.status is Status.PASS


# --- check_no_duplicate_paper_trades ------------------------------------------


def test_no_duplicate_paper_trades_passes_when_none_found():
    cur = FakeCursor([[]])
    finding = check_no_duplicate_paper_trades(cur)
    assert finding.status is Status.PASS


def test_no_duplicate_paper_trades_flags_real_duplicates():
    cur = FakeCursor([[("TICK-6", 2)]])
    finding = check_no_duplicate_paper_trades(cur)
    assert finding.status is Status.FLAG
    assert "TICK-6" in finding.evidence[0]


# --- check_strategy_version_freshness -----------------------------------------


def test_strategy_version_freshness_passes_when_latest_row_matches_current_version():
    cur = FakeCursor([[(sia.STRATEGY_VERSION, "2026-07-23T00:00:00Z")]])
    finding = check_strategy_version_freshness(cur)
    assert finding.status is Status.PASS


def test_strategy_version_freshness_flags_a_stale_tag():
    cur = FakeCursor([[("v0-old", "2026-07-20T00:00:00Z")]])
    finding = check_strategy_version_freshness(cur)
    assert finding.status is Status.FLAG
    assert "v0-old" in finding.summary


def test_strategy_version_freshness_is_unknown_with_no_paper_trades_yet():
    cur = FakeCursor([[]])
    finding = check_strategy_version_freshness(cur)
    assert finding.status is Status.UNKNOWN


# --- check_reproduce_high_edge_zero_wins --------------------------------------


def test_reproduce_high_edge_zero_wins_passes_when_the_finding_reproduces():
    # Both high-edge markets lost against what the edge implied:
    #  +0.20 edge (implies YES) but settled NO; -0.15 edge (implies NO) but settled YES.
    cur = FakeCursor([[
        ("TICK-7", 0.20, False),
        ("TICK-8", -0.15, True),
        ("TICK-9", 0.05, True),  # below the 0.10 threshold -- excluded
    ]])
    finding = check_reproduce_high_edge_zero_wins(cur)
    assert finding.status is Status.PASS
    assert "0" in finding.evidence[1]


def test_reproduce_high_edge_zero_wins_flags_when_the_finding_does_not_reproduce():
    # +0.20 edge (implies YES), and it actually settled YES -- a real win.
    cur = FakeCursor([[("TICK-10", 0.20, True)]])
    finding = check_reproduce_high_edge_zero_wins(cur)
    assert finding.status is Status.FLAG


def test_reproduce_high_edge_zero_wins_is_unknown_with_no_high_edge_markets():
    cur = FakeCursor([[("TICK-11", 0.05, True)]])  # below threshold
    finding = check_reproduce_high_edge_zero_wins(cur)
    assert finding.status is Status.UNKNOWN
