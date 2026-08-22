# Target Model Matching

Target Model Matching — built in Module 10, as a sub-model of the Market
State Engine (not a peer module). Judges transitions through the
CONSOLIDATION → ACCUMULATION → BREAKOUT_WATCH → BREAKOUT_READY portion of
the state machine specifically, using a named target model (see
`models/target_model_v1/`). The Market State Engine as a whole covers all 9
states; this sub-model covers one slice of them.
