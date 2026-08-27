# Identity (Module 22)

The only service in ARGUS that issues credentials, and the module where
the system stops assuming its caller is authorized.

Every other service gets identity through exactly one function — Module
19's `current_user_id`, whose body this module replaced and whose
signature and call sites it did not.

## Files

| File | What it owns |
|---|---|
| `seam.py` | The new body of Module 19's `current_user_id` |
| `passwords.py` | Argon2id hashing, and equal timing for a missing account |
| `tokens.py` | Session tokens: 256 random bits, stored as a SHA-256 hash |
| `sessions.py` | Create, verify, revoke. What the seam calls |
| `attempts.py` | Brute-force protection, counted from an append-only log |
| `accounts.py` | Register, log in, log out, change password |
| `mfa.py` | TOTP, and the counter that makes a code one-time |
| `roles.py` | Three roles, one rank order, one check |
| `audit.py` | `audit_log` writes, and the payload scrubber |
| `schemas.py` | Request and response shapes. Two credential fields, both once-only |
| `config.py` | Every number, including a new `security` kind |
| `errors.py` | Codes that deliberately say less than they know |
| `app.py` | Routing, and the transaction policy that makes lockouts work |

## Endpoints

```
POST   /identity/register              create an account
POST   /identity/login                 sign in; returns the only copy of the token
POST   /identity/logout                end this session
GET    /identity/me                    your account
GET    /identity/sessions              your live sessions, without tokens
POST   /identity/password              change it; ends every session
POST   /identity/mfa/enroll            start TOTP enrolment
POST   /identity/mfa/verify            finish it by proving the app works
DELETE /identity/mfa/enroll            remove your second factor
GET    /identity/admin/users/{id}      admin-gated, requires a second factor
POST   /identity/admin/users/{id}/deactivate
```

## The seam, and what it cost

Module 19 promised Module 22 would replace one function body and touch
nothing else. That held:

- `current_user_id`'s signature gained one keyword argument with a
  default, so every Module 19-era caller still compiles.
- Two dependency *bodies* changed by one line each, forwarding the
  standard `Authorization` header. Neither takes a new parameter —
  both already had `request` for other reasons.
- No route signature, query or schema in Modules 19, 20 or 21 changed.
- Their 273 tests pass unmodified.

## What `stub_identity_enabled` means now

It changed from "can this service answer user-scoped requests at all" to
"is the header bypass available".

| Credential | Stub on | Stub off |
|---|---|---|
| Valid session | served | **served** |
| Invalid session | 401 | 401 |
| `X-Argus-User` only | served, logged as a bypass | 501 |
| Nothing | 401 | 501 |

Real sessions are never gated by the flag — a deployment must not be able
to switch authentication off. The 501 survives for the request that
genuinely cannot be answered. Production posture is `False`; the default
stayed `True` so Modules 19-21's suites pass unmodified, which is itself
the evidence the seam held.

## Decisions, stated rather than defaulted

**Argon2id, not bcrypt.** Memory-hardness is what costs an attacker with
GPUs. Bcrypt's cost is CPU time only, and it silently truncates at 72
bytes. Parameters are OWASP's m=19456, t=2, p=1.

**Session tokens get SHA-256, not argon2.** A slow hash defends against
guessing, and there is no guessing 256 bits of `os.urandom`. The hash is
there so a stolen `sessions` table is not a stolen set of live
credentials, which SHA-256 delivers completely for a random input.

**MFA is required for `admin`, optional for `registered_user`.** ARGUS
has no account-recovery flow. Mandating a second factor now would mean
the first person to reset their phone is permanently locked out of a
personal list of tickers. An admin's blast radius is different, so the
requirement is different — and it is enforced at the role gate rather
than at login, because an admin who cannot sign in cannot enrol. Revisit
when account recovery exists.

**A refusal commits.** `get_connection` commits on `IdentityError` and
rolls back on everything else. A failed login writes the attempt row the
lockout counts and then raises to produce a 401; under the ordinary
`engine.begin()` pattern that exception destroys the evidence, and the
account never locks. This is the single most consequential twelve lines
in the module.

**A lockout counts failures since the last success.** `login_attempts` is
append-only, so there is no counter to reset — and no counter to DELETE
either, which is the point. The history survives the reset, so an
operator can still see that an account was attacked.

## What this module does not have

- **No SSO or OAuth.** Deferred to Institutional tier.
- **No account recovery.** Its absence is what shapes the MFA decision.
- **No role-assignment endpoint.** Granting yourself `admin` over HTTP is
  the first thing worth not building; roles are set in the database.
- **No admin console.** One gated route establishes the pattern.
- **No `X-Forwarded-For` trust.** The source address comes from the
  connection. Behind a proxy that needs configuring at the ASGI layer —
  flagged for Module 23.
