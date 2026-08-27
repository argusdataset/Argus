"""Every number this module has, isolated per Module 10's discipline.

A new kind appears here, and it earns its place. Modules 10 through 21
tagged settings `structural`, `calibratable` or `operational`, where
operational meant "bounds *how* a computation runs and provably cannot
change *what* it produces". None of those fit an argon2 cost parameter or
a lockout threshold: they do not affect any ARGUS score, but they are not
free knobs either — turning one down weakens a security property, and
that deserves a name that says so out loud rather than hiding inside
`operational` next to a page size.

So: `SECURITY`. A setting whose value is a defence. Changing one is a
risk decision, and `IdentitySettings.security()` enumerates them so a
deployment review can read the list rather than grep for it.

## Why the numbers are what they are

The argon2 parameters follow OWASP's second recommended configuration
(19 MiB, t=2, p=1) rather than the highest one available. The higher
profiles buy little against an attacker with GPUs and cost real latency
on every login, and a login that takes a second is a login people work
around. Memory is the parameter that matters against custom hardware, so
it is the one set explicitly rather than left at the library default.

Session lifetime is deliberately short-ish. A 30-day session is a
credential sitting on a laptop; twelve hours is one working day, after
which the person signs in again. There is no refresh-token machinery
because there is no mobile client to keep warm, and inventing one now
would be a second credential type with its own revocation story.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from core.model_validation_evaluation.validation.config import (
    CALIBRATABLE,
    OPERATIONAL,
    STRUCTURAL,
)

#: A setting whose value is a defence. See the module docstring.
SECURITY = "security"

KINDS: tuple[str, ...] = (STRUCTURAL, CALIBRATABLE, OPERATIONAL, SECURITY)

__all__ = [
    "CALIBRATABLE",
    "KINDS",
    "OPERATIONAL",
    "SECURITY",
    "STRUCTURAL",
    "IdentityConfig",
    "IdentitySetting",
    "IdentitySettings",
]


@dataclass(frozen=True, slots=True)
class IdentitySetting:
    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value

    def __int__(self) -> int:
        return int(self.value)


def _s(value: float, kind: str, rationale: str) -> IdentitySetting:
    return IdentitySetting(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class IdentitySettings:
    """The defences, and the three numbers that are merely operational."""

    # ---- password hashing -------------------------------------------
    argon2_time_cost: IdentitySetting = field(
        default_factory=lambda: _s(
            2.0,
            SECURITY,
            "Argon2id iterations. OWASP's m=19456,t=2,p=1 profile. Raising "
            "it multiplies every login's latency for a linear gain against "
            "an attacker who has the hashes; memory cost is the parameter "
            "that hurts custom hardware, so that is where the budget went.",
        )
    )
    argon2_memory_kib: IdentitySetting = field(
        default_factory=lambda: _s(
            19456.0,
            SECURITY,
            "19 MiB per hash. The parameter that makes GPU and ASIC "
            "cracking expensive rather than merely slow. Lowering it is the "
            "single most damaging change available in this file.",
        )
    )
    argon2_parallelism: IdentitySetting = field(
        default_factory=lambda: _s(
            1.0,
            SECURITY,
            "Lanes. One, per OWASP, because a web process hashing a login "
            "has no spare cores to give and more lanes at fixed memory "
            "weakens the memory-hardness.",
        )
    )
    min_password_length: IdentitySetting = field(
        default_factory=lambda: _s(
            12.0,
            SECURITY,
            "Twelve characters. Length is the only password rule with "
            "evidence behind it; composition rules push people towards "
            "P@ssw0rd1 and were left out deliberately.",
        )
    )

    # ---- sessions ---------------------------------------------------
    session_token_bytes: IdentitySetting = field(
        default_factory=lambda: _s(
            32.0,
            SECURITY,
            "256 bits of os.urandom per session token. Far past guessing; "
            "the number exists so a future change to it is visible.",
        )
    )
    session_lifetime_hours: IdentitySetting = field(
        default_factory=lambda: _s(
            12.0,
            SECURITY,
            "One working day. A long-lived session is a credential sitting "
            "on a laptop, and ARGUS has no refresh-token machinery to make "
            "a short one painless — twelve hours is the compromise, stated "
            "rather than defaulted.",
        )
    )

    # ---- brute force ------------------------------------------------
    max_failed_attempts: IdentitySetting = field(
        default_factory=lambda: _s(
            5.0,
            SECURITY,
            "Failures against one address inside the window before it locks. "
            "Five tolerates a person mistyping and stops an online guessing "
            "run cold; the offline attack this cannot address is what the "
            "argon2 parameters are for.",
        )
    )
    lockout_window_minutes: IdentitySetting = field(
        default_factory=lambda: _s(
            15.0,
            SECURITY,
            "How far back failures are counted. Bounded so that five "
            "mistakes spread over a year never lock anybody out.",
        )
    )
    lockout_minutes: IdentitySetting = field(
        default_factory=lambda: _s(
            15.0,
            SECURITY,
            "How long a lock holds. Long enough to make guessing "
            "uneconomic, short enough that a legitimate person waits rather "
            "than opening a support ticket — which is what makes this "
            "survivable without an account-recovery flow.",
        )
    )
    max_failed_attempts_per_address: IdentitySetting = field(
        default_factory=lambda: _s(
            25.0,
            SECURITY,
            "Failures from one source address across all accounts. Without "
            "this, per-account limits are evaded by spraying one password "
            "across ten thousand addresses — the attack per-account "
            "lockouts are famously blind to.",
        )
    )

    # ---- MFA --------------------------------------------------------
    totp_step_seconds: IdentitySetting = field(
        default_factory=lambda: _s(
            30.0,
            STRUCTURAL,
            "RFC 6238's default time step. Structural, not tunable: every "
            "authenticator app assumes thirty seconds, so changing it "
            "changes what a valid code *is* rather than how strictly one is "
            "checked.",
        )
    )
    totp_skew_steps: IdentitySetting = field(
        default_factory=lambda: _s(
            1.0,
            SECURITY,
            "Steps either side accepted, for clock drift. One means a "
            "90-second window. Zero would reject people whose phone is a "
            "few seconds off; more widens what a stolen code is worth.",
        )
    )

    # ---- operational ------------------------------------------------
    max_sessions_listed: IdentitySetting = field(
        default_factory=lambda: _s(
            50.0,
            OPERATIONAL,
            "Sessions returned when a person lists their own. Bounds the "
            "payload; the same sessions are live either way.",
        )
    )

    def as_dict(self) -> dict[str, float]:
        return {f.name: float(getattr(self, f.name).value) for f in fields(self)}

    def describe(self) -> dict[str, dict[str, Any]]:
        return {f.name: asdict(getattr(self, f.name)) for f in fields(self)}

    def security(self) -> dict[str, float]:
        """The settings whose value is a defence. Read this before changing one."""
        return {
            f.name: float(getattr(self, f.name).value)
            for f in fields(self)
            if getattr(self, f.name).kind == SECURITY
        }

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @property
    def session_lifetime_seconds(self) -> float:
        return self.session_lifetime_hours.value * 3600.0

    @property
    def lockout_window_seconds(self) -> float:
        return self.lockout_window_minutes.value * 60.0

    @property
    def lockout_seconds(self) -> float:
        return self.lockout_minutes.value * 60.0


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    """The Identity service's configuration.

    `stub_identity_enabled` is deliberately *not* here. It lives on
    `TerminalConfig`, where Module 19 put it, because it is read by the
    seam Module 19 owns — see `services/identity/seam.py` on why moving it
    would have been the wrong kind of tidiness.
    """

    name: str = "argus-identity"
    settings: IdentitySettings = field(default_factory=IdentitySettings)

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "settings": self.settings.as_dict()}

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"
