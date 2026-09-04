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

### G2. Corporate actions are never ingested on any schedule — **OPEN, HIGH**

Module 26 ingests prices daily and fundamentals/news on a tiered cadence.
Nothing ingests splits or dividends, ever, and Module 06's universe
builder is likewise unwired — a separate gap, already noted.

Module 08's `load_panel` builds its adjustment factors from
`canonical_corporate_actions` at load time rather than reading a stored
adjusted series. With that table static, a split makes every price series
wrong from the split date backwards, silently, and Module 15's README
already describes the consequence: an unadjusted 2-for-1 split is a −50%
single-bar excursion that records a successful setup as a catastrophic
failure.

It was flagged rather than fixed because it is a spending decision, not a
technical one. Module 04 exposes splits and dividends only per-symbol —
there is no calendar endpoint — so a daily full-universe sweep is two
extra requests per symbol per day, roughly tripling Module 26's OHLCV
volume. The cheaper option is a third tier on the same 30/10/1/1 cadence
as the deep refresh.

---

### G3. `get_config()` cannot be loaded in the deployed environment — **OPEN, HIGH**

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

**What is fixed.** Module 27 avoids it: `bootstrap_secrets_provider()`
(`packages/config/secrets.py`) is the extracted, public, documented form
of what `connection.py` was doing privately, and the telegram service
uses it. `connection.py` now calls it instead of holding a second copy.

**What is not.** Modules 04 and 26 still call `get_config()`. The
narrow fix is for each to take the bootstrap path; the real fix is for
`AppConfig` to accept `DATABASE_URL` as a source for `database`, which is
a Module 02 contract change touching every service and was out of scope
for an alerts bot. Either way this needs doing before the ingestion cron
is deployed, or it will fail on its first real run.

---

## Summary

| Severity | Open | Deferred | Closed |
|---|---|---|---|
| HIGH | A1, A2 (Module 11 copy), C1, C3, G2, G3 | — | — |
| MEDIUM | A2 (Module 16 copy), A3, A4, B1, C4, C5, C7 | D1 | — |
| LOW | C6, C8, F1 | D2, D3 | — |
| — | — | — | C2, E1, E2, E3, E4, E5, E6, G1 |

**Three entries here appear in no module report:** A2's Module 11 occurrence,
A3's structural-test gap, and A4's registry-scan blind spot. All three were
found by this audit.

A3 and A4 are the same shape and worth reading together: in both cases the
guarantee currently holds, and the *test that exists to keep it holding* does
not cover the way it would most likely be broken — A3 scans for an idiom the
codebase does not use, A4 scans every package except the two where the most
recent code lives. Neither is a bug today. Both are the reason a bug would not
be caught tomorrow.
