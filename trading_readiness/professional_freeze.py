from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from collections.abc import Mapping

from paper_trading import STRATEGY_VERSION

PROFESSIONAL_POLICY_VERSION = "professional-trader-v2-2026-07-24"
PROFESSIONAL_FREEZE_VERSION = "professional-forward-test-v1"


@dataclass(frozen=True)
class ProfessionalStrategyFreeze:
    base_strategy_name: str
    base_strategy_version: str
    decision_policy_version: str
    probability_method_version: str
    policy_config: Mapping[str, Any]
    code_config_hash: str
    frozen_at: str
    forward_period_start: str
    automatic_promotion_allowed: bool = False
    configured_live_strategy_changed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_config",
            MappingProxyType(dict(self.policy_config)),
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            field.name: getattr(self, field.name)
            for field in fields(self)
        }
        value["policy_config"] = dict(self.policy_config)
        return value


def professional_code_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in (
        "trading_readiness/professional.py",
        "trading_readiness/professional_collector.py",
        "trading_readiness/professional_freeze.py",
    ):
        digest.update((root / relative).read_bytes())
    return digest.hexdigest()


def frozen_professional_strategy(
    *,
    frozen_at: datetime,
    code_hash: str,
) -> ProfessionalStrategyFreeze:
    timestamp = frozen_at.astimezone(UTC).isoformat()
    return ProfessionalStrategyFreeze(
        base_strategy_name="weather-daily-temp",
        base_strategy_version=STRATEGY_VERSION,
        decision_policy_version=PROFESSIONAL_POLICY_VERSION,
        probability_method_version="normal-v4-observation-conditioned",
        policy_config={
            "margin_of_safety": "0.05",
            "maximum_spread": "0.10",
            "intended_quantity": "1",
            "maximum_order_size": "1",
            "material_forecast_change_degrees": "0.50",
            "material_probability_change": "0.02",
            "material_spread_change": "0.02",
            "material_quantity_change": "1",
            "prospective_paper_only": True,
            "automatic_promotion_allowed": False,
        },
        code_config_hash=code_hash,
        frozen_at=timestamp,
        forward_period_start=timestamp,
    )


def persist_professional_freeze(
    path: Path,
    freeze: ProfessionalStrategyFreeze,
) -> dict[str, Any]:
    requested = {
        "manifest_version": PROFESSIONAL_FREEZE_VERSION,
        "freeze": freeze.to_dict(),
    }
    if path.exists():
        current = json.loads(path.read_text())
        ignored = {"frozen_at", "forward_period_start"}
        existing = {
            key: value
            for key, value in current["freeze"].items()
            if key not in ignored
        }
        new = {
            key: value
            for key, value in requested["freeze"].items()
            if key not in ignored
        }
        if existing != new:
            existing_version = current["freeze"][
                "decision_policy_version"
            ]
            requested_version = requested["freeze"][
                "decision_policy_version"
            ]
            if existing_version == requested_version:
                raise ValueError(
                    "Professional forward-test freeze is immutable; create a "
                    "new decision policy version and forward cohort."
                )
            archive_path = path.with_name(
                f"{path.stem}.{existing_version}{path.suffix}"
            )
            if archive_path.exists():
                archived = json.loads(archive_path.read_text())
                if archived != current:
                    raise ValueError(
                        "Existing professional policy archive does not match "
                        "the frozen manifest."
                    )
            else:
                archive_path.write_text(
                    json.dumps(current, indent=2, sort_keys=True) + "\n"
                )
            path.write_text(
                json.dumps(requested, indent=2, sort_keys=True) + "\n"
            )
            return requested
        return current
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(requested, indent=2, sort_keys=True) + "\n")
    return requested


def load_professional_freeze(path: Path) -> ProfessionalStrategyFreeze:
    value = json.loads(path.read_text())
    return ProfessionalStrategyFreeze(**value["freeze"])
