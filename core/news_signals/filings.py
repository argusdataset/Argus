"""The SEC 8-K signal: did this security file a material-event form today?

Module 28's second signal, deliberately living in the same package as the
news-volume anomaly rather than in one of its own. The two answer the same
kind of question — *did something happen around this security today* — and
this one answers it **more precisely**: an 8-K is a form the SEC requires
a company to file when something material happens, where a headline count
is a proxy for attention that may or may not have a cause behind it.

## Why `raised` is a plain `bool` here, and tri-state next door

`NewsVolumeSignal.raised` is `bool | None` because "unusually many
articles" is a comparison against a baseline, and a security without
enough history has no baseline to compare against — reporting `False`
there would be claiming a measurement nobody made.

Nothing of the kind applies to a filing. "Was an 8-K filed on this date"
is a discrete, binary fact: the query either finds a filing row for the
day or it does not, and a security with no filing history at all is a
security that did not file today. So there is no `MissReason` in this
file and no `None` in the column — see `infra/db/schema/sec_filings.py`.

## Field names are unconfirmed, so aliases are tried and recorded

FMP's Ultimate plan was not purchased when this was written, so the exact
JSON keys `/stable/8k-latest` and `/stable/search-by-symbol` return are
documented but unverified. `FIELD_ALIASES` therefore lists several
plausible spellings per concept and `translate_filing` records which one
resolved in the stored row's `detail`, exactly as
`core/candidate_detection/eligibility/bankruptcy.py` does for the
balance-sheet fields it could not verify either.

Item numbers get the same treatment and one extra allowance: some feeds
publish them as a dedicated field ("5.02,1.01"), others only inside prose
("Item 5.02 Departure of Directors..."), so both are read with one
regular expression. When nothing resolves, the list is empty and the
signal still raises — *that* a filing happened is the signal; *which*
item it disclosed is the colour, and a missing item number must not
suppress a real event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.news_signals.config import FilingIngestionSettings, NewsSignalConfig
from data.canonical_model.pit import PitTimestamps
from data.normalization.persistence import DEFAULT_BATCH_SIZE, WriteResult
from data.provider_adapters.fmp.models import SecFiling
from infra.db.schema.sec_filings import sec_filing_signals, sec_filings

__all__ = [
    "FIELD_ALIASES",
    "FiledFilings",
    "SecFilingSignal",
    "StoredFiling",
    "assess_filing_batch",
    "evaluate_filing",
    "filings_filed_on",
    "store_filing_signals",
    "translate_filing",
    "write_filings",
]

#: Accepted spellings per concept, tried in order. See the module
#: docstring on why this is not a confirmed list.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "filed_at": ("acceptedDate", "filingDate", "fillingDate", "date", "publishedDate"),
    "link": ("finalLink", "link", "url"),
    "items": ("items", "item", "description", "title"),
    "form_type": ("formType", "type", "form"),
}

#: An SEC item number: one or two digits, a dot, two digits ("5.02",
#: "1.01"). Matched inside free text as well as in a dedicated field.
_ITEM_PATTERN = re.compile(r"\b(\d{1,2}\.\d{2})\b")

#: A bare `YYYY-MM-DD` with no time part. See `_as_datetime` on why this
#: has to be recognised before ISO parsing rather than after.
_DATE_ONLY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


class FilingTranslationError(ValueError):
    """A filing cannot be stored without inventing a timestamp.

    Raised rather than defaulted, for the reason
    `data/normalization/translate.py` gives: every plausible default for
    "when was this filed" is a PIT lie, and a filing with no resolvable
    date is better rejected and reported than stored with a made-up one.
    """


@dataclass(frozen=True, slots=True)
class StoredFiling:
    """One filing, ready for `sec_filings`."""

    security_id: UUID
    pit: PitTimestamps
    form_type: str
    item_numbers: tuple[str, ...]
    link: str | None
    lineage: dict[str, Any]
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SecFilingSignal:
    """One security's same-day-filing reading for one day."""

    security_id: UUID
    scan_date: date
    as_of: datetime
    #: Never None. See the module docstring.
    raised: bool
    item_numbers: tuple[str, ...]
    config_version_label: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "security_id": str(self.security_id),
            "scan_date": self.scan_date.isoformat(),
            "as_of": self.as_of.isoformat(),
            "raised": self.raised,
            "item_numbers": list(self.item_numbers),
            "config_version_label": self.config_version_label,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class FiledFilings:
    """What one security filed on the day being assessed."""

    count: int
    item_numbers: tuple[str, ...]


# --------------------------------------------------------------------------
# Ingest: provider record -> stored row
# --------------------------------------------------------------------------


def resolve_field(payload: dict[str, Any], concept: str) -> tuple[Any, str | None]:
    """First resolvable alias for `concept`, with the key that worked.

    Returns `(None, None)` when nothing resolved, so a caller can record
    the absence rather than guess. The key comes back because a stored row
    that says *which* spelling FMP actually used turns a future schema
    surprise into a one-line correction instead of an investigation.
    """
    for alias in FIELD_ALIASES[concept]:
        value = payload.get(alias)
        if value is None or value == "":
            continue
        return value, alias
    return None, None


def extract_item_numbers(payload: dict[str, Any]) -> tuple[tuple[str, ...], str | None]:
    """The 8-K item numbers this payload mentions, and where they came from.

    Reads a dedicated field and free prose with the same regular
    expression — see the module docstring on why both. Order is preserved
    and duplicates dropped, so `("5.02", "1.01")` reads the way the filing
    lists them.
    """
    value, resolved_key = resolve_field(payload, "items")
    if value is None:
        return (), None

    found = _ITEM_PATTERN.findall(str(value))
    seen: dict[str, None] = {}
    for item in found:
        seen.setdefault(item, None)
    return tuple(seen), resolved_key


def translate_filing(
    filing: SecFiling,
    security_id: UUID,
    *,
    settings: FilingIngestionSettings | None = None,
) -> StoredFiling:
    """One provider filing record, ready to store.

    `event_time` and `observation_time` are both the filing's accepted
    timestamp: an SEC filing becomes public at the moment it is accepted,
    so unlike a fundamental (whose fiscal period ended weeks before anyone
    could read it) there is no gap between the two.
    `availability_time` is that plus the configured lag.
    """
    resolved = settings or FilingIngestionSettings()
    payload = dict(filing.raw)

    raw_filed_at, filed_at_key = resolve_field(payload, "filed_at")
    filed_at = _as_datetime(raw_filed_at)
    if filed_at is None:
        raise FilingTranslationError(
            f"Filing for {filing.symbol!r} has no resolvable filing timestamp. Tried "
            f"{FIELD_ALIASES['filed_at']}; refusing to invent one."
        )

    link, link_key = resolve_field(payload, "link")
    items, items_key = extract_item_numbers(payload)
    form_type = filing.form_type
    if not form_type:
        raw_form, _ = resolve_field(payload, "form_type")
        form_type = str(raw_form) if raw_form else "8-K"

    return StoredFiling(
        security_id=security_id,
        pit=PitTimestamps.derive(
            event_time=filed_at,
            observation_time=filed_at,
            ingestion_time=filing.provenance.fetched_at,
            lag=resolved.availability_lag,
        ),
        form_type=str(form_type),
        item_numbers=items,
        link=str(link) if link else None,
        lineage={
            "provider": filing.provenance.provider,
            "endpoint": filing.provenance.endpoint,
            "from_cache": filing.provenance.from_cache,
            # Which spelling actually resolved, per concept. The whole
            # point of the alias table: a mismatch is then a documented
            # fact in the row rather than a silent empty column.
            "resolved_fields": {
                "filed_at": filed_at_key,
                "link": link_key,
                "items": items_key,
            },
        },
        data=payload,
    )


def write_filings(
    connection: Connection,
    filings: list[StoredFiling],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> WriteResult:
    """Insert-only, idempotent. Re-ingesting a day inserts nothing new.

    Two statements rather than one, matching `core/ingestion/news_writer.py`:
    `sec_filings` carries two *partial* unique indexes — on the link where
    one exists, on `(form_type, event_time)` where it does not — and
    Postgres infers a partial index only when the statement repeats its
    predicate.
    """
    result = WriteResult(offered=len(filings))
    if not filings:
        return result

    with_link = [_filing_values(filing) for filing in filings if filing.link]
    without_link = [_filing_values(filing) for filing in filings if not filing.link]

    result.inserted += _insert_filings(
        connection,
        with_link,
        batch_size=batch_size,
        index_elements=["security_id", "link"],
        index_where=sec_filings.c.link.isnot(None),
    )
    result.inserted += _insert_filings(
        connection,
        without_link,
        batch_size=batch_size,
        index_elements=["security_id", "form_type", "event_time"],
        index_where=sec_filings.c.link.is_(None),
    )
    return result


# --------------------------------------------------------------------------
# Assess: stored rows -> the day's reading
# --------------------------------------------------------------------------

_FILED_ON_QUERY = """
    SELECT
        security_id,
        COUNT(*) AS filing_count,
        COALESCE(
            jsonb_agg(item_numbers) FILTER (WHERE jsonb_array_length(item_numbers) > 0),
            '[]'::jsonb
        ) AS item_number_groups
    FROM sec_filings
    WHERE security_id = ANY(:security_ids)
      AND availability_time <= :as_of
      AND event_time >= :day_start
      AND event_time < :day_end
    GROUP BY security_id
"""


def filings_filed_on(
    connection: Connection,
    security_ids: list[UUID],
    *,
    scan_date: date,
    as_of: datetime,
) -> dict[UUID, FiledFilings]:
    """What each security filed on `scan_date`, in one query.

    One query for however many securities, never one per security — the
    same discipline `core/news_signals/queries.py` follows.

    PIT-filtered on `availability_time <= as_of` like every canonical read
    in ARGUS: a filing accepted this evening but not yet fetchable at the
    cutoff being asked about must not appear in that cutoff's answer.

    A security with no filing that day is simply absent from the result;
    callers read a missing key as "nothing filed", which is a real answer
    here rather than an absence of one.
    """
    if not security_ids:
        return {}

    day_start = datetime.combine(scan_date, time.min, tzinfo=UTC)
    rows = connection.execute(
        text(_FILED_ON_QUERY),
        {
            "security_ids": security_ids,
            "as_of": as_of,
            "day_start": day_start,
            "day_end": day_start + timedelta(days=1),
        },
    ).all()

    filed: dict[UUID, FiledFilings] = {}
    for row in rows:
        items: dict[str, None] = {}
        for group in row.item_number_groups or []:
            for item in group or []:
                items.setdefault(str(item), None)
        filed[row.security_id] = FiledFilings(
            count=int(row.filing_count), item_numbers=tuple(items)
        )
    return filed


def evaluate_filing(
    *,
    security_id: UUID,
    scan_date: date,
    as_of: datetime,
    filed: FiledFilings | None,
    config_version_label: str,
) -> SecFilingSignal:
    """The binary decision, from what the query found.

    `filed=None` means the security filed nothing that day, and that is a
    measured `False` — not an undetermined reading. See the module
    docstring on why this differs from the volume-anomaly signal it sits
    beside.
    """
    count = filed.count if filed else 0
    items = filed.item_numbers if filed else ()
    return SecFilingSignal(
        security_id=security_id,
        scan_date=scan_date,
        as_of=as_of,
        raised=count > 0,
        item_numbers=items,
        config_version_label=config_version_label,
        detail={
            "filing_count": count,
            "item_numbers": list(items),
            "scan_date": scan_date.isoformat(),
            # Recorded explicitly so a reader of a stored row is not left
            # wondering whether an empty item list meant "no items" or
            # "items could not be read" — see the module docstring.
            "items_resolved": bool(items),
        },
    )


def assess_filing_batch(
    connection: Connection,
    security_ids: list[UUID],
    *,
    scan_date: date,
    as_of: datetime,
    config: NewsSignalConfig | None = None,
) -> dict[UUID, SecFilingSignal]:
    """Every security's filing reading for `scan_date`, from one query."""
    if not security_ids:
        return {}

    resolved = config or NewsSignalConfig()
    version_label = resolved.version_label()
    filed = filings_filed_on(connection, security_ids, scan_date=scan_date, as_of=as_of)

    return {
        security_id: evaluate_filing(
            security_id=security_id,
            scan_date=scan_date,
            as_of=as_of,
            filed=filed.get(security_id),
            config_version_label=version_label,
        )
        for security_id in security_ids
    }


def store_filing_signals(
    connection: Connection,
    signals: list[SecFilingSignal],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Upsert one day's readings. A same-day rerun overwrites, never doubles."""
    written = 0
    for start in range(0, len(signals), batch_size):
        batch = signals[start : start + batch_size]
        if not batch:
            continue
        statement = insert(sec_filing_signals).values(
            [
                {
                    "security_id": signal.security_id,
                    "signal_date": signal.scan_date,
                    "raised": signal.raised,
                    "item_numbers": list(signal.item_numbers),
                    "config_version_label": signal.config_version_label,
                    "computed_at": signal.as_of,
                    "detail": signal.detail,
                }
                for signal in batch
            ]
        )
        result = connection.execute(
            statement.on_conflict_do_update(
                constraint="uq_sec_filing_signal_security_date",
                set_={
                    "raised": statement.excluded.raised,
                    "item_numbers": statement.excluded.item_numbers,
                    "config_version_label": statement.excluded.config_version_label,
                    "computed_at": statement.excluded.computed_at,
                    "detail": statement.excluded.detail,
                },
            ).returning(sec_filing_signals.c.id)
        )
        written += len(result.fetchall())
    return written


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _as_datetime(value: Any) -> datetime | None:
    """A provider timestamp, tolerant of the several shapes FMP uses.

    A date with no time is anchored to end-of-day rather than midnight,
    the same choice `translate_fundamental` makes for `filing_date`:
    assuming midnight would make the filing readable a full day earlier
    than it can be evidenced.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime.combine(value, time.max, tzinfo=UTC)

    raw = str(value).strip()
    if not raw:
        return None

    # Checked before ISO parsing, not after: `fromisoformat("2026-03-10")`
    # succeeds and returns midnight, which would make a filing knowable a
    # full day before it can be evidenced. The date-only case has to be
    # recognised while the string still says it is one.
    if _DATE_ONLY_PATTERN.fullmatch(raw):
        return datetime.combine(date.fromisoformat(raw), time.max, tzinfo=UTC)

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(raw[:10]), time.max, tzinfo=UTC)
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _filing_values(filing: StoredFiling) -> dict[str, Any]:
    return {
        "security_id": filing.security_id,
        **filing.pit.as_columns(),
        "form_type": filing.form_type,
        "item_numbers": list(filing.item_numbers),
        "link": filing.link,
        "lineage": filing.lineage,
        "data": filing.data,
    }


def _insert_filings(
    connection: Connection,
    rows: list[dict[str, Any]],
    *,
    batch_size: int,
    index_elements: list[str],
    index_where: Any,
) -> int:
    inserted = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        if not batch:
            continue
        statement = (
            insert(sec_filings)
            .values(batch)
            .on_conflict_do_nothing(index_elements=index_elements, index_where=index_where)
            .returning(sec_filings.c.id)
        )
        inserted += len(connection.execute(statement).fetchall())
    return inserted
