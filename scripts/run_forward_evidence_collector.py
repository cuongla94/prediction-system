"""Collect one prospective decision batch or run the persistent market stream.

Neither mode calls create_order/cancel_order. All outputs are append-only
research evidence and simulated paper-order events.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from kalshi_client import KalshiClient
from trading_readiness.collector import (
    ForwardEvidenceCollector,
    PostgresEvidenceRepository,
    run_stream_forever,
)
from trading_readiness.config import ReadinessConfig
from trading_readiness.freeze import frozen_candidates, load_frozen_candidates
from trading_readiness.professional_collector import (
    PostgresProfessionalRepository,
    ProfessionalJournalCollector,
)
from trading_readiness.professional_freeze import (
    frozen_professional_strategy,
    load_professional_freeze,
    professional_code_hash,
)


def _code_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    files = [
        root / "trading_readiness" / "collector.py",
        root / "trading_readiness" / "execution.py",
        root / "trading_readiness" / "freeze.py",
        root / "trading_readiness" / "stream.py",
    ]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run(mode: str) -> int:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required.")
        return 1
    config = ReadinessConfig.from_env()
    manifest_path = (
        Path(__file__).resolve().parents[1]
        / "artifacts"
        / "trading_readiness"
        / "candidate_freeze_manifest.json"
    )
    if not manifest_path.exists():
        print(
            "Frozen candidate manifest is missing. Run "
            "scripts/run_trading_readiness_report.py first."
        )
        return 1
    professional_manifest_path = (
        Path(__file__).resolve().parents[1]
        / "artifacts"
        / "professional_trader"
        / "strategy_freeze_manifest.json"
    )
    if mode == "once" and not professional_manifest_path.exists():
        print(
            "Professional trader freeze manifest is missing. Run "
            "scripts/run_professional_trader_report.py first."
        )
        return 1
    candidates = load_frozen_candidates(manifest_path)
    expected = frozen_candidates(
        freeze_timestamp=datetime.fromisoformat(
            candidates[0].candidate_freeze_timestamp
        ),
        code_hash=_code_hash(),
        required_independent_events=config.minimum_independent_events,
    )
    if {
        candidate.strategy_version: candidate.code_config_hash
        for candidate in candidates
    } != {
        candidate.strategy_version: candidate.code_config_hash
        for candidate in expected
    }:
        print(
            "Collector code/config no longer matches the frozen manifest. "
            "Create new candidate versions and a new confirmatory period."
        )
        return 1
    professional_freeze = None
    if mode == "once":
        professional_freeze = load_professional_freeze(
            professional_manifest_path
        )
        expected_professional = frozen_professional_strategy(
            frozen_at=datetime.fromisoformat(professional_freeze.frozen_at),
            code_hash=professional_code_hash(
                Path(__file__).resolve().parents[1]
            ),
        )
        if (
            professional_freeze.code_config_hash
            != expected_professional.code_config_hash
            or professional_freeze.decision_policy_version
            != expected_professional.decision_policy_version
            or professional_freeze.policy_config
            != expected_professional.policy_config
        ):
            print(
                "Professional decision code/config no longer matches its "
                "freeze. Create a new policy version and forward cohort."
            )
            return 1
    with (
        psycopg.connect(database_url, connect_timeout=10) as connection,
        KalshiClient.from_env() as client,
    ):
        repository = PostgresEvidenceRepository(connection)
        repository.insert_freezes(candidates)
        connection.commit()
        collector = ForwardEvidenceCollector(
            client=client,
            repository=repository,
            candidates=candidates,
            config=config,
        )
        if mode == "stream":
            asyncio.run(run_stream_forever(collector))
            return 0
        counters = collector.collect_decisions()
        professional = ProfessionalJournalCollector(
            client=client,
            repository=PostgresProfessionalRepository(connection),
            freeze=professional_freeze,
        )
        professional_counters = professional.collect()
        connection.commit()
    print(
        "Forward evidence: "
        f"{counters['decisions']} decisions, "
        f"{counters['eligible']} eligible, "
        f"{counters['paper_events']} paper events, "
        f"{counters['settled']} newly settled; professional journal: "
        f"{professional_counters['information_events']} information events, "
        f"{professional_counters['decision_snapshots']} decisions, "
        f"{professional_counters['post_trade_reviews']} reviews."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("once", "stream"), default="once"
    )
    args = parser.parse_args()
    return run(args.mode)


if __name__ == "__main__":
    sys.exit(main())
