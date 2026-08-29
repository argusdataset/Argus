"""Regenerates PHASE1_AUDIT_REPORT.pdf.

Committed beside the PDF it produces, so the report is reproducible rather
than an artefact nobody can rebuild — the same standard this project applies
to every stored result. Needs `reportlab`, which is not an ARGUS dependency:

    pip install reportlab && python docs/architecture/build_phase1_audit_report.py
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

OUT = str(Path(__file__).with_name("PHASE1_AUDIT_REPORT.pdf"))

INK = colors.HexColor("#12161d")
MUTED = colors.HexColor("#5b6472")
FAINT = colors.HexColor("#8a93a1")
RULE = colors.HexColor("#d7dce3")
ACCENT = colors.HexColor("#1f4f9c")
HIGH = colors.HexColor("#b3243b")
MED = colors.HexColor("#b8791a")
LOW = colors.HexColor("#4a7c59")
CODEBG = colors.HexColor("#f4f6f9")

ss = getSampleStyleSheet()


def S(name, **kw):
    kw.setdefault("parent", ss["Normal"])
    return ParagraphStyle(name, **kw)


TITLE = S("t", fontName="Helvetica-Bold", fontSize=26, leading=30, textColor=INK, spaceAfter=4)
SUBTITLE = S("st", fontName="Helvetica", fontSize=13, leading=17, textColor=MUTED, spaceAfter=18)
H1 = S(
    "h1",
    fontName="Helvetica-Bold",
    fontSize=16,
    leading=20,
    textColor=INK,
    spaceBefore=20,
    spaceAfter=8,
)
H2 = S(
    "h2",
    fontName="Helvetica-Bold",
    fontSize=11.5,
    leading=15,
    textColor=ACCENT,
    spaceBefore=13,
    spaceAfter=5,
)
H3 = S(
    "h3",
    fontName="Helvetica-Bold",
    fontSize=10,
    leading=13,
    textColor=INK,
    spaceBefore=9,
    spaceAfter=3,
)
BODY = S(
    "b",
    fontName="Helvetica",
    fontSize=9.5,
    leading=13.8,
    textColor=INK,
    alignment=TA_JUSTIFY,
    spaceAfter=6,
)
LEAD = S(
    "lead",
    fontName="Helvetica",
    fontSize=10.5,
    leading=15.5,
    textColor=INK,
    alignment=TA_JUSTIFY,
    spaceAfter=8,
)
BULLET = S("bl", parent=BODY, leftIndent=11, bulletIndent=2, spaceAfter=3)
CODE = S(
    "c",
    fontName="Courier",
    fontSize=7.9,
    leading=10.4,
    textColor=INK,
    backColor=CODEBG,
    borderPadding=6,
    leftIndent=2,
    spaceBefore=4,
    spaceAfter=7,
)
CELL = S("cell", fontName="Helvetica", fontSize=8.3, leading=11, textColor=INK)
CELLB = S("cellb", parent=CELL, fontName="Helvetica-Bold")
CAP = S(
    "cap", fontName="Helvetica-Oblique", fontSize=8.2, leading=11, textColor=FAINT, spaceAfter=8
)


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def P(t, s=BODY):
    return Paragraph(t, s)


def code(t):
    lines = [esc(x) for x in t.strip("\n").split("\n")]
    return Paragraph("<br/>".join(x.replace(" ", "&nbsp;") or "&nbsp;" for x in lines), CODE)


def table(rows, widths, header=True, align=None):
    data = []
    for r, row in enumerate(rows):
        data.append(
            [
                c
                if isinstance(c, Paragraph)
                else Paragraph(c, CELLB if (header and r == 0) else CELL)
                for c in row
            ]
        )
    st = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
    ]
    if header:
        st += [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f6")),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, RULE),
        ]
    if align:
        st += align
    t = Table(data, colWidths=widths, hAlign="LEFT")
    t.setStyle(TableStyle(st))
    return t


def sev(level):
    c = {"HIGH": HIGH, "MEDIUM": MED, "LOW": LOW, "CLOSED": LOW}[level]
    return Paragraph(f'<font color="{c.hexval()}"><b>{level}</b></font>', CELL)


def rule():
    t = Table([[""]], colWidths=[170 * mm], rowHeights=[0.8])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.7, RULE)]))
    return t


# ---------------------------------------------------------------- page furniture


def decorate(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(FAINT)
    canvas.drawString(20 * mm, 12 * mm, "ARGUS — Full Integration Audit (Modules 03-25)")
    canvas.drawRightString(190 * mm, 12 * mm, f"{canvas.getPageNumber()}")
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(20 * mm, 15 * mm, 190 * mm, 15 * mm)
    canvas.restoreState()


doc = BaseDocTemplate(
    OUT,
    pagesize=A4,
    leftMargin=20 * mm,
    rightMargin=20 * mm,
    topMargin=18 * mm,
    bottomMargin=20 * mm,
    title="ARGUS — Full Integration Audit (Modules 03-25)",
    author="Phase 1 Engineering Complete checkpoint",
)
doc.addPageTemplates(
    [
        PageTemplate(
            id="main", frames=[Frame(20 * mm, 20 * mm, 170 * mm, 259 * mm, id="f")], onPage=decorate
        )
    ]
)

s = []
A = s.append

# ---------------------------------------------------------------- title

A(Spacer(1, 30))
A(P("ARGUS", TITLE))
A(P("Full Integration Audit — Modules 03-25", SUBTITLE))
A(rule())
A(Spacer(1, 12))
A(
    table(
        [
            ["Checkpoint", 'Phase 1 — "Engineering Complete", before any real data'],
            [
                "Scope",
                "The composition of 25 individually-approved modules, not any module's internals",
            ],
            ["Branch", "claude/new-session-96aymh"],
            [
                "Commits",
                "5c5383e (register) · 0904705 · 8bf9808 (startup logging) · 47177b8 (argv refusal)",
            ],
            [
                "Deliverable",
                "docs/architecture/KNOWN_ISSUES.md — 19 entries, first consolidated register",
            ],
            ["Test suite", "2,259 passing · 0 failed · 0 skipped · lint clean"],
        ],
        [32 * mm, 138 * mm],
        header=False,
    )
)

A(Spacer(1, 16))
A(P("Verdict", H2))
A(
    P(
        "The composition holds. All six cross-module seams are intact, no module built after "
        "another quietly broke it, and the full suite passes in one process with no ordering "
        "dependency. Twenty-five individually-correct modules did add up to a coherent system — "
        "which is not automatically true and was the thing this audit existed to check.",
        LEAD,
    )
)
A(
    P(
        "The drift found is <b>in the enforcement mechanisms, not the code they enforce</b>. Two "
        "structural tests that exist to keep guarantees holding do not cover the way those "
        "guarantees would most likely be broken. Neither is a bug today; both are the reason a bug "
        "would not be caught tomorrow. That is precisely the class of thing 25 narrow sequential "
        "reviews cannot see and a composition audit can.",
        LEAD,
    )
)
A(
    P(
        "Separately, taking the system to production surfaced three real defects — two in the "
        "deployment layer's own robustness, one a latent database defect present since Module 11. "
        "All three are fixed or registered.",
        LEAD,
    )
)

A(PageBreak())

# ---------------------------------------------------------------- Part A

A(P("Part A — Test suite integrity", H1))

A(P("The measured total", H2))
A(code("2,259 passed · 0 failed · 0 skipped"))
A(
    P(
        "One process, all modules, clean environment: no <font face='Courier'>.env</font> on disk, "
        "no <font face='Courier'>ARGUS_*</font> in the shell. This is the measured figure, not a "
        "sum of previously-reported per-module numbers.",
        BODY,
    )
)

A(P("Cross-module isolation: none found", H2))
A(
    P(
        "The suite was re-run with the collection order reversed to expose ordering dependencies. "
        "One failure — a timing-sensitive performance assertion (20.4ms vs 21.7ms per security) "
        "that passes 8/8 twice when run alone. A flake, not order-dependence. No shared-fixture "
        "leakage, no migration-ordering problem, no global-state bleed between modules.",
        BODY,
    )
)

A(P("A methodological correction, reported on myself", H2))
A(
    P(
        "The first reversed-order run reported three failures in "
        "<font face='Courier'>tests/unit/config</font> and <font face='Courier'>tests/unit/db</font>. "
        "Those were my own contamination — a <font face='Courier'>.env</font> file planted for the "
        "test below, while that background run was executing. I recognised the signature, re-ran "
        "clean, and they disappeared. Recorded because three unexplained failures left unchased "
        "would have been a false finding in an audit whose whole job is to not produce those.",
        BODY,
    )
)

A(P("The .env isolation gap — still open, now proven", H2))
A(
    P(
        "<font face='Courier'>tests/unit/config/conftest.py</font> clears every "
        "<font face='Courier'>ARGUS_*</font> key from the process environment. It cannot clear a "
        "<font face='Courier'>.env</font> file, because <font face='Courier'>AppConfig</font> "
        "declares <font face='Courier'>env_file=\".env\"</font> and pydantic-settings reads it from "
        "disk, never through <font face='Courier'>os.environ</font>.",
        BODY,
    )
)
A(
    code("""
# with a two-line .env planted:
FAILED tests/unit/config/test_environment.py::test_defaults_to_development
FAILED tests/unit/config/test_settings.py::test_partially_set_database_group_names_each_missing_field
2 failed, 31 passed

# the leak, visible inside the assertion itself:
input_value={'name': 'leaked_from_d...nv', 'host': 'myhost'}
""")
)
A(
    P(
        "Never fires in CI because <font face='Courier'>.env</font> is gitignored and never created. "
        "Fires on any developer machine that has run the app locally. Registered as B1, MEDIUM. "
        "Not fixed — pre-existing and already-flagged, which the audit's boundary puts out of scope. "
        "The fix is one line.",
        BODY,
    )
)

A(PageBreak())

# ---------------------------------------------------------------- Part B

A(P("Part B — Cross-module seam integrity", H1))

A(
    table(
        [
            ["#", "Seam", "Verdict"],
            [
                "1",
                "<font face='Courier'>current_user_id</font> (M19 seam / M22 implementation)",
                "PASS",
            ],
            ["2", "<font face='Courier'>services/shared/</font> extracted conventions", "PASS"],
            ["3", "Module 18 read surface", "PASS"],
            [
                "4",
                "M17 <font face='Courier'>approved_runs()</font> / M20 release gate",
                "<b>PASS</b> — enforcement gap",
            ],
            [
                "5",
                "M23 ordering registry + <font face='Courier'>probe()</font>",
                "<b>PASS</b> — coverage gap",
            ],
            ["6", "Project-wide log scrubbing", "PASS"],
        ],
        [8 * mm, 107 * mm, 55 * mm],
    )
)

A(Spacer(1, 6))
A(P("1 — The identity seam", H3))
A(
    P(
        "Exactly one commit touched Modules 19-21 after Module 22, and its complete diff is an "
        "additive optional <font face='Courier'>security=None</font> parameter plus a "
        "<font face='Courier'>harden(app)</font> wrap. <b>No route signature, no query, no schema "
        "changed.</b> No second identity mechanism exists. One name collision registered as F1: "
        "<font face='Courier'>intelligence/detail.py:resolve_identity</font> resolves a "
        "<i>security's</i> ticker, not a user.",
        BODY,
    )
)

A(P("2 — Shared conventions", H3))
A(
    P(
        "One definition each of <font face='Courier'>error_payload</font>, "
        "<font face='Courier'>ApiError</font>, <font face='Courier'>Unavailable</font>, "
        "<font face='Courier'>Freshness</font>, <font face='Courier'>Provenance</font> — all in "
        "<font face='Courier'>services/shared/</font>, every consumer importing from there. Module "
        "21's <font face='Courier'>Provenance</font> subclassing fix holds: the base carries only "
        "<font face='Courier'>note</font>, with exactly two per-service subclasses.",
        BODY,
    )
)

A(P("3 — Module 18's read surface", H3))
A(
    P(
        "Every consumer outside <font face='Courier'>core/live_scanner/</font> goes through the "
        "published surface — <font face='Courier'>runs_by_status</font>, "
        "<font face='Courier'>scan_results</font>, <font face='Courier'>run_daily</font>. Nothing "
        "queries <font face='Courier'>live_scan_runs</font> directly.",
        BODY,
    )
)

A(P("4 — The publication gates: guarantee holds, net has a hole", H3))
A(
    P(
        "Verified independently: <font face='Courier'>approved_runs()</font> has exactly one caller, "
        "and <font face='Courier'>gate.py</font> reaches results only through Module 17's loader. "
        "But the structural test scans for result tables named as <i>string literals</i> — the "
        "raw-SQL idiom, which ARGUS does not use. A planted bypass using the idiom it does use "
        "passed all 32 structural tests:",
        BODY,
    )
)
A(
    code("""
# planted into services/public_stats/aggregates.py
from infra.db.schema.setups import setup_outcomes   # then: select(setup_outcomes)

32 passed in 1.84s          <-- NOT CAUGHT
""")
)

A(P("5 — Ordering registry: complete, but scanned incompletely", H3))
A(
    P(
        "All four hazards still registered <font face='Courier'>safe=False</font>, still unfixed. "
        "<b>No fifth instance exists</b> — verified by enumerating every "
        "<font face='Courier'>limit(1)</font> project-wide and re-scanning all 41 source files "
        "changed since Module 23. But the completeness test scans "
        "<font face='Courier'>core/</font>, <font face='Courier'>services/</font> and "
        "<font face='Courier'>data/</font> — not <font face='Courier'>infra/</font>, where Modules "
        "24 and 25 put all of their code. I ran the same AST logic there: clean.",
        BODY,
    )
)

A(P("6 — Log scrubbing", H3))
A(
    P(
        "Break attempt: a credential-shaped log field planted in "
        "<font face='Courier'>core/universe/</font> — a package that is neither identity nor "
        "observability, i.e. exactly what Module 24's project-wide extension was for. "
        "<b>Caught</b>, with file and line.",
        BODY,
    )
)

A(PageBreak())

# ---------------------------------------------------------------- Part C

A(P("Part C — Consolidated known-issues register", H1))
A(
    P(
        "Nineteen entries compiled into <font face='Courier'>docs/architecture/KNOWN_ISSUES.md</font>, "
        "each re-verified against the current tree rather than trusted from the report that filed it. "
        "<b>Three appear in no module report at all.</b>",
        LEAD,
    )
)

A(P("The Module 16 mystery — resolved, and it is two functions", H2))
A(
    P(
        "Module 25's report was cut off mid-sentence and named one occurrence. There are two. "
        "<font face='Courier'>data_snapshot</font> has a <b>unique</b> "
        "<font face='Courier'>version_label</font> and a <b>non-unique</b> "
        "<font face='Courier'>content_checksum</font>, and both publishers compute their idempotence "
        "key and their uniqueness key at different granularities:",
        BODY,
    )
)
A(
    code("""
checksum      = sha256(f"{config.content_checksum()}|{as_of.isoformat()}")   # microseconds
version_label = f"{config.version_label()}@{as_of.date().isoformat()}"       # days
""")
)
A(
    P(
        "Two calls, same config, same date, different instant: the checksums differ, so the "
        '"already published?" lookup misses; it inserts; the labels are identical; '
        "<font face='Courier'>UniqueViolation</font>. <b>The failure is a loud crash, not a silent "
        "overwrite</b> — the question the cut-off note left open. Confirmed empirically against a "
        "real migrated database:",
        BODY,
    )
)
A(
    code("""
Module 16  publish_outcome_snapshot          core/outcome_tracking/config.py:219
   same instant, twice    : IDEMPOTENT (same id returned)
   two instants, same day : UniqueViolation: uq_data_snapshot_version_label

Module 11  publish_similarity_configuration  core/historical_similarity/config.py:196
   same instant, twice    : IDEMPOTENT (same id returned)
   two instants, same day : UniqueViolation: ... same constraint
""")
)
A(
    P(
        "Module 16's copy is worked around at its only live caller. <b>Module 11's has no "
        "workaround and appears in no module report.</b>",
        BODY,
    )
)

A(P("The register", H2))
A(
    table(
        [
            ["ID", "Item", "Sev"],
            [
                "A1",
                'Four "latest row wins" ordering hazards — open by instruction; no fifth exists',
                sev("HIGH"),
            ],
            [
                "A2",
                "<font face='Courier'>data_snapshot</font> publishers collide on two same-day calls — <b>two</b> functions",
                sev("HIGH"),
            ],
            [
                "A3",
                "Gate structural test scans an idiom the codebase does not use <i>(audit finding)</i>",
                sev("MEDIUM"),
            ],
            [
                "A4",
                "Ordering-registry completeness test does not scan <font face='Courier'>infra/</font> <i>(audit finding)</i>",
                sev("MEDIUM"),
            ],
            [
                "A5",
                "TOTP secrets stored as plaintext; no encryption mechanism exists <i>(audit finding)</i>",
                sev("MEDIUM"),
            ],
            [
                "B1",
                "A local <font face='Courier'>.env</font> leaks past the suite's env isolation",
                sev("MEDIUM"),
            ],
            ["C1", "No off-platform backup storage — <b>largest gap</b>", sev("HIGH")],
            [
                "C2",
                'Dockerfile "never built" — <b>corrected: it builds and runs</b>',
                sev("CLOSED"),
            ],
            ["C3", "Production health past startup unconfirmed", sev("HIGH")],
            ["C4", "Rate limiter single-process — contained, not solved", sev("MEDIUM")],
            ["C5", "No PITR — RPO is 24 hours", sev("MEDIUM")],
            ["C6", "No load test", sev("LOW")],
            ["C7", "Scanner never run in production shape", sev("MEDIUM")],
            ["C8", "Append-only tables grow unbounded — measured, not prunable", sev("LOW")],
            [
                "D1",
                "Account recovery deferred; interacts with hard MFA requirement for admin",
                sev("MEDIUM"),
            ],
            [
                "D2",
                "Cookie sessions deferred — reasoning unchanged, public page is unauthenticated",
                sev("LOW"),
            ],
            [
                "E1",
                "<font face='Courier'>fundamentals.py</font> filter drift — <b>RESOLVED</b>",
                sev("CLOSED"),
            ],
            ["E2", "TLS / HSTS — <b>RESOLVED by Module 25</b>", sev("CLOSED")],
            ["E3", "Log retention — <b>RESOLVED by Module 25</b>", sev("CLOSED")],
        ],
        [11 * mm, 133 * mm, 26 * mm],
    )
)

A(Spacer(1, 5))
A(
    P(
        "A3 and A4 are the same shape twice, and worth reading together: in both cases the guarantee "
        "holds, and the test that exists to keep it holding does not cover the way it would most "
        "likely be broken.",
        CAP,
    )
)

A(PageBreak())

# ---------------------------------------------------------------- Part D

A(P("Part D — Production deployment", H1))
A(
    P(
        "Reported as blocked in the first pass — the audit environment's egress proxy refused CONNECT "
        "to every Railway host, and no workaround was attempted. Bringing the remaining six services "
        "up then surfaced three real defects, two of them in Module 25's own deployment layer.",
        LEAD,
    )
)

A(P("Defect 1 — Entrypoints silently discarded their arguments", H2))
A(P("<b>Fixed — commit 47177b8.</b> This was the live incident.", BODY))
A(
    P(
        "A service was configured with "
        "<font face='Courier'>python -m infra.deploy.migrate &amp;&amp; uvicorn ...</font>. Reaching "
        "a shell, <font face='Courier'>&amp;&amp;</font> sequences two commands. Executed without "
        "one, <font face='Courier'>&amp;&amp;</font> and everything after it become argv for the "
        "migration — which discarded them with <font face='Courier'>_ = argv</font>, migrated "
        'nothing, printed "schema already at head", and exited 0.',
        BODY,
    )
)
A(
    code("""
$ python -m infra.deploy.migrate '&&' uvicorn infra.deploy.asgi:identity_app --factory ...
  EXIT CODE = 0                     <-- exits 0, "successfully"
  last log line: "schema already at head"
  uvicorn startup lines: 0
  listeners on 8942:     0
""")
)
A(
    P(
        "Every observable was consistent with success: the platform saw a process that ran and "
        "ended, no server started, the health check failed five minutes later, the domain returned "
        "502. The one thing that would have named it instantly was sitting in argv, thrown away.",
        BODY,
    )
)
A(
    P(
        "<font face='Courier'>infra/deploy/cli.py</font> now refuses: it names the arguments "
        "verbatim, exits 2, and when the list begins with a shell operator says so explicitly and "
        "prints the <font face='Courier'>sh -c '...'</font> form that guarantees a shell. Refusal "
        "happens before logging is configured and before any database work.",
        BODY,
    )
)

A(P("Defect 2 — A failing service factory could die without saying anything", H2))
A(P("<b>Fixed — commit 8bf9808.</b>", BODY))
A(
    P(
        "Under <font face='Courier'>--workers</font>, a factory exception is raised in a child "
        'process whose stderr a hosted platform may or may not surface. From the outside, "the '
        'factory raised" and "the factory was never called" were indistinguishable. '
        "<font face='Courier'>_module_app</font> now announces entry on stderr before anything can "
        "fail — its <i>absence</i> is the diagnostic — and reports any failure on two independent "
        "channels before re-raising, because the structured channel depends on logging having "
        "worked, and logging is one of the things that can break.",
        BODY,
    )
)

A(P("Defect 3 — TOTP secrets are stored in plaintext", H2))
A(
    P(
        "<b>Registered as A5, not yet fixed.</b> Found while checking a claim about MFA configuration.",
        BODY,
    )
)
A(
    code("""
# services/identity/mfa.py
secret = pyotp.random_base32()
.values(mfa_secret=secret, ...)          # stored raw

# infra/db/schema/users.py
Column("mfa_secret", Text, nullable=True)   # plain Text, no encryption

$ grep -rn "Fernet|AES|encrypt\\(" core/ services/ infra/ data/ packages/
  NONE — no encryption-at-rest mechanism exists
""")
)
A(
    P(
        "Anyone with a database dump — including any <font face='Courier'>pg_dump</font> the backup "
        "tooling produces — can mint valid TOTP codes for every enrolled admin. Scrubbing keeps the "
        "secret out of logs; nothing protects it at rest. MEDIUM today with one admin and no real "
        "users; HIGH the moment either changes, or the backup archive lands off-platform.",
        BODY,
    )
)

A(P("What remains unverified", H2))
A(
    P(
        "Health past startup for the six new services, TLS termination behaviour, the platform's own "
        "health-check polling, the pre-deploy hook in place, and the cron schedules firing. C3 stays "
        "open until each is observed rather than inferred.",
        BODY,
    )
)

A(PageBreak())

# ---------------------------------------------------------------- assessment

A(P("Assessment", H1))

A(P("Is the system what 25 approved reports implied?", H2))
A(
    P(
        "Substantially yes, with one instructive exception. Every seam held. Two items expected to be "
        "open were already closed — <font face='Courier'>fundamentals.py</font>'s filter drift, and "
        "TLS and log retention, both delivered by Module 25 — which is the process working as "
        "designed.",
        LEAD,
    )
)

A(P("Where it drifted", H2))
A(
    P(
        "The enforcement mechanisms, not the code they enforce. A3 scans for an idiom the codebase "
        "does not use; A4 scans every package except the two where the newest code lives. In both "
        "cases the guarantee is intact and the net has a hole shaped like the likeliest mistake. "
        "That is the class of finding a composition audit exists for.",
        LEAD,
    )
)
A(
    P(
        "The second real finding is A2's second copy. Module 25 found a defect, described half of "
        "it, and was cut off. The half it never reached was that the same bug sits unmitigated in "
        "Module 11, written five modules earlier. A register like the one this audit produced would "
        "have caught that when Module 16 was written — which is the argument for the register "
        "existing at all.",
        LEAD,
    )
)

A(P("Ranked, before real data lands", H2))
A(
    table(
        [
            ["#", "Item", "Why it is first"],
            [
                "1",
                "A2 — Module 11's <font face='Courier'>publish_similarity_configuration</font>",
                "Unhandled <font face='Courier'>IntegrityError</font> on a path that believes it is idempotent, with no workaround. Small, contained fix.",
            ],
            [
                "2",
                "C1 — off-platform backup storage",
                "The restore drill works and passes; the destination does not exist. DR rests entirely on provider snapshots nobody has verified.",
            ],
            [
                "3",
                "C3 — production health past startup",
                "Nothing about the running system is confirmed beyond the fact that it starts.",
            ],
            [
                "4",
                "A5 — MFA secrets at rest",
                "Cheap to fix now; expensive to discover later. Rises to HIGH with real users.",
            ],
        ],
        [8 * mm, 62 * mm, 100 * mm],
    )
)

A(Spacer(1, 10))
A(P("A note on this audit's own limits", H2))
A(
    P(
        "Part D was blocked, then partially completed, and is still not fully verified — it is "
        "unperformed in places, not passed. I also found my own contamination in a background test "
        "run mid-audit, caught it, and re-ran. That is worth stating because it is the same lesson "
        "the deployment taught twice over: <b>a green suite is a claim about the conditions it ran "
        "under</b>. Two thousand tests passed while a deployed process could not start, and passed "
        "again while a migration silently swallowed the rest of its own start command. The defects "
        "fixed here are not that something failed — it is that failing looked identical to "
        "succeeding.",
        LEAD,
    )
)

doc.build(s)
print("written:", OUT)
