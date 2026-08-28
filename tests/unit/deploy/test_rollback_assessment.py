"""Rollback risk, as a judgement over revisions. Applying anything is integration.

> Rollback reverts the image, never the schema.

The rule holds because `migrate.py` refuses a migration the previously
running code cannot survive — so a schema that was safe for old code
*during* a deploy is equally safe for it after a rollback. These tests
are about the one thing that can break the rule: a destructive migration
that got through, by the first-deploy exemption or by an override.
"""

from __future__ import annotations

from infra.deploy.rollback import RevisionRisk, RollbackAssessment


def _assessment(*risks: RevisionRisk) -> RollbackAssessment:
    return RollbackAssessment(current="0013", target="0009", revisions=risks)


def test_an_additive_revision_does_not_block_a_code_rollback():
    """Old code cannot see a column that was only added."""
    risk = RevisionRisk(
        revision="0010", upgrade_operations=(), downgrade_operations=("drop_table",)
    )
    assert not risk.blocks_code_rollback
    assert _assessment(risk).code_rollback_safe


def test_a_revision_that_tightened_a_column_blocks_a_code_rollback():
    """The old image would start writing NULLs the schema now refuses."""
    risk = RevisionRisk(revision="0006", upgrade_operations=("alter_column",))
    assert risk.blocks_code_rollback
    assert not _assessment(risk).code_rollback_safe


def test_the_blocking_revisions_are_named_rather_than_counted():
    """An operator mid-incident needs to know which one, not how many."""
    assessment = _assessment(
        RevisionRisk(revision="0010"),
        RevisionRisk(revision="0011", upgrade_operations=("drop_column",)),
    )
    assert [risk.revision for risk in assessment.blocking] == ["0011"]


def test_a_destructive_downgrade_makes_the_schema_rollback_lossy():
    """Which is the whole reason `rollback_schema` refuses by default.

    A downgrade that drops a table does not restore a previous state; it
    destroys what the new schema was holding.
    """
    assessment = _assessment(RevisionRisk(revision="0013", downgrade_operations=("drop_table",)))
    assert assessment.schema_rollback_lossy
    assert assessment.code_rollback_safe


def test_nothing_between_target_and_head_is_safe_in_both_directions():
    assessment = RollbackAssessment(current="0013", target="0013")
    assert assessment.code_rollback_safe
    assert not assessment.schema_rollback_lossy
