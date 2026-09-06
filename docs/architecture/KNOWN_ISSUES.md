# ARGUS — Consolidated Known-Issues Register

**Compiled:** Full Integration Audit, Phase 1 "Engineering Complete", 2026-08-28
**Scope:** every item any of Modules 03-25's reports flagged as *found but not
fixed*, *deferred*, or *carried forward*, plus items this audit found that no
module report contains.

Until this document existed, that information was scattered across 25+
individual reports, each written by a session that could not see the others.
This is the single place it lives now. An entry leaves this register by being
fixed and having its fix pointed at, not by being forgotten.

Each entry carries: **who found it**, **what it is**, **current status as
verified by this audit** (not as claimed by the report that filed it), and a
severity.

Severity is about consequence-if-it-fires, not likelihood:

| | |
|---|---|
| **HIGH** | Can produce a wrong published number, lose data, or take production down. |
| **MEDIUM** | Degrades a guarantee, or removes a safety net while leaving behaviour correct today. |
| **LOW** | Operational friction, documentation drift, or a hazard with no current trigger. |

---

## A. Correctness hazards in live code

### A1. Four "latest row wins" ordering hazards — OPEN, by instruction

**Found by** Module 23 (`infra/observability/ordering.py`).
**Status:** open, deliberately. Module 24 was told to confirm-not-fix; Module 25
was told to carry forward. Both did.

`now()` is transaction-start time, not statement time, and
`gen_random_uuid()` breaks ties randomly. So two rows written in one
transaction have identical timestamps and no deterministic order, and any
`ORDER BY <timestamp> DESC LIMIT 1` over them returns either row.

The four registered instances (`safe=False` in the registry):

| Table | Consequence when it fires |
|---|---|
| `setup_outcomes` | A recomputed outcome and its original are indistinguishable by time; "the current outcome" is a coin flip. |
| `signals` | Two signals scored for one security in one transaction; the "latest score" is arbitrary. |
| `historical_similarity_results` | Same, for a re-run similarity search. |
| `public_stat_snapshots` | Two refreshes in one transaction; the served chart is arbitrary. Mitigated in practice — `refresh_public_stats` deletes before inserting per chart. |

**Verified this audit:** all four still registered, still `safe=False`, still
unfixed. The registry also holds 11 `safe=True` entries that were examined and
cleared, including Module 24's `registration_attempts` (it sums attempts rather
than taking a latest row).

**No fifth instance exists.** Verified independently rather than by trusting
the registry: every `limit(1)` in `core/`, `services/`, `infra/`, `data/` and
`packages/` was enumerated, and every source file changed after Module 23 was
re-scanned. The only hit in post-Module-23 code is a *docstring* in
`infra/deploy/scanner.py` explaining why that module refuses to write the
query — Module 25 needed "the latest universe version" and required an explicit
`ARGUS_UNIVERSE_VERSION` instead of becoming a fifth instance.

**Severity: HIGH.** Nothing has fired because nothing writes two such rows in
one transaction yet. The first bulk backfill or replay is when that changes.

---

### A2. `data_snapshot` publishers collide on two calls in one day — OPEN, and it is **two** functions, not one

**Found by** Module 25, described incompletely — its report was cut off
mid-sentence and named only one of the two occurrences. This audit finished the
investigation.

**What it actually is.** `data_snapshot` has a **unique** `version_label` and a
**non-unique** `content_checksum`:

```python
Column("version_label", Text, nullable=False, unique=True),   # <- unique
Column("content_checksum", Text, nullable=False),             # <- NOT unique
```

Both publishers compute their idempotence key and their uniqueness key at
**different granularities**:

```python
checksum      = sha256(f"{config.content_checksum()}|{as_of.isoformat()}")   # microseconds
version_label = f"{config.version_label()}@{as_of.date().isoformat()}"       # days
```

So for two calls with the same config on the same calendar date but a different
instant:

1. the checksums differ → the "already published?" lookup **misses**;
2. it proceeds to `INSERT`;
3. the labels are identical → **`UniqueViolation` on `uq_data_snapshot_version_label`**.

The function's own docstring says *"Idempotent by checksum"*. It is idempotent
only for a byte-identical `as_of`.

**The failure mode is a loud crash, not a silent overwrite.** Module 25's
cut-off note left both possibilities open; this audit settled it. That is the
better of the two, but it still means an unhandled `IntegrityError` on a path
that believes it is idempotent.

**Both occurrences, confirmed empirically against a real migrated database:**

| Function | Module | File |
|---|---|---|
| `publish_outcome_snapshot` | 16 | `core/outcome_tracking/config.py:219` |
| `publish_similarity_configuration` | 11 | `core/historical_similarity/config.py:196` |

```
Module 16  publish_outcome_snapshot
   same instant, twice    : IDEMPOTENT (same id returned)
   two instants, same day : UniqueViolation: duplicate key value violates
                            unique constraint "uq_data_snapshot_version_label"

Module 11  publish_similarity_configuration
   same instant, twice    : IDEMPOTENT (same id returned)
   two instants, same day : UniqueViolation: ... same constraint
```

The Module 11 occurrence appears in **no module report at all**. It is a
finding of this audit.

**Blast radius.** `publish_outcome_snapshot` is called by
`infra/deploy/scanner.py`'s `build_lineage`, which is the deployed scanner's
startup path. A scanner container restarting on the same day would have crashed
on its second start. Module 25 worked around it locally — `_snapshot_instant`
quantises `as_of` to midnight UTC — and left a regression test
(`test_module_16s_snapshot_publisher_collides_on_two_instants_in_one_day`)
that fails if Module 16 is ever fixed, so the workaround gets revisited.
`publish_similarity_configuration` has **no such workaround**.

**Suggested fix** (not applied — outside this audit's boundary): make the two
keys agree. Either put the full `as_of` in the label, or compute the checksum
over `as_of.date()`. The second is likely right: a snapshot's identity is the
cutoff *date*, which is the granularity the label already assumes and the
granularity Module 18 scans at.

**Severity: HIGH** for Module 11's un-worked-around copy, **MEDIUM** for Module
16's (contained at its only live caller, but the trap is still armed for the
next caller).

---

### A3. The public-stats gate's structural test does not cover the idiom it needs to — OPEN (audit finding)

**Found by** this audit. In no module report.

Module 20's guarantee is absolute: nothing derived from a `PENDING_REVIEW` or
`REJECTED` result may reach the public page. It is enforced *structurally* by
`tests/unit/public_stats/test_gate_structure.py`, so that the **next** endpoint
is covered too, not just today's.

**The guarantee holds today.** Verified independently: `approved_runs()` has
exactly one caller (`gate.py`), `gate.py` reaches results only through Module
17's loader and imports no result-table object, and the only
`infra.db.schema` imports elsewhere in the package are the gate's own metadata
tables (`public_release_windows`, `public_stat_snapshots`).

**The enforcement has a hole.** `test_no_file_names_a_result_table_directly`
scans for *string literals* — `"setup_outcomes"`, `FROM setup_outcomes` — which
is the raw-SQL idiom. ARGUS does not use that idiom. It uses SQLAlchemy Core
`Table` objects. A break attempt planting

```python
from infra.db.schema.setups import setup_outcomes   # then: select(setup_outcomes)
```

into `services/public_stats/aggregates.py` **passed all 32 structural tests.**

So the test bans the way the gate would *not* realistically be bypassed and
misses the way it would.

**Suggested fix** (not applied — pre-existing, and the boundary says report):
add a banned-imports check alongside the existing string scan —
`infra.db.schema.setups` (and any module exporting a result table) may not be
imported by any file in the package. Roughly three lines, reusing the
`_imports()` helper already in the file.

**Severity: MEDIUM.** No live bypass exists; the safety net that is supposed to
stop one being added has a hole in the shape of the most likely mistake.

---

### A4. The ordering registry's completeness test does not scan `infra/` — OPEN (audit finding)

**Found by** this audit. In no module report.

`test_the_audit_covers_every_ordered_read_in_the_codebase`
(`tests/integration/observability/test_ordering.py`) is what makes the
registry in A1 trustworthy: it walks the parse tree for a descending order over
a timestamp-shaped column and fails if the table is not registered. Its own
docstring is right about why it matters — *"a registry that silently misses a
table is worse than no registry, because the next person trusts it."*

It scans `core/`, `services/` and `data/`. It does **not** scan `infra/` or
`packages/` — which is where Module 24 (`infra/security/`) and Module 25
(`infra/deploy/`) put all of their code. The two most recent modules were
written entirely outside the scan that is supposed to catch them.

**The registry is nonetheless complete in substance.** This audit ran the same
AST logic over `infra/` and `packages/` with a widened column list. Four hits,
none a new hazard:

| Hit | Verdict |
|---|---|
| `setup_outcomes`, `public_stat_snapshots` | inside `ordering.py`'s own `probe()`; both registered |
| `sessions` at `infra/deploy/retention.py:267` | a `WHERE expires_at <` filter, not an ordered read; registered safe |
| `table` at `infra/observability/pipeline.py:237` | false positive — a parameterised variable named `table`, not a table |

**Suggested fix** (not applied): add `Path("infra")` and `Path("packages")` to
the test's scan list. One line. Expect the `pipeline.py` false positive to need
an exclusion, which is itself worth knowing about.

**Severity: MEDIUM.** Same class as A3: the guarantee holds, and the mechanism
that is supposed to keep it holding has a blind spot exactly where new code is
being written.

---

## B. Test-suite and tooling integrity

### B1. A local `.env` file leaks past the test suite's env isolation — OPEN

**Found by** the deployment-readiness audit. Re-confirmed here **with evidence**
rather than by inspection.

`tests/unit/config/conftest.py`'s autouse `_isolated_config_env` clears every
`ARGUS_*` key from `os.environ` via `monkeypatch.delenv`. It cannot clear a
`.env` file, because `AppConfig` declares `env_file=".env"` and
pydantic-settings reads that file from disk directly, never through
`os.environ`.

**Proven, not asserted.** A two-line `.env` was planted and the config tests
re-run:

```
FAILED tests/unit/config/test_environment.py::test_defaults_to_development
FAILED tests/unit/config/test_settings.py::test_partially_set_database_group_names_each_missing_field
2 failed, 31 passed
```

with the leaked value visible in the failure —
`input_value={'name': 'leaked_from_d...nv', 'host': 'myhost'}`. The file was
removed and all 33 pass again.

**Why it has never fired:** `.env` is gitignored, is absent from this checkout,
and CI never creates one. It fires only on a developer machine that has one —
which is every developer machine that has ever run the app locally.

**Suggested fix** (not applied — pre-existing and explicitly a re-confirm task):
have the fixture neutralise the file source as well as the environment, e.g.
`monkeypatch.setitem(AppConfig.model_config, "env_file", None)`.

**Severity: MEDIUM.** It cannot corrupt production. It can make a developer's
local suite fail for a reason that has nothing to do with their change, or —
worse — make a genuinely broken config *pass* because the `.env` supplied what
the code failed to.

---

## C. Production-readiness gaps (Module 25's list, re-verified)

Module 25's own list lives in `infra/deploy/README.md` §8. Re-verified here; one
entry was stale and has been corrected.

### C1. No off-platform backup storage — OPEN. **Severity: HIGH**

`infra/deploy/backup.py`'s `dump()` writes to a filesystem path. On Railway that
path is on an ephemeral container filesystem, so a backup written there is not a
backup. Disaster recovery therefore rests entirely on the managed database
provider's own snapshots, which **have not been verified as enabled or
restorable**. The restore *drill* is real and passes (`python -m
infra.deploy.backup`, 44/44 guard triggers, functional checks on the restored
copy) — what is missing is a durable destination and a schedule.

### C2. ~~The Dockerfile has never been built~~ — **CORRECTED: it has been built and run**

Module 25 recorded this honestly at the time: Docker Hub egress was blocked in
the build environment (403 on `production.cloudfront.docker.com`), so the image
was written and reviewed but never built. **That is no longer true**, and the
old entry contradicted the entry that followed it.

Evidence, from the Railway deploy log of deployment `6435838a`:

- the traceback's frames are `/app/infra/deploy/asgi.py` and
  `/opt/venv/lib/python3.11/site-packages/uvicorn/...` — exactly the paths the
  Dockerfile creates (`WORKDIR /app`, venv at `/opt/venv`);
- `uvicorn` started and invoked `terminal_app` through `--factory`, which is the
  generated start command from `infra/deploy/processes.py`.

So: image built, container ran, venv present, source present, start command
correct. It crashed *after* all of that, at configuration resolution — the
`DATABASE_URL` ordering bug, fixed in commit `2096861` and regression-tested by
`tests/integration/deploy/test_platform_environment.py`.

`infra/deploy/README.md` §8 has been updated to say this. **Status: closed.**

### C3. Post-fix production health unconfirmed — OPEN. **Severity: HIGH**

What the crashed-then-fixed deploy proves is that the build, image and start
command work. It does not prove anything after startup. Unconfirmed: TLS
termination behaviour, the health-check path in the platform's own polling, the
`preDeployCommand` migration hook, the cron schedules, and whether all seven
process definitions exist as seven Railway services or one.

**Attempted and blocked by environment policy, not by a missing URL.** The
public hostname `argus-production-32f4.up.railway.app` was supplied and probed.
The audit environment's outbound proxy refuses the connection before it leaves
the machine:

```
curl: (56) CONNECT tunnel failed, response 403

$HTTPS_PROXY/__agentproxy/status →
  "kind":   "connect_rejected",
  "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)",
  "host":   "argus-production-32f4.up.railway.app:443"
```

Not Railway-specific: the proxy runs a restrictive allowlist. `github.com`
connects; `example.com` and the Railway host are both refused at CONNECT. No
workaround was attempted — the standing instruction on egress blocks is to stop
and report.

**To close this entry**, either allowlist `*.up.railway.app` in the environment's
network policy, or run the probes externally and hand the output back. The
commands are in Part D of the audit report.

### C4. Rate limiter is single-process — OPEN. **Severity: MEDIUM**

Counters live in process memory. `WEB_CONCURRENCY` defaults to 1 and
`DeploymentProfile.validate()` *refuses to boot* a production process with more
than one worker and no shared store, which contains the problem rather than
solving it. Horizontal scaling by replicas multiplies every configured ceiling
by the replica count and no check catches that.
`ARGUS_RATE_LIMIT_STORE_URL` is read but nothing consumes it. Redis was
considered and declined for a system with no users.

### C5. No point-in-time recovery — OPEN. **Severity: MEDIUM**

RPO is 24 hours because a daily logical dump is the mechanism. Up to a day of
registrations, logins, watchlist edits and audit records can be lost. Tightening
it needs WAL archiving, which is a platform capability rather than something
`pg_dump` can be scheduled into.

### C6. No load test — OPEN. **Severity: LOW**

Pool sizes (10 + 5 overflow per process), the 120-second health-check timeout
and the 3-second liveness cache are reasoned, not measured.

### C7. The scanner has never run in production shape — OPEN. **Severity: MEDIUM**

`run_scheduled_scan` is tested; a real weekday firing against a real universe
version with real provider data has not happened. Every module's boundary
forbade it. This is Phase 2's first real test.

### C8. Append-only tables grow without bound — OPEN, measured. **Severity: LOW**

`audit_log`, `login_attempts` and `registration_attempts` cannot be pruned: a
`DELETE` raises SQLSTATE 23001 from a trigger, and dropping the guard to run a
retention job destroys the property the table exists for. Module 25 implemented
measurement instead (`infra/deploy/retention.py`: rows, on-disk bytes, observed
arrival rate, days-to-threshold at 1 GiB). The migration path — monthly
declarative partitioning, `DETACH PARTITION` rather than `DELETE` — is
documented and deliberately unimplemented.

---

## D. Deferred features (deliberate, with reasoning)

### D1. Account recovery / password reset — DEFERRED. **Severity: MEDIUM**

**Deferred by** Module 22, re-affirmed by Module 24. **Verified absent this
audit:** no `password_reset`, `account_recovery`, `forgot_password` or recovery
code path exists anywhere in `services/`, `core/` or `infra/`.

This has a consequence Module 24 named and it is worth repeating here: it
interacts with MFA scope. An admin who enrols TOTP and loses the device has no
recovery route, and `roles.py` makes an enrolled second factor a hard
requirement for admin actions rather than an advisory one. Today's mitigation is
that role changes are made by an operator with database access. That stops being
adequate the moment a second admin exists.

### D2. Cookie-based sessions for a browser UI — DEFERRED. **Severity: LOW**

**Deferred by** Module 24 on the grounds that no browser UI existed. One exists
now — ARGUS Public — but it is **unauthenticated by design** and sends no
credential, so the reasoning still holds unchanged. **Verified absent:** no
`set_cookie`, `httponly` or `samesite` anywhere in `services/` or `infra/`.
Revisit when an authenticated browser UI is built, not before.

### D3. Redis / shared rate-limit store — DEFERRED. See C4.

---

## E. Closed since being filed

Recorded so nobody re-opens them from an old report.

### E1. `fundamentals.py` inline-filter drift from Module 07 — **RESOLVED**

**Filed by** Module 09 as deferred. This audit expected to find it open; it is
closed.

Both lookups in `core/data_validation/fundamentals.py` now go through Module
07's central helper, `select_latest_as_of` in `core/data_validation/engine.py`.
Neither inlines a query; there is no bare `select(` in the file. The helper's
`precedence` parameter was added specifically so
`get_latest_fundamental_as_of` could express its `fiscal_period_end DESC,
availability_time DESC` ordering *without* writing its own query — and, as its
docstring puts it, *"It changes only the ordering, never the filter — every
caller gets the same `availability_column <= as_of` enforcement regardless."*

### E2. TLS enforcement and HSTS — **RESOLVED** by Module 25

Deferred by Module 24 to "the point that actually terminates TLS."
`infra/deploy/tls.py` now does it: per-environment HSTS (off in development, 1
day in staging without subdomains, 1 year with subdomains in production), a 426
for a credentialed plaintext request and a 308 for an anonymous one, and — a
correction found by Module 25's own test — HSTS is sent on secure responses
only, per RFC 6797 §7.2.

### E3. Log retention / rotation policy — **RESOLVED** by Module 25

Deferred by Module 24 as "an operations policy." `infra/deploy/retention.py`
states it: application logs are JSON on stderr, captured by the platform, and
are **diagnostic and disposable**; anything that must outlive the platform's
window is written to `audit_log`. Sessions are pruned 30 days past expiry by a
daily cron. The three guarded tables are measured rather than pruned (C8).

### E4. `RAILWAY_PRIVATE_NETWORK` missing Railway's actual edge range — **RESOLVED**

Found live, not by review: the first `public_stats` deploy with
`ARGUS_ENV=production` produced `ERR_TOO_MANY_REDIRECTS` in the browser for
every page load. `identity` never showed this, only because it had not yet
been switched to `ARGUS_ENV=production` at the time it was checked — it was
carrying the same latent bug.

Root cause, confirmed from a real deploy log rather than documentation: a
Railway healthcheck request arrived from peer `100.64.0.2` — inside
`100.64.0.0/10` (RFC 6598, carrier-grade NAT), a range `RAILWAY_PRIVATE_NETWORK`
(`infra/deploy/config.py`) did not include. `tls.py`'s `request_scheme` trusts
`X-Forwarded-Proto` only from a peer in that list; with the edge's own address
untrusted, every request fell back to the ASGI scope's own scheme — always
`http`, since TLS is terminated before the container — and with
`require_https=True` in production, `TlsPolicyMiddleware` issued a `308` to
`https://` on every request. The browser had already used `https://`, so the
container saw the "same" insecure request again and redirected again,
indefinitely.

Fixed by adding `100.64.0.0/10` to `RAILWAY_PRIVATE_NETWORK`. All 194
`tests/unit/deploy/` tests pass unchanged — none had asserted the constant's
literal value, only that the profile carries whatever the constant holds.
Any production or staging service still running with the old default, or
with `ARGUS_TRUSTED_PROXIES` set explicitly to the old three ranges, needs
`100.64.0.0/10` added to that environment variable before this fix takes
effect for it — the code default does not retroactively change a value a
deployment overrode.

### E5. The success criterion used a flat percentage, not per-security volatility — **RESOLVED**

Module 15's `SUCCESS_DEFINITION` was `+10% before -5% within 60 trading
days`, applied identically to every security. That is a structural flaw,
not a calibration detail: a low-volatility large-cap and a high-volatility
penny stock have completely different "normal" daily ranges, so a flat
percentage systematically misclassifies outcomes — inflating the win rate
for naturally volatile securities and deflating it for stable ones, before
any real pattern signal is measured.

Fixed before any real historical scan ran, per the report that raised it:
recalibrating after a scan had already run against the flat criterion
would have meant redoing the whole scan, not adjusting it. The criterion
is now `+1.5×ATR before -0.75×ATR within 60 trading days`, where ATR is
the 20-session average true range at entry, computed with Module 08's own
`true_range` primitive (`core/feature_engine/panel.py`) directly from the
PIT-bounded price panel `core/outcome_tracking/excursion.py` already
loads — not a stored feature vector, which might not exist at the exact
activation instant, and not a second implementation of ATR. A security
with fewer than 20 sessions of pre-entry history gets no resolved
criterion at all (`NO_ATR`) rather than a flat-percentage fallback, which
would have silently reintroduced the flaw for exactly the securities
where getting it wrong matters most.

`target_gain`/`stop_loss` (flat fractions) became
`target_atr_multiple`/`stop_atr_multiple` (ATR multiples) in
`OutcomeThresholds`; the actual per-setup threshold each security was
measured against is now recorded on `Excursion`
(`atr_at_entry`/`target_threshold`/`stop_threshold`) rather than left
implicit in the multiplier alone. `SUCCESS_DEFINITION` and Module 13's
`PROBABILITY_DEFINITION` were updated together and remain
string-identical, asserted by
`tests/unit/outcome_tracking/test_definition.py`. A new
`tests/integration/outcome_tracking/test_atr_normalization.py` proves the
fix directly: two securities making the byte-identical post-entry price
move resolve differently — one SUCCESS, one unresolved — depending only
on how volatile each was *before* entry.

The two false-positive-taxonomy heuristics that also referenced a flat
percentage (`expansion_floor`, `breakdown_floor`) were initially left
alone as out of scope, then normalized in a follow-up once a
methodological audit confirmed they carried the identical flaw — see E6.

### E6. Two more flat thresholds carrying the same flaw — **RESOLVED**

A methodological audit swept Modules 08–17 for flat numeric thresholds
applied uniformly across a universe of heterogeneous securities. Most of
the codebase came back clean: nearly every threshold is already
self-relative (a percentile against the security's own history, a ratio
of recent to prior, a position within its own range, or — in Module 11 —
a distance divided by a per-feature robust scale). Two were not.

**`level_test` (Module 08, `feature_engine/spec.py`).** A flat ±1.5% of
price defining "price came close enough to this level to have tested it".
Price sits inside a fixed percentage band in inverse proportion to how
far it travels per session, so `support_test_count` and
`resistance_test_count` were counting volatility as much as
level-testing — and the bias reached three consumers, not one: Module
10's ACCUMULATION predicate, target-model-v1's pattern quality, and
Module 11's similarity distance, which carries both counts among its 41
metric features. Now `level_test_atr` (0.25) multiplied by the security's
own 20-session ATR, which was already in scope at that line.

One consequence, accepted deliberately: ATR is a rolling mean, so the two
counts are now unavailable for the first sessions of a series where a
percentage band produced a number. `.where(measurable)` masks them rather
than letting `NaN <= NaN` evaluate to `False` and enter the rolling sum
as a measured zero.

**`expansion_floor` / `breakdown_floor` (Module 15).** Flat 3% and -15%
separating false-positive types B, C and D. Same flaw as E5's criterion,
in the classification Module 17 reads most closely. Now
`expansion_atr_multiple` (0.45) and `breakdown_atr_multiple` (-2.25),
resolved against `Excursion.atr_fraction`.

**Units and magnitudes were changed separately, on purpose.** Both
multiples are constant-ratio translations of the percentages they
replace — `0.03/0.10 × 1.5 = 0.45` and `0.15/0.05 × 0.75 = 2.25` — so the
change moved no boundary, only the unit each boundary is expressed in.
A second opinion proposed 0.8 and 3.0 instead; those are ~1.8× and ~1.3×
looser than the existing geometry, which would have folded a substantive
recalibration into a units fix and made the first outcome distribution
computed afterwards uninterpretable — no one could have said whether a
shift came from the normalization or from the new numbers.
`tests/unit/outcome_tracking/test_classification.py` asserts the
equivalence directly, and every pre-existing classification test passed
with no fixture value changed, which is the evidence that the translation
was neutral.

None of `0.25`, `0.45` or `-2.25` is validated. They are starting points
with the correct *form*; choosing their magnitudes needs outcome data
that does not exist until a scan runs.

**Two proposals from the same second opinion were declined as premature.**
A new "live base vs dead base" quality component: all four of its
proposed sub-signals already exist as Module 08 features
(`volatility_compression`, `volume_contraction`, `higher_low_development`
plus the test counts, `time_in_upper_range`), and target-model-v1 already
combines four phase components at equal weights — so the request is to
*reweight* existing signals, which needs the outcome data nobody has yet,
and its premise (that ARGUS passes many dead bases) is untested because
no scan has run. Rebalancing Module 11's similarity weights: there are no
weights. `distance.py` is uniformly-weighted scaled-Euclidean over 41
features, so "rebalancing" means introducing a 41-dimensional vector of
new unvalidated parameters — the opposite of the principle that motivated
the request, and precisely what a fitted model should learn instead.

---

## F. Minor / cosmetic

### F1. `resolve_identity` names two unrelated things — OPEN. **Severity: LOW**

`services/identity/seam.py:resolve_identity(connection, header, ...)` resolves a
**user**. `services/intelligence/detail.py:resolve_identity(connection,
security_id, as_of)` resolves a **security's ticker and name**. Different
domains, identical name, both imported into service modules.

This audit specifically checked whether the second was a smuggled-in second
identity mechanism. It is not — it never touches users, sessions or tokens. But
a reader auditing the identity seam has to establish that twice, and a future
one might not.

---

### G1. The scanner cannot see the bars ingestion writes — **RESOLVED**

Module 26 closed the gap where nothing wrote to `canonical_ohlcv`. It did
not make the scanner able to act on what it writes, and the reason was two
numbers in two other modules.

- Module 05 derives a daily bar's `availability_time` as its session
  close **plus 16 hours** (`ProviderLagPolicy.daily_bar`). Deliberately
  conservative: FMP publishes EOD within hours, but consolidated values
  settle overnight.
- Module 18's point-in-time cutoff for scanning that same session was its
  close **plus 5 hours** (`scan_offset_hours`).

`readiness.check_readiness` filters on `availability_time <= as_of`, and
16 > 5. So a bar was never knowable at the cutoff applied to it, coverage
read zero however complete the ingestion, and the scanner recorded
`DATA_NOT_READY` forever — the same outcome as before Module 26 existed,
reached a different way.

**Why it was invisible.** Module 18's integration fixtures
(`tests/integration/feature_engine/conftest.py::insert_bars`) use a
**one-hour** availability lag, fifteen hours more optimistic than what
Module 05 stamps in production. Every readiness test had always passed
against a bar no production path could produce. This is the same shape as
A3 and A4 below: the guarantee was not broken, the test that would catch
it breaking never exercised the real path.

**The consequence for Module 27.** The Telegram bot alerts on
`BREAKOUT_READY` transitions, and transitions are written by the scan. A
scanner that records `DATA_NOT_READY` records no transitions, so the bot
had nothing to send and went quiet with nothing failing — the same
watchlist Module 21's Intelligence surface reads live off `market_state`
was equally starved, for the identical reason.

**The fix, and the correction to how this entry first described it.**
This entry originally said "the fix is one number" — `scan_offset_hours`,
5 → 17, one hour past the bar's availability lag. That was necessary and
incomplete: `core/live_scanner/schedule.py`'s `next_scan_time` treats a
date whose `due_at` (session close + `scan_offset_hours`) has already
passed its own `expires_at` (session close + `readiness_window_hours`,
left at 12 in the original text) as broken on the very first check, with
zero retries attempted. Raising the offset to 17 against an unchanged
12-hour window would not have fixed `DATA_NOT_READY`-forever; it would
have replaced it with a *different* permanent failure — every date marked
broken on its first check instead — which is worse, because it also
discards the retry margin the readiness design exists to provide. Both
numbers had to move together: `scan_offset_hours` 5 → 17,
`readiness_window_hours` 12 → 24. Applied in
`core/live_scanner/config.py`, whose rationale text for both settings now
carries this reasoning, and pinned by
`tests/integration/ingestion/test_readiness_handoff.py`, which asserts
the corrected relationship (`readiness_window_hours` still exceeds
`scan_offset_hours`) as its own structural test rather than trusting the
two literals to stay in the right order.

**A second observation, still true.** `scan_offset_hours` is tagged
`operational`, whose stated meaning is "bounds how the computation runs,
never what it produces", justified in Module 18's config by "a scan
started at 21:00 and one at 23:00 compute identically" — now "started at
09:00 and one at 11:00", but the same claim. True of the computation,
false of readiness: the number feeds `as_of`, and `as_of` decides what
the scan can see. On Module 17's own definitions it is `calibratable`.
The value has been fixed; the tag has not, and is left as noted rather
than changed silently alongside a numeric fix this entry was already
correcting once.

---

### G2. Corporate actions are never ingested on any schedule — **RESOLVED**

Module 26 ingested prices daily and fundamentals/news on a tiered
cadence. Nothing ingested splits or dividends, ever.

Module 08's `load_panel` builds its adjustment factors from
`canonical_corporate_actions` at load time rather than reading a stored
adjusted series. With that table static, a split made every price series
wrong from the split date backwards, silently, and Module 15's README
already describes the consequence: an unadjusted 2-for-1 split is a −50%
single-bar excursion that records a successful setup as a catastrophic
failure.

**What was actually missing.** Nothing in the pipeline was broken. FMP's
`fetch_splits` and `fetch_dividends`, `translate_corporate_action`,
`persist`'s `write_corporate_actions` — all present and all correct, and
`persist` even writes actions *before* bars because the adjusted series
is derived from them. The two fetchers were simply called from nowhere
outside their own unit tests. The pipe was laid; no water went in.

**The fix: a third kind on the existing tiered cadence.** It was a
spending decision rather than a technical one, and the cheaper of the two
options was taken. Module 04 exposes splits and dividends per-symbol only
— there is no calendar endpoint — so a daily full-universe sweep would be
two extra requests per symbol per day, roughly tripling Module 26's OHLCV
volume. Instead `core/ingestion/deep_refresh.py` now fetches both
alongside fundamentals and news, on the same 30/10/1/1 cadence:

- `DeepRefreshSource` gained `fetch_splits` and `fetch_dividends`.
  `FmpFetcher` already satisfied both; nothing in Module 04 changed.
- `_fetch` makes the two extra requests per due security and counts them.
- `_write` passes `actions=` to `normalize_security`, which is the whole
  of the write side — `persist` already handled the rest.
- `DeepRefreshReport` gained `corporate_actions_inserted`; the log row
  records offered/written counts in `detail` rather than in a new column,
  since the run report already carries the number.

**Cost, in the README's own terms.** Six requests per due security became
eight — a third more deep-refresh volume, not a tripled run. At
`N = 10,000`, a percentage point of the universe in BREAKOUT_READY or
UPTREND costs 800 requests a day rather than 600. (Correcting a figure
that section had wrong: a percentage point in DOWN_TREND costs 20 a day,
not 2 — now 26.7.)

**A failure is not swallowed.** An `FmpError` on the splits or dividends
request fails the security's whole refresh, exactly as one on
fundamentals does. Catching it locally would still write the log row, and
the log row is what says "this security has been refreshed" — so a
DOWN_TREND security would wait 30 days before retrying a split it never
fetched, which is the same silence this entry is about. Failing makes it
due again tomorrow.

**What this deliberately does not do.** Every other recent data addition
— news volume, insider, 13F, the Terminal's analyst data — is fenced off
from `core/scoring` and `core/market_state` by structural tests.
Corporate actions are the opposite case: they *must* reach the core price
path, because that is the only way the adjusted series is right.

**The tests.** `tests/integration/ingestion/test_corporate_actions.py`
proves it from both ends. That the fetchers are now called and their
records stored — and, the assertion that would have caught this,
that a security trading at 100 before a 2-for-1 split and 50 after comes
back from `load_panel` as a flat line at 50, with no −50% bar anywhere.
The same file reproduces the bug with the actions withheld, so the
passing case is known to be load-bearing rather than passing for some
other reason, and asserts that a replay of a date before the split still
sees the unadjusted series — closing G2 must not turn the adjusted series
into a source of foreknowledge.

**Still open, and separate.** Module 06's universe builder / historical
backfill remains unwired for corporate actions. A backfill spanning a
split still produces an unadjusted history; that is its own gap and is
not covered here.

Fixed in commit `ca48d56`.

---

### G3. `get_config()` cannot be loaded in the deployed environment — **RESOLVED**

Every deployed service runs with `DATABASE_URL` and nothing else — that
is what `.railway/railway.ts` sets, and it is deliberate: a platform
injects a connection string, not four discrete fields. But
`AppConfig.database` is a **required** field that a connection string
satisfies none of, so `get_config()` raises a pydantic
`ValidationError` in production.

`infra/db/connection.py` already knows this. Its `_bootstrap_secrets`
docstring records it as "exactly how ARGUS's first Railway deploy
crashed", and it works around the ordering by falling back to a default
secrets chain. What was missed is that the workaround is local to that
one function, while **any other code path calling `get_config()` still
crashes**.

Reproduced, in the environment the deployment actually has:

```
$ env -i DATABASE_URL=... ARGUS_ENV=production python -c "
      from data.provider_adapters.fmp.client import FmpClient; FmpClient()"
ValidationError: 1 validation error for AppConfig
database
  Field required
```

**What this breaks today.** `FmpClient.__init__` calls
`config or get_config()`, and Module 26's ingestion cron constructs one.
So the ingestion job crashes on startup in production — but only *after*
`ARGUS_UNIVERSE_VERSION` is set, because `resolve_universe_version`
raises `ScannerNotReady` and exits 2 first. The failure is therefore
latent precisely until the moment the job would otherwise start working.
`core/ingestion/orchestrator.py`'s `run_daily_ingestion` has the same
call when `app_config` is not supplied.

**The partial fix that came first.** Module 27 avoided it:
`bootstrap_secrets_provider()` (`packages/config/secrets.py`) is the
extracted, public, documented form of what `connection.py` was doing
privately, and the telegram service uses it. `connection.py` calls it
instead of holding a second copy. But a workaround per call site is not a
fix — Modules 04 and 26 still called `get_config()` directly, and any
*new* module calling it would have walked into the same wall.

**The fix — the root one, not the narrow one.** This entry named two
options and said the real fix was for `AppConfig` to accept
`DATABASE_URL` as a source for the `database` group. That is what was
done, in `packages/config/settings.py`:

- `database_settings_from_url()` derives `host`, `port`, `name` and
  `user` from a connection string, using `urllib.parse` rather than
  SQLAlchemy's `make_url` so the project's lowest layer does not gain a
  dependency on its database toolkit.
- A `mode="before"` model validator on `AppConfig` merges those fields
  under any explicit `ARGUS_DATABASE__*` values, **field by field**, so a
  deployment overriding one setting does not have to restate the other
  three. `mode="before"` because `database` is required: an "after"
  validator would run only once validation had already failed with the
  error this exists to prevent.
- The password embedded in the connection string is deliberately *not*
  read. `DatabaseSettings` still has no password field, the credential is
  still resolved at connection time through `SecretsProvider`, and
  `AppConfig` remains safe to log — a test asserts the loaded object
  carries no part of the secret.
- A URL that is absent, malformed, or not PostgreSQL changes nothing: the
  original "Field required" error surfaces exactly as before, because a
  connection string ARGUS cannot use should not be rescued into a
  half-configured process.

**The regression test.** `tests/unit/config/test_database_url.py` runs
this entry's own repro in a **subprocess with a scrubbed environment and
a working directory containing no `.env`** — `env -i`, expressed
portably. In-process `monkeypatch` could not prove it: `env_file=".env"`
is read from disk rather than through `os.environ`, which is B1 above and
is still open, so a developer with a local `.env` would otherwise get a
different answer from CI on precisely the test that speaks for
production. Both call sites this entry names are covered — `FmpClient()`
and `run_daily_ingestion`'s config step — plus the case that must still
fail: a process with no database configuration at all still raises, and
still names `database`.

**One test changed rather than being added.**
`tests/unit/db/test_connection.py::test_no_discrete_setting_is_required_when_a_url_is_supplied`
asserted that `AppConfig()` *raised* in the platform environment, and
that `build_database_url` worked anyway. That was the workaround being
documented as if it were the property. It now asserts the stronger truth:
the config loads, and both paths agree on the same server.

Fixed in commit `66a343e`.

---

## H. Pre-key audit (2026-09-06)

Eight findings from an audit run at `d8d5337`, before purchasing an FMP
Ultimate subscription. All eight are **RESOLVED**. None of them raises an
error; every one of them is either silent data loss or a job that reports
success while doing nothing, which is why they were worth finding before
real data started accumulating rather than after.

Two general lessons run through them and are worth stating once:

- **Working code with no caller is the most common defect in this
  repository.** G2 was the first instance; H1, H2 and H4 are three more.
  In each case every piece was written, tested and correct, and nothing
  invoked it. A test suite cannot see this, because a unit test *is* a
  caller.
- **A guard that compares two lists cannot see something missing from
  both.** H6's append-only drift test had exactly that shape and passed
  while three tables went unguarded for two migrations.

---

### H1. Nothing could build a universe version — **RESOLVED**

`ARGUS_UNIVERSE_VERSION` gates `ingestion` and `scanner`; both resolve it
through `resolve_universe_version` and both exit 2 without it.
`core/universe/builder.py`'s `build_intervals_from_fetch` and
`construct_version` are the only functions that create one, both were
complete and tested, and **neither had a caller outside `tests/`**.
Twelve entrypoints existed under `infra/deploy/`; none built a universe.

G2's pattern one level up, with a worse consequence: there the data was
wrong, here there was no data and no way to start.

**The fix.** `infra/deploy/universe.py`, shaped like the twelve
entrypoints beside it — `refuse_arguments`, `profile_for().validate()`,
structured logging, the same three exit codes. It prints
`ARGUS_UNIVERSE_VERSION=<label>` on stdout for the platform variable.

Not a cron, deliberately. A rotating universe version would point both
jobs at whatever the last run produced, which is the "latest row wins"
hazard A1 catalogues four instances of.

**The README example never committed.** Under SQLAlchemy 2.0 a
`Connection` that closes without `commit()` rolls back, so following
`core/universe/README.md` would fetch ten thousand tickers, register their
identities, write the version and its membership, and discard all of it
with a successful-looking log. Corrected, and the entrypoint's own test
reads the rows back on a separate connection — which is the only way to
tell a commit from a convincing in-transaction read.

**The timing trap, which is more common than it first looks.** With no
price history a security's listing interval starts at the moment ARGUS
first saw it (`IntervalEvidence.FIRST_OBSERVED`, erring narrow on purpose
— claiming an earlier listing is the survivorship lie Module 06 exists to
prevent). The ingestion reads members at `as_of_for(trading_date)`, and
that cutoff is session close plus seventeen hours: **14:00 UTC**. So a
universe built at any point after 14:00 UTC is dated later than the
instant the next run asks about, and that run finds zero members, logs
`universe_size: 0`, and exits healthy.

The entrypoint slices its own intervals at that exact instant, counts
them, and exits 1 naming both dates rather than letting it be discovered
from an ingestion log. The tests pin both sides of the boundary, verified
against `scan_date_for`/`as_of_for` rather than reasoned about — the
first version of them had the two cases backwards.

Fixed in commit `a81873e`.

---

### H2. Ownership and Terminal data were never ingested on a healthy run — **RESOLVED**

The ingestion stages ran in this order:

1. `refresh_due_securities` — writes a `deep_refresh_log` row for every
   security it refreshes, stamped with the run's own trading date.
2. `_ingest_ownership_data` — asks "who is due today?"
3. `_ingest_terminal_data` — asks the same question.

Both later stages answered it from that same log, through `tiers.decide`:

```python
if last.refreshed_on >= target_date:
    return DueDecision(due=False, trigger=ALREADY_REFRESHED, ...)
```

So on every run where step 1 succeeded, steps 2 and 3 saw an empty list.
`insider_trades`, `institutional_ownership`, `canonical_disclosures`,
`canonical_snapshots`, `analyst_grades` and `technical_indicators` were
never written — every table the Ultimate plan is bought for, and the
entire output of two prior work items.

**Why the tests could not see it**, which is the transferable part:

- `FakeFetcher` had none of the Ultimate methods, so the capability
  guards marked both stages "skipped" and the empty list was never
  reached. A double that cannot do the thing under test will agree that
  the thing works.
- `test_ownership_ingestion.py` and `test_terminal_data.py` call the
  stage functions directly with a hand-built `due=[member]`. That is the
  right shape for testing those functions and it steps over exactly the
  part that was broken.

**The fix.** The due list is computed once, before the deep refresh, and
the same list is passed to all three stages — which is what one cadence
should always have meant. `tests/integration/ingestion/test_stage_ordering.py`
asserts it at the orchestrator level against a fetcher that can actually
serve the endpoints, and fails on the old code.

Fixed in commit `730bb51`.

---

### H3. The FMP plan rate could not be set from the deployment — **RESOLVED**

`.railway/railway.ts` names four variables for `ingestion` and no rate
setting, and that file is the whole environment — its own header says
*omit means delete*. So a rate raised by hand in the Railway panel was
removed by the next `railway config apply`, and the process fell back to
`fmp_requests_per_minute = 300`, the Starter limit, whatever plan was
being paid for.

Two consequences beyond the obvious tenfold: `core/ingestion/strategy.py`
switches to the bulk endpoint strategy only at `>= 3000`, so that never
enabled either; and at `fmp_max_concurrency = 8` the achievable
throughput is roughly 1,600-2,400 a minute regardless, so raising the
rate alone leaves a third of a 3,000/min entitlement unused.

**The fix.** Both variables are named in `infra/deploy/processes.py`'s
generated config as `preserve()`, for `ingestion` and `scanner` only —
`news_signals` and `ownership_signals` read tables ingestion already
filled and would carry a setting with no effect. A value set in the panel
now survives an apply.

**The values are deliberately not chosen here.** They belong to the
subscription rather than to the code, and the shipped defaults stay at
the most conservative paid tier: a process that silently runs ten times
too fast against a plan that forbids it is a worse failure than one that
runs slowly. `infra/deploy/README.md` §4 says what to set and why both
must move together.

Fixed in commit `a81873e`.

---

### H4. No path to historical price data — first setup ~12 months away — **RESOLVED**

Not a bug: a missing capability, with the largest business consequence of
anything in this register.

`backfill_daily_history` takes any `start`/`end` range and has a
checkpoint, bounded concurrency and per-symbol failure isolation. Its
only production caller is `core/ingestion/prices.py`, which always asks
for `trading_date - 7 days`. History therefore accumulated one day per
day, forwards from whenever ingestion first ran.

Against ARGUS's own requirements:

- `core/feature_engine/spec.py`: `percentile: int = 252`.
- `rolling_percentile_rank` returns an all-NaN column below 252 rows.
- `core/market_state/states.py`'s CONSOLIDATION and ACCUMULATION
  predicates both require `atr_percentile`; a NaN predicate is never true.
- `core/lifecycle/engine.py`: a setup opens from those two states and no
  others.

**No setup could open for roughly 252 trading days.** Meanwhile the other
three watchlists fill from about two months in and Telegram alerts go
out, so the system looks alive while the outcome record — the thing the
project is for — stays empty for a year.

**The fix.** `infra/deploy/backfill.py`, using the existing fetcher rather
than a new one. It resumes from the checkpoint, keys the checkpoint by
range so 2010-2015 and 2015-2020 are two jobs, logs the request count and
estimated minutes before spending anything, and **fetches corporate
actions with the prices by default** — fifteen years of unadjusted
history is G2 with a longer reach, and two extra requests per symbol
removes the class.

It refuses to start without an explicit range. Fifteen years hardcoded
would be a spending decision this repository is not entitled to make.

Module 06's historical universe construction remains out of scope, as it
was for G2 — a separate gap, still open.

Fixed in commit `a81873e`.

---

### H5. `X-Argus-User` bypassed authentication in production — **RESOLVED**

`services/terminal/config.py` defaults `stub_identity_enabled` to True, so
Modules 19-21's suites keep passing, and Module 22's real-auth seam kept
that default deliberately. What nobody noticed is that `infra/deploy/asgi.py`'s
factories passed **no config at all**:

```python
def _terminal(engine, *, security):
    return create_app(engine, security=security)      # -> TerminalConfig(), stub on
```

And `services/intelligence/app.py` constructed a `TerminalConfig()` inline
inside its identity dependency, so that service could not be told
otherwise at any price.

`curl -H 'X-Argus-User: <uuid>'` was therefore enough to read and delete
another user's watchlists, from the first registration onward. No risk
today only because `users` is empty.

**A better default would not have fixed this.** A forgotten argument is
what it was, and a default is exactly the thing that gets forgotten. So
the service config is now *derived* from the deployment profile:
`DeploymentProfile.allow_identity_stub` is False for staging and
production, `build_service` constructs the config from it, and
`check_identity_stub` refuses a stub-enabled config under a profile that
forbids one — which catches a hand-built config too.

`IntelligenceConfig` gained the field and its dependency reads it, so the
hardcoded construction is gone.

**The test asserts the outcome, not the mechanism.** A production-built
Terminal returns 501 `IDENTITY_UNAVAILABLE` to a header naming a real
user; a development-built one still serves it, so the affordance Modules
19-21 rely on is intact.

Fixed in commit `730bb51`.

---

### H6. Four raw PIT tables had no mutation guard — **RESOLVED**

`sec_filings`, `insider_trades`, `institutional_ownership` and
`pending_material_events` appear in neither `APPEND_ONLY_TABLES` nor
`NO_DELETE_TABLES`, and migration 0017 created three of them with no
triggers at all.

All four carry the full PIT column set and are read with
`availability_time <= as_of`, which makes each one evidence of what ARGUS
knew at an instant — the same claim `canonical_news` makes.
`sec_filings`'s own schema docstring even said so ("insert-only, like
`canonical_news`") while the guard was absent. The argument that these
were operational, recomputable data like `news_volume_signals` does not
hold: that table has no PIT columns and is a daily projection, while
these are raw provider facts nothing regenerates.

**The test was the actual defect.** `test_installed_guards_match_declared_tables`
compares the triggers the database has against the tables the module
declares — two lists — so a table absent from *both* satisfied every
assertion. That is why 0017 shipped unnoticed.

**The fix.** Migration 0019 adds the triggers, and a new test starts from
the schema rather than from a list: every table carrying the four PIT
columns must be guarded or named in `PIT_GUARD_EXCEPTIONS` with a written
reason. Twelve tables match today and the exceptions dict is empty.
Verified by removing one table from the list and watching it fail.

Fixed in commit `730bb51`.

---

### H7. Three uniqueness keys could not deduplicate — **RESOLVED**

Every writer is `ON CONFLICT DO NOTHING`, because these tables are
append-only and `DO UPDATE` would be refused by 0003's guard. That only
works if the conflict happens.

**`analyst_grades` and `insider_trades` had nullable columns in their
keys.** Under Postgres's default rule two NULLs are never equal, so a row
with a NULL in its key conflicts with nothing — including an identical
copy of itself. Re-ingestion appended the same row every run, into tables
that cannot be cleaned. Not hypothetical for `analyst_grades`: FMP's
field names there are unverified and `translate_grade` stores `None` when
no alias resolves, so one wrong spelling meant a BREAKOUT_READY security
accumulating its whole grade history daily, forever.

Both are now `UNIQUE NULLS NOT DISTINCT`. The columns stay nullable,
which is correct — `normalize_transaction_code` returns None for an
unrecognised code precisely so an unknown code is never counted as a
purchase, and a NOT NULL sentinel would be a value that lies about what
was read.

**`institutional_ownership` froze the first observation of a quarter.**
The key was `(security_id, year, quarter)`. 13F filings arrive across the
45 days after a quarter closes and amendments later still, all under the
same quarter — so a first fetch that saw 120 of an eventual 340 filers
stayed at 120 permanently, and the next quarter's comparison reported a
215-institution exodus that never happened.

**Adding `observation_time` to the key would not have fixed it**, which
is worth recording because it was the obvious fix and the audit proposed
it. That column is derived here from the quarter end plus the 45-day
deadline, so it is identical for every fetch of a quarter however many
times the figures change. The key needs to distinguish "the same numbers
again" from "different numbers later", and only the numbers can:
`content_fingerprint`, a stable hash of the figures ARGUS reads, resolved
through the same `FIELD_ALIASES` as everything else.

`observation_time` was corrected in the same change to the later of the
deadline and the fetch instant. The deadline stays a floor because
nothing is public before it; the fetch instant is the rest, because a
revision seen in March was not knowable in February and saying otherwise
is the same leak from the other side.

`latest_two_quarters` collapses revisions to one row per quarter — without
that it would have compared a quarter against an earlier version of
itself and reported the difference as a change in ownership.

Fixed in commit `730bb51`.

---

### H8. Split ratios were read from fixed field names, and skipped in silence — **RESOLVED**

Splits were the last FMP field group in the codebase read from hardcoded
key names. Every other one — bankruptcy, insider, 13F, the Terminal's
Ultimate data — goes through an alias table, because the field names were
assembled from documentation rather than verified against a live key.

Splits carried the same risk with a worse consequence. An unresolved
analyst grade is a missing panel; an unresolved split is a *wrong price
series*, and G2's own text describes what that costs: an unadjusted
2-for-1 is a −50% single-bar excursion recorded as a catastrophic failure
of a setup that succeeded.

And it was silent. `core/feature_engine/panel.py`:

```python
ratio = _split_ratio(row.details)
if ratio is None:
    continue          # no log, no counter, nothing
```

while `data/normalization/adjustments.py` reported the identical case
through `report.skip(...)`. One rule, two implementations, disagreeing
about whether anyone should be told.

**The fix.** One implementation with an alias table
(`SPLIT_FIELD_ALIASES`, including a combined `splitRatio` fallback), and
the skip is logged with a count of unresolved splits, the securities
affected, and the spellings tried.

**The reporting matters more than the aliases**, and the fix is ordered
that way on purpose: the alias list is still a guess, so an incomplete
list plus a visible counter is a problem found on day one, while a
complete-looking list and silence is a problem found in a backtest months
later.

Fixed in commit `730bb51`.

---

## I. Production incident (2026-09-06)

### I1. One service owning the migration step left the other eight racing it — **RESOLVED**

Commit `730bb51` (migration 0020, H7's uniqueness-key fix) shipped a
migration that `assert_backwards_compatible` correctly refused: it drops
constraints an older `ON CONFLICT` clause names and adds a NOT NULL
column older code does not supply. `identity` was the only service with
a `preDeployCommand`, on the reasoning that every other service's
authentication depends on its schema — and on an unstated assumption the
reasoning didn't need until this incident: that the other services would
*wait* for `identity`'s migration to finish.

They don't, and can't with Railway's actual trigger. A single push
starts one independent deploy per service; Railway's own "deployment
dependencies" feature (reference-variable-driven startup ordering) is
documented to apply only to template deploys, staged-changes application,
environment duplication and PR environments — not to this trigger. So
while `identity`'s migration sat refused, the other eight services
deployed anyway, immediately expecting the revision `identity` alone was
stuck trying to reach. `check_health` reported `down`, `/health/live`
returned 503 on every probe, and five deployments (`Argus`,
`Public_stats`, `intelligence`, `telegram`, `health`) were marked FAILED
by Railway's retry window. Recovered operationally, not by a code change:
`ARGUS_ALLOW_DESTRUCTIVE_MIGRATION=1` for one deploy (the three affected
tables were empty in every environment, confirmed before forcing it),
then each FAILED deployment redeployed by hand — Railway does not retry
those on its own.

Not a fluke of timing. Every future migration — even a safe one
`assert_backwards_compatible` would never refuse — carried the same race
whenever `identity`'s build-and-migrate time did not comfortably outrun
the other services' boot time; a refused migration just made the race
deterministic instead of probabilistic, because the schema then never
advances at all.

**The fix.** Every process now runs `python -m infra.deploy.migrate` as
its own preDeploy step (`dashboard_settings()` in `infra/deploy/railway.py`,
regenerated into `.railway/railway.ts`), so no service can start ahead of
the schema its own code expects. That only works because
`infra/deploy/migrate.py` also serializes the resulting concurrent
invocations through a session-scoped Postgres advisory lock — Alembic's
version table is not a lock, so N containers calling `command.upgrade`
within the same few seconds would otherwise race the same DDL, and the
loser would crash with an "already exists" error rather than finding the
work already done. Whichever process acquires the lock first does the
real work or hits the refusal; every process that waited then finds
either the schema already at head or the identical refusal — the whole
fleet advances together or is refused together, never split between the
two.

Railway's per-service startup-ordering feature was checked directly
against its own documentation before being ruled out, rather than assumed
absent: it names exactly which deploy triggers it covers, and a GitHub
push is not one of them.

`tests/integration/deploy/test_migrations_before_traffic.py` reproduces
both halves: several independent connections calling `upgrade_to_head`
at the same time, once against the repository's real migration chain (no
DDL-race crash, schema reaches head) and once against a pending
destructive migration (every process refuses identically, none starts,
schema unchanged) — the second is the incident itself, proven fixed.

Fixed in commit `719a3e5`.

---

## Summary

| Severity | Open | Deferred | Closed |
|---|---|---|---|
| HIGH | A1, A2 (Module 11 copy), C1, C3 | — | — |
| MEDIUM | A2 (Module 16 copy), A3, A4, B1, C4, C5, C7 | D1 | — |
| LOW | C6, C8, F1 | D2, D3 | — |
| — | — | — | C2, E1, E2, E3, E4, E5, E6, G1, G2, G3, H1-H8, I1 |

**The H series was found by a pre-key audit at `d8d5337`** and none of it
appeared in any module report. Two patterns run through it and are worth
carrying forward:

*Working code with no caller* accounts for H1, H2 and H4 — and G2 before
them. In each case every piece was written, tested and correct, and
nothing invoked it. A unit test cannot see this, because a unit test is
itself a caller. The check that does see it is asking, of any capability:
*what in production calls this?*

*A guard comparing two lists cannot see something absent from both.*
H6's append-only drift test had exactly that shape and passed while three
tables went unguarded across two migrations. Its replacement starts from
the schema instead — which is the general fix: derive the expected set
from the thing being protected, not from a second list somebody maintains
by hand.

**Three entries here appear in no module report:** A2's Module 11 occurrence,
A3's structural-test gap, and A4's registry-scan blind spot. All three were
found by this audit.

A3 and A4 are the same shape and worth reading together: in both cases the
guarantee currently holds, and the *test that exists to keep it holding* does
not cover the way it would most likely be broken — A3 scans for an idiom the
codebase does not use, A4 scans every package except the two where the most
recent code lives. Neither is a bug today. Both are the reason a bug would not
be caught tomorrow.
