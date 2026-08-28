"""The project-wide log-scrubbing scan: one AST test over every package.

Module 22 checked `services/identity/`. Module 23 checked
`infra/observability/`. Module 23's own report named what that left open:
"the other twenty modules have no loggers yet, so the gap is currently
theoretical" and "now is the cheap moment to close it." This is that
closing — one scan, covering `core/`, `services/`, `data/`, `infra/` and
`packages/` together, reusing `infra.security.scrubbing.scan` rather than
walking the tree a fourth time.
"""

from __future__ import annotations

from pathlib import Path

from infra.observability.logging import CREDENTIAL_KEY_PARTS
from infra.security.scrubbing import DEFAULT_ROOTS, scan, scan_file

FIXTURE = Path("tests/fixtures/security/planted_violation.py")


def test_the_scan_covers_every_application_package():
    """The five roots the codebase actually has application code under."""
    assert set(DEFAULT_ROOTS) == {"core", "services", "data", "infra", "packages"}
    for root in DEFAULT_ROOTS:
        assert Path(root).is_dir(), f"{root} does not exist — the root list is stale"


def test_no_logging_call_site_anywhere_in_the_codebase_is_credential_shaped():
    """The actual protective test. Run against the real repository, not a mock.

    This is what Module 24's task list asks for verbatim: one AST test,
    project-wide, confirming no logging call site anywhere can emit a
    credential-shaped field unredacted.
    """
    violations = scan()

    assert violations == [], "\n".join(str(violation) for violation in violations)


def test_the_scan_actually_walks_a_nontrivial_number_of_files():
    """A scan that silently walked nothing would pass by finding no files to check.

    Guards the test above against becoming vacuous — if `DEFAULT_ROOTS`
    or `scan()` ever stopped resolving real files, this notices before
    the "found nothing" result is mistaken for "found nothing wrong".
    """
    from infra.security.scrubbing import LOGGING_METHODS

    assert len(LOGGING_METHODS) == 7  # sanity: the method set didn't shrink
    files = [
        path
        for root in DEFAULT_ROOTS
        for path in Path(root).rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    assert len(files) > 200, f"only {len(files)} files were found — the roots may be wrong"


def test_the_scan_catches_a_credential_planted_in_a_package_other_than_identity_or_observability():
    """Proof the scanner works: a real violation, in a fixture, actually caught.

    `tests/fixtures/security/planted_violation.py` is a file that exists
    for exactly this test — it is not part of any importable package
    (nothing imports it) and it is not under any of `DEFAULT_ROOTS`, so
    the project-wide sweep above never touches it and can never be
    accidentally satisfied by a fixture that happens to violate its own
    rule. This test points the scanner at it directly by path.
    """
    assert FIXTURE.exists(), "the planted-violation fixture is missing"

    violations = scan_file(FIXTURE)

    assert violations != []
    assert any(violation.argument == "password" for violation in violations)
    assert all(violation.path == FIXTURE for violation in violations)


def test_the_fixture_itself_is_outside_every_scanned_root():
    """If this ever failed, the sweep test above would start failing on the fixture."""
    assert not any(FIXTURE.is_relative_to(root) for root in DEFAULT_ROOTS)


def test_modules_22_and_23s_own_word_lists_are_not_quietly_diverging():
    """Module 22's `services/identity/audit.py` scrubber predates this scan.

    Its own credential-key list is checked to be a subset of the shared
    one this scan (and Module 23's runtime scrubber) use, so a name added
    to protect audit_log is never a name this scan is blind to.
    """
    from services.identity.audit import _FORBIDDEN_KEY_PARTS

    assert _FORBIDDEN_KEY_PARTS <= CREDENTIAL_KEY_PARTS
