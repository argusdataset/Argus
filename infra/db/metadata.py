"""Shared SQLAlchemy metadata, naming conventions, and column helpers.

ARGUS uses SQLAlchemy **Core** (`Table` objects), not the declarative ORM.
Rationale: Alembic needs a `MetaData` target to diff against, and Core
gives exactly that with nothing extra. The ORM's mapper/session machinery
would be unused weight here — no module in the current roadmap needs
object-relational mapping, and later modules that want it can map onto
these tables without the schema being defined in ORM terms.
"""

from __future__ import annotations

from enum import EnumMeta

from sqlalchemy import Column, DateTime, Enum, MetaData

# Explicit naming convention so Alembic emits stable, predictable
# constraint names — without it, downgrades cannot reliably drop
# constraints the database named itself.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


def pg_enum(enum_class: EnumMeta, name: str) -> Enum:
    """A native PostgreSQL enum type whose labels are the enum's *values*.

    SQLAlchemy defaults to persisting enum member **names**, which would
    store `FalsePositiveType.A_NO_PATTERN` as `'A_NO_PATTERN'` rather
    than the `'A'` the spec calls for. Pinning `values_callable` to
    `.value` keeps the database labels and the Python values identical
    for every enum, so the two can never drift.
    """
    return Enum(
        enum_class,
        name=name,
        native_enum=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


def pit_columns() -> list[Column]:
    """The four point-in-time timestamps every canonical data row carries.

    All four are non-nullable. Collapsing them into one — or allowing a
    NULL — destroys the guarantee that a signal computed "as of" a past
    date could not have seen data that did not exist yet, and that
    guarantee cannot be recovered later without reprocessing everything.

    - event_time:        when the real-world event occurred (bar close,
                         fiscal quarter end)
    - observation_time:  when the value was first observed/reported
                         (e.g. the earnings release timestamp)
    - availability_time: when it became available to ARGUS specifically,
                         accounting for provider lag. This is the column
                         Module 07's PIT enforcement filters on: a query
                         "as of X" may only see rows with
                         availability_time <= X.
    - ingestion_time:    when ARGUS actually stored it

    Returned as fresh Column objects on each call because a Column
    instance can only belong to one Table.
    """
    return [
        Column("event_time", DateTime(timezone=True), nullable=False),
        Column("observation_time", DateTime(timezone=True), nullable=False),
        Column("availability_time", DateTime(timezone=True), nullable=False),
        Column("ingestion_time", DateTime(timezone=True), nullable=False),
    ]
