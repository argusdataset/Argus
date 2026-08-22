"""Schema-level guarantees that later modules will depend on.

Each of these encodes a rule from ARGUS's design that would be expensive
or impossible to recover if the schema quietly failed to enforce it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from infra.db.enums import MarketState
from infra.db.metadata import pit_columns
from infra.db.schema import metadata

PIT_COLUMN_NAMES = [column.name for column in pit_columns()]
CANONICAL_TABLES = ["canonical_ohlcv", "canonical_fundamentals", "canonical_corporate_actions"]


# --------------------------------------------------------------------------
# Point-in-time correctness
# --------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", CANONICAL_TABLES)
def test_canonical_table_has_all_four_pit_columns(engine: Engine, table_name: str):
    with engine.connect() as conn:
        present = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = ANY(:cols)"
                ),
                {"t": table_name, "cols": PIT_COLUMN_NAMES},
            )
        }
    assert present == set(PIT_COLUMN_NAMES)


@pytest.mark.parametrize("table_name", CANONICAL_TABLES)
def test_pit_columns_are_not_nullable(engine: Engine, table_name: str):
    """A nullable PIT column is a hole the enforcement in Module 07 cannot close."""
    with engine.connect() as conn:
        nullable = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = ANY(:cols) AND is_nullable = 'YES'"
                ),
                {"t": table_name, "cols": PIT_COLUMN_NAMES},
            )
        ]
    assert nullable == []


def test_pending_material_events_also_carries_pit_columns(engine: Engine):
    """Knowing *when* an earnings date was announced is itself PIT-sensitive."""
    with engine.connect() as conn:
        present = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'pending_material_events' AND column_name = ANY(:cols)"
                ),
                {"cols": PIT_COLUMN_NAMES},
            )
        }
    assert present == set(PIT_COLUMN_NAMES)


def test_feature_vectors_track_input_availability(engine: Engine):
    """Module 07 must be able to verify a vector used only data available by a date."""
    with engine.connect() as conn:
        nullable = conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'feature_vectors' AND column_name = 'availability_time'"
            )
        ).scalar_one()
    assert nullable == "NO"


# --------------------------------------------------------------------------
# "Deliberately not scored" is distinct from "scored low"
# --------------------------------------------------------------------------


def _seed_signal_lineage(engine: Engine) -> dict[str, uuid.UUID]:
    suffix = uuid.uuid4().hex[:8]
    with engine.begin() as conn:
        ids = {
            "security": conn.execute(
                text("INSERT INTO security_identity (name) VALUES ('Sig Co') RETURNING id")
            ).scalar_one()
        }
        for table, key in [
            ("target_model_version", "target_model"),
            ("feature_schema_version", "feature_schema"),
            ("scoring_configuration", "scoring"),
            ("detection_configuration", "detection"),
        ]:
            ids[key] = conn.execute(
                text(
                    f"INSERT INTO {table} (version_label, definition, content_checksum) "
                    "VALUES (:label, '{}'::jsonb, 'sha') RETURNING id"
                ),
                {"label": f"{table}-{suffix}"},
            ).scalar_one()
        ids["universe"] = conn.execute(
            text(
                "INSERT INTO universe_version (version_label, definition, as_of_date) "
                "VALUES (:label, '{}'::jsonb, now()) RETURNING id"
            ),
            {"label": f"universe-{suffix}"},
        ).scalar_one()
        ids["snapshot"] = conn.execute(
            text(
                "INSERT INTO data_snapshot (version_label, as_of_time, definition, content_checksum) "
                "VALUES (:label, now(), '{}'::jsonb, 'sha') RETURNING id"
            ),
            {"label": f"snapshot-{suffix}"},
        ).scalar_one()
    return ids


_INSERT_SIGNAL = text(
    "INSERT INTO signals (security_id, event_time, evidence_status, argus_score, confidence, "
    "opportunity_score, risk_score, probability, probability_definition, "
    "target_model_version_id, feature_schema_version_id, data_snapshot_id, "
    "scoring_configuration_id, universe_version_id, detection_configuration_id) "
    "VALUES (:security, now(), :status, :argus, :confidence, :opportunity, :risk, "
    ":probability, :probability_definition, :target_model, :feature_schema, :snapshot, "
    ":scoring, :universe, :detection)"
)


def _signal_params(ids: dict[str, uuid.UUID], **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "security": ids["security"],
        "target_model": ids["target_model"],
        "feature_schema": ids["feature_schema"],
        "snapshot": ids["snapshot"],
        "scoring": ids["scoring"],
        "universe": ids["universe"],
        "detection": ids["detection"],
        "status": "SCORED",
        "argus": 91,
        "confidence": 54,
        "opportunity": 70,
        "risk": 30,
        "probability": None,
        "probability_definition": None,
    }
    params.update(overrides)
    return params


def test_scored_signal_accepts_all_five_numbers(engine: Engine):
    """A high score with low confidence is valid and meaningful, not a contradiction."""
    ids = _seed_signal_lineage(engine)
    with engine.begin() as conn:
        conn.execute(
            _INSERT_SIGNAL,
            _signal_params(
                ids,
                probability=62,
                probability_definition="+10% before -5% within 60 trading days",
            ),
        )


def test_insufficient_evidence_signal_must_have_no_scores(engine: Engine):
    ids = _seed_signal_lineage(engine)
    with engine.begin() as conn:
        conn.execute(
            _INSERT_SIGNAL,
            _signal_params(
                ids,
                status="INSUFFICIENT_EVIDENCE",
                argus=None,
                confidence=None,
                opportunity=None,
                risk=None,
            ),
        )


def test_insufficient_evidence_signal_rejects_a_forced_score(engine: Engine):
    """Never force a numeric score onto insufficient evidence."""
    ids = _seed_signal_lineage(engine)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(_INSERT_SIGNAL, _signal_params(ids, status="INSUFFICIENT_EVIDENCE"))


def test_scored_signal_rejects_missing_numbers(engine: Engine):
    ids = _seed_signal_lineage(engine)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(_INSERT_SIGNAL, _signal_params(ids, argus=None))


def test_probability_requires_its_definition(engine: Engine):
    """A probability without the outcome it refers to is meaningless."""
    ids = _seed_signal_lineage(engine)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            _INSERT_SIGNAL,
            _signal_params(ids, probability=62, probability_definition=None),
        )


def test_scores_outside_zero_to_one_hundred_are_rejected(engine: Engine):
    ids = _seed_signal_lineage(engine)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(_INSERT_SIGNAL, _signal_params(ids, argus=101))


# --------------------------------------------------------------------------
# Structural expectations other modules rely on
# --------------------------------------------------------------------------


def test_setups_has_no_mutable_status_column(engine: Engine):
    """Status is derived from setup_events; a column here would undo that."""
    assert "status" not in metadata.tables["setups"].columns


def test_signals_carry_the_full_lineage_tuple(engine: Engine):
    """Reproducibility depends on every one of these being recorded."""
    expected = {
        "target_model_version_id",
        "feature_schema_version_id",
        "data_snapshot_id",
        "scoring_configuration_id",
        "universe_version_id",
        "detection_configuration_id",
    }
    assert expected <= {column.name for column in metadata.tables["signals"].columns}


def test_signals_have_all_seven_score_components(engine: Engine):
    components = {
        column.name
        for column in metadata.tables["signals"].columns
        if column.name.startswith("component_")
    }
    assert components == {
        "component_pattern_quality",
        "component_historical_evidence",
        "component_market_regime",
        "component_volume_liquidity",
        "component_volatility_structure",
        "component_fundamental_context",
        "component_risk_reward",
    }


def test_material_event_type_is_not_an_enum(engine: Engine):
    """New event kinds (litigation, M&A) must not require a migration."""
    with engine.connect() as conn:
        data_type = conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'pending_material_events' AND column_name = 'event_type'"
            )
        ).scalar_one()
    assert data_type == "text"


def test_similarity_keeps_cross_asset_and_same_asset_separate(engine: Engine):
    """They are independent evidence and must be storable side by side."""
    ids = _seed_signal_lineage(engine)
    with engine.begin() as conn:
        for scope in ("CROSS_ASSET", "SAME_ASSET"):
            conn.execute(
                text(
                    "INSERT INTO historical_similarity_results "
                    "(security_id, event_time, scope, similar_setup_count, "
                    "similarity_distribution, feature_schema_version_id, data_snapshot_id) "
                    "VALUES (:security, :when, :scope, 12, '{}'::jsonb, :fs, :snap)"
                ),
                {
                    "security": ids["security"],
                    "when": "2024-01-02T00:00:00+00:00",
                    "scope": scope,
                    "fs": ids["feature_schema"],
                    "snap": ids["snapshot"],
                },
            )

        stored = conn.execute(
            text(
                "SELECT count(DISTINCT scope) FROM historical_similarity_results "
                "WHERE security_id = :security"
            ),
            {"security": ids["security"]},
        ).scalar_one()
    assert stored == 2


# --------------------------------------------------------------------------
# Ticker-history validity ranges (Module 04 Part 0 correction)
# --------------------------------------------------------------------------


def _new_security(engine: Engine, name: str) -> uuid.UUID:
    with engine.begin() as conn:
        return conn.execute(
            text("INSERT INTO security_identity (name) VALUES (:n) RETURNING id"), {"n": name}
        ).scalar_one()


_INSERT_TICKER = text(
    "INSERT INTO security_ticker_history (security_id, ticker, exchange, valid_from, valid_to) "
    "VALUES (:security, :ticker, 'NASDAQ', :valid_from, :valid_to)"
)


def test_one_security_cannot_hold_two_tickers_at_once(engine: Engine):
    security_id = _new_security(engine, "Overlap Co")
    ticker = f"OV{uuid.uuid4().hex[:6].upper()}"
    with engine.begin() as conn:
        conn.execute(
            _INSERT_TICKER,
            {
                "security": security_id,
                "ticker": ticker,
                "valid_from": "2010-01-01T00:00:00+00:00",
                "valid_to": "2015-01-01T00:00:00+00:00",
            },
        )

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            _INSERT_TICKER,
            {
                "security": security_id,
                "ticker": f"{ticker}B",
                "valid_from": "2014-01-01T00:00:00+00:00",
                "valid_to": "2016-01-01T00:00:00+00:00",
            },
        )


def test_one_ticker_cannot_map_to_two_securities_at_once(engine: Engine):
    """Otherwise "who was AAPL on 2013-06-01" has more than one answer."""
    first = _new_security(engine, "First Holder")
    second = _new_security(engine, "Second Holder")
    ticker = f"RC{uuid.uuid4().hex[:6].upper()}"
    with engine.begin() as conn:
        conn.execute(
            _INSERT_TICKER,
            {
                "security": first,
                "ticker": ticker,
                "valid_from": "2010-01-01T00:00:00+00:00",
                "valid_to": "2015-01-01T00:00:00+00:00",
            },
        )

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            _INSERT_TICKER,
            {
                "security": second,
                "ticker": ticker,
                "valid_from": "2012-01-01T00:00:00+00:00",
                "valid_to": "2013-01-01T00:00:00+00:00",
            },
        )


def test_ticker_may_be_recycled_after_the_previous_holder_delists(engine: Engine):
    """Recycling is legitimate and must stay possible — only overlap is barred."""
    first = _new_security(engine, "Delisted Co")
    second = _new_security(engine, "New Holder")
    ticker = f"RE{uuid.uuid4().hex[:6].upper()}"
    with engine.begin() as conn:
        conn.execute(
            _INSERT_TICKER,
            {
                "security": first,
                "ticker": ticker,
                "valid_from": "2010-01-01T00:00:00+00:00",
                "valid_to": "2015-01-01T00:00:00+00:00",
            },
        )
        conn.execute(
            _INSERT_TICKER,
            {
                "security": second,
                "ticker": ticker,
                "valid_from": "2015-01-01T00:00:00+00:00",
                "valid_to": None,
            },
        )

    with engine.connect() as conn:
        holders = conn.execute(
            text("SELECT count(*) FROM security_ticker_history WHERE ticker = :t"), {"t": ticker}
        ).scalar_one()
    assert holders == 2


# --------------------------------------------------------------------------
# The ninth market state
# --------------------------------------------------------------------------


def test_unclassified_is_an_available_market_state(engine: Engine):
    """A security with too little history must not be forced into DOWN_TREND."""
    with engine.connect() as conn:
        labels = (
            conn.execute(
                text(
                    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'market_state_enum' ORDER BY e.enumsortorder"
                )
            )
            .scalars()
            .all()
        )

    assert len(labels) == 9
    assert labels[0] == "UNCLASSIFIED"
    assert set(labels) == {member.value for member in MarketState}
