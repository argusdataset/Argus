"""The secrets re-audit, run against the real repository and proven against
planted fixtures. This is the table Module 24's report presents.
"""

from __future__ import annotations

from pathlib import Path

from infra.security.secrets_audit import (
    REPO_ROOT,
    run_secrets_audit,
    scan_file_for_credentials,
    scan_hardcoded_credentials,
    scan_secret_provider_bypass,
    scan_tracked_env_files,
)

CREDENTIAL_FIXTURE = Path("tests/fixtures/security/planted_credential.py")


def test_the_full_audit_passes_against_the_real_repository():
    """The result this module's report presents as the audit table."""
    report = run_secrets_audit()

    failures = [check.as_dict() for check in report.checks if not check.passed]
    assert report.passed, failures


def test_no_hardcoded_credential_anywhere_in_source():
    result = scan_hardcoded_credentials()

    assert result.passed, result.evidence


def test_no_env_file_besides_the_example_is_tracked():
    result = scan_tracked_env_files()

    assert result.passed, result.evidence


def test_no_environment_secret_read_bypasses_secretsprovider():
    result = scan_secret_provider_bypass()

    assert result.passed, result.evidence


def test_the_documented_migration_url_override_is_the_one_exception(monkeypatch):
    """Confirmed by removing the exemption and watching the scan find it —
    proof the check is real rather than a hardcoded pass.
    """
    import infra.security.secrets_audit as module

    monkeypatch.setattr(module, "DOCUMENTED_ENVIRON_EXCEPTION", ("nonexistent/file.py", "X"))

    result = scan_secret_provider_bypass()

    assert result.passed is False
    assert any("infra/db/migrations/env.py" in item for item in result.evidence)


# --------------------------------------------------------------------------
# Proof the scanners work: planted fixtures, matching this repo's
# established pattern (Module 24's log-scrubbing test does the same).
# --------------------------------------------------------------------------


def test_the_credential_scanner_catches_a_planted_hardcoded_key():
    assert CREDENTIAL_FIXTURE.exists()

    lines = scan_file_for_credentials(CREDENTIAL_FIXTURE)

    assert lines != []


def test_the_credential_scanner_does_not_flag_argus_own_self_naming_codes():
    """`INVALID_CREDENTIALS = "INVALID_CREDENTIALS"` is a code naming
    itself, ARGUS's error-code convention throughout `errors.py` — an
    opaque secret is never equal to the name of the variable holding it.
    """
    lines = scan_file_for_credentials(REPO_ROOT / "services/identity/errors.py")

    assert lines == []


def test_the_credential_scanner_does_not_flag_the_secret_key_name_constants():
    """`DATABASE_PASSWORD_SECRET = "DATABASE_PASSWORD"` names which key
    `SecretsProvider` resolves — it is not a password.
    """
    lines = scan_file_for_credentials(REPO_ROOT / "infra/db/connection.py")

    assert lines == []


def test_the_env_gitignore_check_would_catch_a_missing_rule(tmp_path):
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    _init_bare_repo(tmp_path)

    result = scan_tracked_env_files(tmp_path)

    assert result.passed is False
    assert "missing" in result.detail


def test_the_env_gitignore_check_would_catch_a_tracked_env_file(tmp_path):
    import subprocess

    (tmp_path / ".gitignore").write_text(".env\n.env.*\n")
    (tmp_path / ".env").write_text("SECRET=nope\n")
    _init_bare_repo(tmp_path)
    subprocess.run(["git", "add", "-f", ".env"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "oops"], cwd=tmp_path, check=True)

    result = scan_tracked_env_files(tmp_path)

    assert result.passed is False
    assert ".env" in result.evidence


def _init_bare_repo(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@argus.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial", "--allow-empty"], cwd=path, check=True)
