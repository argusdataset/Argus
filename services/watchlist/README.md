# Watchlist Service

Watchlist Service — built in Module 19. Manages user watchlists of candidates and tickers.

**"Watchlist" means two different things in ARGUS — this folder is only one of them:**
- **User Watchlists** (this folder, Module 19) — user-created, named, manually edited, stored.
- **ARGUS Intelligence Watchlists** (Module 21, served from `services/intelligence/`) — the 3 named lists (DOWN TREND / CONSOLIDATION / BREAKOUT READY), auto-generated as **derived views** over the Market State Engine, never stored as an independent source of truth.

These must never be merged or confused.
