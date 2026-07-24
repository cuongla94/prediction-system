from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FREEZE_MANIFEST_VERSION = "forward-confirmation-v1"


@dataclass(frozen=True)
class FrozenCandidate:
    strategy_name: str
    strategy_version: str
    model_weight: float
    market_weight: float
    calibration_method: str
    signal_threshold: float
    no_trade_filters: tuple[str, ...]
    maximum_acceptable_price_logic: str
    candidate_freeze_timestamp: str
    confirmatory_period_start: str
    required_independent_event_count: int
    code_config_hash: str
    promotion_status: str = "FROZEN_RESEARCH"
    automatic_promotion_allowed: bool = False

    def __post_init__(self) -> None:
        if abs(self.model_weight + self.market_weight - 1.0) > 1e-12:
            raise ValueError("Frozen blend weights must sum to 1.0.")
        if self.promotion_status == "LIVE_CANDIDATE":
            raise ValueError("Freezing never promotes a candidate.")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["no_trade_filters"] = list(self.no_trade_filters)
        return value


def _candidate_hash(value: dict[str, Any], code_hash: str) -> str:
    payload = {
        key: item
        for key, item in value.items()
        if key
        not in {
            "candidate_freeze_timestamp",
            "confirmatory_period_start",
            "code_config_hash",
        }
    }
    payload["collector_code_hash"] = code_hash
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def frozen_candidates(
    *,
    freeze_timestamp: datetime,
    code_hash: str,
    required_independent_events: int = 100,
) -> tuple[FrozenCandidate, FrozenCandidate]:
    """The two predeclared candidates; no search or dynamic selection occurs."""
    timestamp = freeze_timestamp.astimezone(UTC).isoformat()
    definitions = (
        {
            "strategy_name": "blend_model_0.50_market_0.50",
            "strategy_version": "forward-blend-model-0.50-market-0.50-v1",
            "model_weight": 0.50,
            "market_weight": 0.50,
            "calibration_method": "stored_normal_v4_observation_conditioned",
            "signal_threshold": 0.05,
            "no_trade_filters": (
                "invalid_probability",
                "stale_or_invalid_orderbook",
                "insufficient_visible_depth",
                "price_above_maximum",
            ),
            "maximum_acceptable_price_logic": (
                "selected_side_probability - signal_threshold - estimated_taker_fee"
            ),
        },
        {
            "strategy_name": "blend_model_0.25_market_0.75",
            "strategy_version": "forward-blend-model-0.25-market-0.75-v1",
            "model_weight": 0.25,
            "market_weight": 0.75,
            "calibration_method": "stored_normal_v4_observation_conditioned",
            "signal_threshold": 0.05,
            "no_trade_filters": (
                "invalid_probability",
                "stale_or_invalid_orderbook",
                "insufficient_visible_depth",
                "price_above_maximum",
            ),
            "maximum_acceptable_price_logic": (
                "selected_side_probability - signal_threshold - estimated_taker_fee"
            ),
        },
    )
    result: list[FrozenCandidate] = []
    for definition in definitions:
        config_hash = _candidate_hash(definition, code_hash)
        result.append(
            FrozenCandidate(
                **definition,
                candidate_freeze_timestamp=timestamp,
                confirmatory_period_start=timestamp,
                required_independent_event_count=required_independent_events,
                code_config_hash=config_hash,
            )
        )
    return result[0], result[1]


def persist_freeze_manifest(
    path: Path,
    candidates: tuple[FrozenCandidate, ...],
) -> dict[str, Any]:
    """Write once, then reject any mutation of an existing candidate version."""
    requested = {
        "manifest_version": FREEZE_MANIFEST_VERSION,
        "candidate_count": len(candidates),
        "candidates": [candidate.to_dict() for candidate in candidates],
        "configured_live_strategy_changed": False,
        "automatic_promotion_allowed": False,
    }
    if path.exists():
        current = json.loads(path.read_text())
        current_versions = {
            row["strategy_version"]: row for row in current.get("candidates", [])
        }
        for row in requested["candidates"]:
            existing = current_versions.get(row["strategy_version"])
            if existing is None:
                raise ValueError(
                    f"Cannot add {row['strategy_version']} to an active freeze manifest."
                )
            ignored = {
                "candidate_freeze_timestamp",
                "confirmatory_period_start",
            }
            existing_config = {
                key: value for key, value in existing.items() if key not in ignored
            }
            requested_config = {
                key: value for key, value in row.items() if key not in ignored
            }
            if existing_config != requested_config:
                raise ValueError(
                    f"Frozen candidate {row['strategy_version']} is immutable."
                )
        return current
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(requested, indent=2, sort_keys=True) + "\n")
    return requested


def load_frozen_candidates(path: Path) -> tuple[FrozenCandidate, ...]:
    value = json.loads(path.read_text())
    candidates = []
    for row in value.get("candidates", []):
        candidate = dict(row)
        candidate["no_trade_filters"] = tuple(candidate["no_trade_filters"])
        candidates.append(FrozenCandidate(**candidate))
    if not candidates:
        raise ValueError("Freeze manifest contains no candidates.")
    return tuple(candidates)
