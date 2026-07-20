from __future__ import annotations

import json
import time

import pytest

from weather.calibration_override import (
    clear_cache,
    load_override,
    override_metadata,
    write_override,
)
from weather.calibration_params import CALIBRATION, CalibrationParams, get_calibration


@pytest.fixture(autouse=True)
def _isolate_cache():
    # The module-level mtime cache is process-global; without this a test that
    # populated it would leak into the next one.
    clear_cache()
    yield
    clear_cache()


def _params(bias=1.0, monthly=None, std=2.0):
    return CalibrationParams(
        overall_bias=bias, monthly_bias=monthly, std=std, fit_date="2026-07-20", fit_days=400
    )


# --- round-trip ------------------------------------------------------------


def test_write_then_load_round_trips(tmp_path):
    path = tmp_path / "fitted.json"
    write_override(
        {"KXHIGHNY": _params(bias=1.5, monthly={1: 2.5, 7: -1.5}, std=2.2)},
        fit_date="2026-07-20",
        start_date="2024-10-01",
        end_date="2026-07-19",
        path=path,
    )
    loaded = load_override(path)
    assert loaded["KXHIGHNY"].overall_bias == pytest.approx(1.5)
    assert loaded["KXHIGHNY"].std == pytest.approx(2.2)
    assert loaded["KXHIGHNY"].fit_days == 400


def test_monthly_bias_keys_survive_as_ints(tmp_path):
    # JSON object keys are always strings. CalibrationParams.bias_for_month
    # looks up by int, so a missed conversion wouldn't raise — it would
    # silently fall back to the flat bias for every month, quietly undoing
    # the entire monthly correction for NYC/Chicago/Denver.
    path = tmp_path / "fitted.json"
    write_override(
        {"KXHIGHNY": _params(bias=1.0, monthly={1: 2.5, 7: -1.5})},
        fit_date="2026-07-20",
        start_date="2024-10-01",
        end_date="2026-07-19",
        path=path,
    )
    params = load_override(path)["KXHIGHNY"]
    assert set(params.monthly_bias) == {1, 7}
    assert params.bias_for_month(7) == pytest.approx(-1.5)
    assert params.bias_for_month(3) == pytest.approx(1.0)  # unfitted month -> flat


def test_write_is_atomic_leaving_no_temp_file(tmp_path):
    # The weekly fit runs on the same box serving the dashboard and the */15
    # settlement cron, so a partially-written file must never be observable.
    path = tmp_path / "fitted.json"
    write_override({"KXHIGHNY": _params()}, fit_date="d", start_date="a", end_date="b", path=path)
    assert path.exists()
    assert list(tmp_path.iterdir()) == [path]


# --- failing soft ----------------------------------------------------------


def test_missing_file_returns_empty(tmp_path):
    assert load_override(tmp_path / "nope.json") == {}


def test_malformed_json_falls_back_rather_than_raising(tmp_path, capsys):
    path = tmp_path / "fitted.json"
    path.write_text("{not json at all")
    assert load_override(path) == {}
    assert "malformed" in capsys.readouterr().out


def test_valid_json_wrong_shape_falls_back(tmp_path):
    # Parseable JSON without the expected "params" key — a truncated or
    # half-migrated file shouldn't take pricing down.
    path = tmp_path / "fitted.json"
    path.write_text(json.dumps({"fit_date": "2026-07-20"}))
    assert load_override(path) == {}


def test_entry_missing_required_field_falls_back(tmp_path):
    path = tmp_path / "fitted.json"
    path.write_text(json.dumps({"params": {"KXHIGHNY": {"overall_bias": 1.0}}}))  # no std
    assert load_override(path) == {}


def test_metadata_none_for_unusable_file(tmp_path):
    path = tmp_path / "fitted.json"
    path.write_text("garbage")
    assert override_metadata(path) is None


def test_metadata_reports_fit_window(tmp_path):
    path = tmp_path / "fitted.json"
    write_override(
        {"KXHIGHNY": _params(), "KXHIGHCHI": _params()},
        fit_date="2026-07-20",
        start_date="2024-10-01",
        end_date="2026-07-19",
        path=path,
    )
    meta = override_metadata(path)
    assert meta["fit_date"] == "2026-07-20"
    assert meta["series_count"] == 2


# --- precedence in get_calibration -----------------------------------------


def test_get_calibration_uses_committed_baseline_when_no_override(monkeypatch, tmp_path):
    monkeypatch.setattr("weather.calibration_override.OVERRIDE_PATH", tmp_path / "absent.json")
    clear_cache()
    assert get_calibration("KXHIGHNY") == CALIBRATION["KXHIGHNY"]


def test_override_takes_precedence_over_committed_baseline(monkeypatch, tmp_path):
    path = tmp_path / "fitted.json"
    write_override(
        {"KXHIGHNY": _params(bias=99.0, std=3.3)},
        fit_date="2026-07-20",
        start_date="a",
        end_date="b",
        path=path,
    )
    monkeypatch.setattr("weather.calibration_override.OVERRIDE_PATH", path)
    clear_cache()
    assert get_calibration("KXHIGHNY").overall_bias == pytest.approx(99.0)
    # A series absent from the override still resolves from the baseline, so a
    # partial override can't blank out cities it didn't mention.
    assert get_calibration("KXHIGHCHI") == CALIBRATION["KXHIGHCHI"]


def test_unknown_series_still_raises_with_the_guidance_message(monkeypatch, tmp_path):
    monkeypatch.setattr("weather.calibration_override.OVERRIDE_PATH", tmp_path / "absent.json")
    clear_cache()
    with pytest.raises(KeyError, match="No fitted calibration"):
        get_calibration("KXHIGHNOWHERE")


# --- cache invalidation ----------------------------------------------------


def test_rewriting_the_override_is_picked_up_without_a_restart(monkeypatch, tmp_path):
    # The dashboard runs under gunicorn as a long-lived process. A read-once
    # cache would keep serving last week's numbers until someone restarted the
    # service, silently disagreeing with the cron scripts (fresh process each
    # run) about what the model is calibrated to.
    path = tmp_path / "fitted.json"
    monkeypatch.setattr("weather.calibration_override.OVERRIDE_PATH", path)

    write_override({"KXHIGHNY": _params(bias=1.0)}, fit_date="d", start_date="a", end_date="b", path=path)
    clear_cache()
    assert get_calibration("KXHIGHNY").overall_bias == pytest.approx(1.0)

    # mtime has 1s granularity on some filesystems; make the change detectable.
    time.sleep(1.1)
    write_override({"KXHIGHNY": _params(bias=7.0)}, fit_date="d", start_date="a", end_date="b", path=path)
    assert get_calibration("KXHIGHNY").overall_bias == pytest.approx(7.0)
