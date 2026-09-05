"""The complete ARGUS schema.

Importing this package registers every table on the shared
`infra.db.metadata.metadata` object, which is what Alembic diffs against.
Import order matters only in that foreign keys are resolved by name at
DDL-emit time, so the modules can be imported in any order.
"""

from infra.db.metadata import metadata
from infra.db.schema import (
    canonical,
    identity,
    ingestion,
    intelligence,
    live_scanner,
    news,
    news_signals,
    ownership_signals,
    public_stats,
    sec_filings,
    setups,
    telegram,
    terminal_data,
    users,
    validation,
    versioning,
)

__all__ = [
    "canonical",
    "identity",
    "ingestion",
    "intelligence",
    "live_scanner",
    "news",
    "news_signals",
    "ownership_signals",
    "public_stats",
    "sec_filings",
    "metadata",
    "setups",
    "telegram",
    "terminal_data",
    "users",
    "validation",
    "versioning",
]
