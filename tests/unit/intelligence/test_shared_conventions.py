"""The extraction: are the three conventions actually shared now?

Module 20 imported `error_payload` and `Unavailable` from Module 19 and
flagged that as the wrong shape — the public stats service depending on
the Terminal for reasons unrelated to the Terminal. Module 21 is the third
consumer, so the conventions moved to `services/shared/`.

These tests assert the move is real rather than aliased: all three
services import from the shared location, none imports the envelope from
another service, and the wire formats Modules 19 and 20 already published
are unchanged.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import services.intelligence as intelligence_package
import services.public_stats as public_stats_package
import services.shared as shared_package
import services.terminal as terminal_package

SERVICES = {
    "terminal": Path(terminal_package.__file__).parent,
    "public_stats": Path(public_stats_package.__file__).parent,
    "intelligence": Path(intelligence_package.__file__).parent,
}
SHARED_ROOT = Path(shared_package.__file__).parent

#: What lives in the shared location now.
SHARED_NAMES = {"error_payload", "ApiError", "Unavailable", "Freshness", "Provenance"}


def _imports(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    tree = ast.parse(path.read_text())
    found: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, tuple(alias.name for alias in node.names)))
    return found


def test_the_shared_package_exists_and_exports_all_three_conventions():
    assert (
        set(shared_package.__all__)
        | {name for name in dir(shared_package) if not name.startswith("_")}
        >= SHARED_NAMES
    )
    assert shared_package.error_payload("A", "b") == {
        "error": {"code": "A", "message": "b", "detail": {}}
    }


@pytest.mark.parametrize("service", sorted(SERVICES), ids=sorted(SERVICES))
def test_no_service_imports_the_conventions_from_another_service(service: str):
    """The specific thing Module 20 flagged.

    A chain of cross-service imports is what this extraction ends: each
    service reaches `services/shared/` directly, and none reaches through
    another.
    """
    root = SERVICES[service]
    others = {f"services.{name}" for name in SERVICES if name != service}

    offenders = []
    for path in sorted(root.glob("*.py")):
        for module, names in _imports(path):
            if any(module.startswith(other) for other in others) and SHARED_NAMES & set(names):
                offenders.append((path.name, module, names))

    assert not offenders, (
        f"{service} imports a shared convention from another service: {offenders}. "
        "They live in services/shared/ precisely so this chain does not form."
    )


def test_module_19_imports_the_envelope_from_shared():
    from services.shared.errors import ApiError
    from services.shared.errors import error_payload as shared_payload
    from services.terminal.errors import TerminalError, error_payload

    assert error_payload is shared_payload
    assert issubclass(TerminalError, ApiError)


def test_module_20_imports_the_envelope_from_shared():
    from services.public_stats.errors import PublicStatsError, error_payload
    from services.shared.errors import ApiError
    from services.shared.errors import error_payload as shared_payload

    assert error_payload is shared_payload
    assert issubclass(PublicStatsError, ApiError)


def test_module_21_imports_the_envelope_from_shared():
    from services.intelligence.errors import IntelligenceError
    from services.shared.errors import ApiError

    assert issubclass(IntelligenceError, ApiError)


def test_each_service_keeps_its_own_error_class_rather_than_sharing_one():
    """Subclassed, not aliased. Two services sharing an exception class
    would mean one service's handler silently answering for the other."""
    from services.intelligence.errors import IntelligenceError
    from services.public_stats.errors import PublicStatsError
    from services.terminal.errors import TerminalError

    classes = {TerminalError, PublicStatsError, IntelligenceError}

    assert len(classes) == 3
    assert not isinstance(TerminalError("a", "b"), PublicStatsError)
    assert not isinstance(PublicStatsError("a", "b"), IntelligenceError)


def test_unavailable_is_one_class_across_every_service():
    from services.intelligence.schemas import Unavailable as intelligence_unavailable
    from services.public_stats.schemas import Unavailable as stats_unavailable
    from services.shared.schemas import Unavailable as shared_unavailable
    from services.terminal.schemas import Unavailable as terminal_unavailable

    assert (
        terminal_unavailable is stats_unavailable is intelligence_unavailable is shared_unavailable
    )


def test_freshness_is_one_class_across_every_service():
    from services.intelligence.schemas import Freshness as intelligence_freshness
    from services.public_stats.schemas import Freshness as stats_freshness
    from services.shared.schemas import Freshness as shared_freshness

    assert intelligence_freshness is stats_freshness is shared_freshness


def test_provenance_is_shared_as_a_base_and_subclassed_per_service():
    """The one that could not move wholesale.

    What provenance *means* is identical everywhere; the identifiers are
    not — a public statistic cites approved runs, a score cites
    configuration versions. Flattening both into one `sources` dict would
    have shared more and said less, and would have changed Module 20's
    already-published response shape.
    """
    from services.intelligence.schemas import IntelligenceProvenance
    from services.public_stats.schemas import StatsProvenance
    from services.shared.schemas import Provenance

    assert issubclass(StatsProvenance, Provenance)
    assert issubclass(IntelligenceProvenance, Provenance)
    assert "note" in Provenance.model_fields
    # And the published shapes are intact.
    assert {"approved_runs", "approved_live_windows"} <= set(StatsProvenance.model_fields)
    assert "scoring_configuration_id" in IntelligenceProvenance.model_fields


def test_module_20s_published_response_shape_did_not_change():
    """The extraction was supposed to touch import lines, not contracts.

    Module 20's charts are already specified as carrying these exact
    provenance keys; a reader of that API must not have to change.
    """
    from services.public_stats.schemas import ChartResponse

    provenance = ChartResponse.model_fields["provenance"].annotation

    assert set(provenance.model_fields) >= {
        "approved_runs",
        "approved_live_windows",
        "note",
    }


def test_the_shared_package_holds_conventions_and_nothing_a_service_decides():
    """A shared package that grew logic would become a place two services
    could disagree about behaviour rather than about shape."""
    files = {path.name for path in SHARED_ROOT.glob("*.py")}

    assert files == {"__init__.py", "errors.py", "schemas.py"}
    for path in SHARED_ROOT.glob("*.py"):
        for module, _names in _imports(path):
            assert not module.startswith("core."), (
                f"{path.name} imports from core/. Shared response shapes must not depend "
                "on ARGUS's computation modules."
            )
            if module.startswith("services.shared"):
                continue  # its own submodules
            assert not module.startswith("services."), (
                f"{path.name} imports from a service. Shared code that reached back into "
                "a service would invert the dependency it exists to fix."
            )
