"""A deliberately hardcoded credential, for the secrets-audit test to find.

Not part of any importable package; outside `SOURCE_ROOTS`, so the real,
project-wide audit never touches it — `tests/unit/security/
test_secrets_audit.py` points the scanner at this file by path instead.

The value is an arbitrary opaque string, not shaped like any real
provider's key format (no `sk_live_`, `AKIA`, `ghp_`, or similar
recognisable prefix) — it only needs to look like *a* credential to the
pattern this project's own scanner matches, not like one issued by any
actual service.
"""

FMP_API_KEY = "not-a-real-value-abcdefghijklmnopqrstuvwxyz0123456789"
