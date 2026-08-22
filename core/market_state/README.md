# Market State

Market State Engine — built in Module 10. Implements the 9-state machine
(`DOWN_TREND → BASE_FORMING → CONSOLIDATION → ACCUMULATION → BREAKOUT_WATCH →
BREAKOUT_READY → UPTREND → DISTRIBUTION → DOWN_TREND`), with explicit
measurable entry/exit criteria, confidence, minimum evidence, minimum sample
size, and duration tracking per state. Drives the 3 user-facing intelligence
watchlists (DOWN TREND, CONSOLIDATION, BREAKOUT READY) as derived views,
never stored independently. `target_model_matching/` lives inside this
folder as a named sub-model, not a peer.
