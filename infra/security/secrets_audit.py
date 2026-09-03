"""The secrets re-audit, as code rather than as a claim.

Module 24's brief asks for this "matching the deployment audit's
methodology" — a table of checks and results, re-verified rather than
assumed, because substantial code has been added since anyone last
looked (Module 22's whole password/token/session surface, this module's
own additions). A narrative paragraph saying "looks fine" is exactly the
kind of claim that goes stale the next time somebody adds a config field;
a function that actually re-scans the repository does not.

## What is checked, and why these three

**No hardcoded credential.** The one way `SecretsProvider` gets bypassed
without anyone deciding to: a real value typed into source instead of
resolved. Scanned by pattern rather than trusted by inspection, because
"I read the diff and it looked fine" is exactly the review step a
hardcoded secret survives.

**`.env` files are gitignored, and none is tracked.** Two different
failures share this one check — a `.gitignore` rule that was never added,
and a rule that exists but a file was force-added past anyway (`git add
-f`). Only checking the pattern in `.gitignore` misses the second; only
checking tracked files misses the first if nothing has been committed
yet. Both are checked.

**`SecretsProvider` is the only path.** Every credential-shaped
`os.environ` or `os.getenv` read outside `packages/config/` is a second
path nobody audited — Module 22's own report named exactly this pattern
(`DotEnvSecretsProvider`/`EnvironmentSecretsProvider`) as the intended
one and only one. One narrow, named, documented exception exists —
`infra/db/migrations/env.py`'s `ARGUS_MIGRATION_DATABASE_URL` — and is
checked for by name rather than silently allowed to make the scan noisy
for anyone extending it.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infra.observability.logging import CREDENTIAL_KEY_PARTS

__all__ = [
    "REPO_ROOT",
    "CheckResult",
    "SecretsAuditReport",
    "run_secrets_audit",
    "scan_file_for_credentials",
    "scan_hardcoded_credentials",
    "scan_secret_provider_bypass",
    "scan_tracked_env_files",
]

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Roots holding application source. Matches
#: `infra.security.scrubbing.DEFAULT_ROOTS` — the same surface, a
#: different question about it.
SOURCE_ROOTS: tuple[str, ...] = ("core", "services", "data", "infra", "packages")

#: An identifier assigned a quoted, opaque-looking value: `KEY = "<16+
#: characters>"` or `"KEY": "<16+ characters>"`. Whether `KEY` is
#: credential-shaped is decided separately, against
#: `CREDENTIAL_KEY_PARTS` — the same substring match Module 22 and
#: Module 23 use, reused rather than reimplemented as a second word list
#: that could drift from theirs.
_ASSIGNMENT = re.compile(
    r"""(?x)
    ['"]? (?P<key> [A-Za-z_][A-Za-z0-9_]* ) ['"]?
    \s*[:=]\s*
    ['"] (?P<value> [A-Za-z0-9+/_\-]{16,} ) ['"]
    """
)

#: Values that are obviously not credentials even though the key they are
#: assigned to is credential-shaped — the constant *names* this codebase
#: stores those words under (`"DATABASE_PASSWORD"`, the secret's key in
#: `SecretsProvider`, not a password), and header/attribute names that
#: merely contain one of the words.
_ALLOWED_VALUES = frozenset(
    {
        "fmp_api_key",
        "database_password",
        "database_url",
        "x-argus-user",
        "authorization",
        "argus-timing-equaliser-not-a-credential",
        # Module 27's two secret *keys* and the header name Telegram
        # echoes its value back in — the same case as `fmp_api_key`
        # above: the string is what `SecretsProvider` is asked for, not
        # what it answers with.
        "telegram_bot_token",
        "telegram_webhook_secret",
        "x-telegram-bot-api-secret-token",
    }
)

#: The one documented exception to "SecretsProvider is the only path" —
#: named so the scan checks *for* it by name rather than needing a
#: growing allowlist nobody reviews.
DOCUMENTED_ENVIRON_EXCEPTION = (
    "infra/db/migrations/env.py",
    "URL_OVERRIDE_ENV_VAR",
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One row of the audit table."""

    name: str
    passed: bool
    detail: str
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class SecretsAuditReport:
    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "checks": [check.as_dict() for check in self.checks]}


def run_secrets_audit(root: Path = REPO_ROOT) -> SecretsAuditReport:
    """The whole audit, as one call. Every individual scan is also public,
    for a test that wants to check one thing rather than the report shape.
    """
    return SecretsAuditReport(
        checks=(
            scan_hardcoded_credentials(root),
            scan_tracked_env_files(root),
            scan_secret_provider_bypass(root),
        )
    )


def scan_hardcoded_credentials(root: Path = REPO_ROOT) -> CheckResult:
    """No source file under `SOURCE_ROOTS` assigns a real-looking value to
    a credential-named key. `scan_file_for_credentials` does the actual
    matching and is exposed separately so a test can point it at a
    fixture outside those roots and confirm the pattern itself works.
    """
    offenders: list[str] = []
    for source_root in SOURCE_ROOTS:
        for path in sorted((root / source_root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            offenders.extend(f"{relative}:{line}" for line in scan_file_for_credentials(path))

    return CheckResult(
        name="no_hardcoded_credentials",
        passed=not offenders,
        detail=(
            "No source file assigns a real-looking value to a credential-named key."
            if not offenders
            else f"{len(offenders)} possible hardcoded credential(s) found."
        ),
        evidence=tuple(offenders),
    )


def scan_file_for_credentials(path: Path) -> list[int]:
    """Line numbers in one file where a real-looking value is assigned to
    a credential-named key.

    The key only has to *contain* a credential-shaped word, matching
    `CREDENTIAL_KEY_PARTS`'s own substring rule — `FMP_API_KEY` and
    `DATABASE_PASSWORD_SECRET` are both real names in this codebase, and
    an exact-word match would miss every one of them.
    """
    text = path.read_text(errors="ignore")
    lines: list[int] = []
    for match in _ASSIGNMENT.finditer(text):
        key = match.group("key").lower()
        value = match.group("value").lower()
        if value in _ALLOWED_VALUES:
            continue
        # `INVALID_CREDENTIALS = "INVALID_CREDENTIALS"` — an error code
        # naming itself, ARGUS's own convention throughout errors.py.
        # An opaque secret is never equal to the name of the variable
        # holding it; a self-referential constant always is.
        if value == key:
            continue
        if any(part in key for part in CREDENTIAL_KEY_PARTS):
            lines.append(text.count("\n", 0, match.start()) + 1)
    return lines


def scan_tracked_env_files(root: Path = REPO_ROOT) -> CheckResult:
    """`.env`/`.env.*` are gitignored, and none besides `.env.example` is tracked."""
    gitignore = (root / ".gitignore").read_text() if (root / ".gitignore").exists() else ""
    rules_present = ".env" in gitignore.split() and ".env.*" in gitignore.split()

    tracked = _git_tracked_files(root)
    stray = [
        path
        for path in tracked
        if (path == ".env" or path.startswith(".env.")) and path != ".env.example"
    ]

    passed = rules_present and not stray
    detail_parts = []
    if not rules_present:
        detail_parts.append(".gitignore is missing the .env / .env.* rules")
    if stray:
        detail_parts.append(f"{len(stray)} env file(s) tracked besides .env.example")
    if not detail_parts:
        detail_parts.append(
            ".env and .env.* are gitignored, and no env file besides .env.example is tracked."
        )

    return CheckResult(
        name="env_files_gitignored",
        passed=passed,
        detail="; ".join(detail_parts),
        evidence=tuple(stray),
    )


def scan_secret_provider_bypass(root: Path = REPO_ROOT) -> CheckResult:
    """No credential-shaped `os.environ`/`os.getenv` read outside `packages/config/`
    and the one documented, named exception.

    Walked as an AST rather than matched against raw text, for the reason
    every structural test since Module 20 gives: a text or regex scan
    matches a comment or a docstring *describing* the pattern as readily
    as it matches the pattern itself — this file's own module docstring,
    which explains what is being checked for, is exactly that trap, and
    the first version of this scan flagged itself.
    """
    offenders: list[str] = []
    exception_path, exception_marker = DOCUMENTED_ENVIRON_EXCEPTION

    for source_root in SOURCE_ROOTS:
        for path in sorted((root / source_root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            if (
                relative.startswith("packages/config/")
                or relative == "infra/security/secrets_audit.py"
            ):
                continue

            try:
                tree = ast.parse(path.read_text(errors="ignore"), filename=str(path))
            except SyntaxError:
                continue

            exempted = relative == exception_path and exception_marker in path.read_text(
                errors="ignore"
            )
            if exempted:
                continue

            for node in ast.walk(tree):
                if _is_os_environ_read(node):
                    offenders.append(f"{relative}:{node.lineno}")

    return CheckResult(
        name="secrets_provider_only_path",
        passed=not offenders,
        detail=(
            "No environment secret read outside packages/config/ and the one "
            "documented exception (infra/db/migrations/env.py's test-only URL override)."
            if not offenders
            else f"{len(offenders)} environment read(s) outside SecretsProvider."
        ),
        evidence=tuple(offenders),
    )


def _is_os_environ_read(node: ast.AST) -> bool:
    """`os.environ[...]`, `os.environ.get(...)`, or `os.getenv(...)`."""
    if isinstance(node, ast.Subscript) and _is_os_attr(node.value, "environ"):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and _is_os_attr(func.value, "environ")
        ):
            return True
        if isinstance(func, ast.Attribute) and func.attr == "getenv" and _is_os_module(func.value):
            return True
    return False


def _is_os_attr(node: ast.expr, attr: str) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == attr and _is_os_module(node.value)


def _is_os_module(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "os"


def _git_tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]
