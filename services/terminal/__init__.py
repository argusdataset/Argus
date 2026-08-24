"""ARGUS Terminal (Module 19): company data, charts, and user watchlists.

The first module in ARGUS whose caller is not ARGUS. Everything here
serves a consumer that sees only the response shapes — v0's frontend now,
a mobile client later — which is why `schemas.py` is the centre of the
module rather than an afterthought.

Deliberately isolated from the scoring pipeline. Nothing here imports
from `core/scoring/` or `core/market_state/`, and a structural test
asserts it: fundamentals and news are a context layer for a person to
read, never an input to `argus_score`.
"""

from services.terminal.app import create_app
from services.terminal.config import (
    CALIBRATABLE,
    KINDS,
    OPERATIONAL,
    STRUCTURAL,
    TerminalConfig,
    TerminalLimit,
    TerminalLimits,
)
from services.terminal.errors import TerminalError, error_payload
from services.terminal.identity import USER_HEADER, current_user_id

__all__ = [
    "CALIBRATABLE",
    "KINDS",
    "OPERATIONAL",
    "STRUCTURAL",
    "USER_HEADER",
    "TerminalConfig",
    "TerminalError",
    "TerminalLimit",
    "TerminalLimits",
    "create_app",
    "current_user_id",
    "error_payload",
]
