# Intelligence Service

Intelligence API — built in Module 21. Authenticated. Serves the 3 named
ARGUS Intelligence Watchlists as views over the Market State Engine
(Module 10); per-asset detail (the 5-number score set, full component
breakdown, historical similarity results, pending material events); a chart
datafeed with ARGUS overlays (consolidation zone boundaries,
state-transition markers) that extends Module 19's Terminal datafeed rather
than reimplementing OHLCV serving; Module 16's explanation text; and the
"why did this fail" view for setups that reached OUTCOME. Watchlist entries
show score numbers, never fundamentals — fundamentals belong to Terminal
only.
