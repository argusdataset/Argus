# Architecture Docs

`ARGUS_CONTEXT.md` in this folder is the durable project context (what ARGUS
is, why it's built the way it is, and the module map) — carried forward from
the Module 01 build prompt so future modules don't depend on prior
conversation history to understand the system.

`CROSS_CUTTING_REQUIREMENTS.md` holds the rules every module is measured
against, rather than each module restating them.

`KNOWN_ISSUES.md` is the consolidated known-issues register: every item any
module's report flagged as found-but-not-fixed, deferred, or carried forward,
compiled in one place by the Phase 1 full integration audit. Before this
existed that information was spread across 25+ individual reports, each written
by a session that could not see the others. An entry leaves it by being fixed
and having its fix pointed at — not by being forgotten.

`PHASE1_AUDIT_REPORT.pdf` is that audit's findings as a document, for reading
away from a terminal. `build_phase1_audit_report.py` regenerates it — committed
beside it so the report is reproducible rather than an artefact nobody can
rebuild. It needs `reportlab`, which is deliberately not an ARGUS dependency:
nothing the system runs depends on being able to draw a PDF.
