"""Every number Module 27 has, isolated per Module 10's discipline.

All operational. This module computes no statistic, decides no threshold,
and its numbers change how a run paces itself and how much text it sends
— never what any answer says. The one that comes closest to mattering is
`send_rate_per_second`, and it is still operational: it changes how long
a dispatch takes, not who gets a message.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from core.model_validation_evaluation.validation.config import (
    CALIBRATABLE,
    KINDS,
    OPERATIONAL,
    STRUCTURAL,
)

__all__ = [
    "CALIBRATABLE",
    "KINDS",
    "OPERATIONAL",
    "STRUCTURAL",
    "TelegramConfig",
    "TelegramSetting",
    "TelegramSettings",
]


@dataclass(frozen=True, slots=True)
class TelegramSetting:
    value: float
    kind: str
    rationale: str

    def __float__(self) -> float:
        return self.value

    def __int__(self) -> int:
        return int(self.value)


def _s(value: float, kind: str, rationale: str) -> TelegramSetting:
    return TelegramSetting(value=value, kind=kind, rationale=rationale)


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    """How fast to send, how long to wait, and how much to say."""

    send_rate_per_second: TelegramSetting = field(
        default_factory=lambda: _s(
            20.0,
            OPERATIONAL,
            "Messages per second the dispatch run paces itself to. Telegram "
            "documents roughly 30 per second across all chats before it "
            "starts returning 429, and the cost of exceeding it is a "
            "throttle that makes the run slower than pacing would have. "
            "Twenty leaves headroom for the burst that a large "
            "BREAKOUT_READY day produces, and the run has no deadline — "
            "nothing waits on it.",
        )
    )
    request_timeout_seconds: TelegramSetting = field(
        default_factory=lambda: _s(
            10.0,
            OPERATIONAL,
            "Per-request timeout against Telegram's API. Short: a send that "
            "has not completed in ten seconds is not going to, and a "
            "dispatch run holding a socket open per stalled subscriber is "
            "how one unreachable chat delays every chat behind it.",
        )
    )
    max_message_chars: TelegramSetting = field(
        default_factory=lambda: _s(
            4096.0,
            STRUCTURAL,
            "Telegram's own hard limit on a message body — it rejects "
            "anything longer. Structural because it follows from the "
            "provider's contract rather than being a magnitude anyone "
            "would tune, and it is here rather than inline so the "
            "truncation that respects it is not a bare number in a "
            "formatter.",
        )
    )
    max_command_chars: TelegramSetting = field(
        default_factory=lambda: _s(
            64.0,
            OPERATIONAL,
            "How much of an incoming message body is examined when looking "
            "for a command. A command is at most a dozen characters; "
            "scanning a forwarded essay for one is work with no possible "
            "yield, and this endpoint is open to the internet.",
        )
    )

    def as_dict(self) -> dict[str, float]:
        return {f.name: float(getattr(self, f.name).value) for f in fields(self)}

    def describe(self) -> dict[str, dict[str, Any]]:
        return {f.name: asdict(getattr(self, f.name)) for f in fields(self)}

    def calibratable(self) -> dict[str, float]:
        return {
            f.name: float(getattr(self, f.name).value)
            for f in fields(self)
            if getattr(self, f.name).kind == CALIBRATABLE
        }

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    @property
    def seconds_between_sends(self) -> float:
        return 1.0 / self.send_rate_per_second.value

    @property
    def timeout(self) -> float:
        return self.request_timeout_seconds.value

    @property
    def message_limit(self) -> int:
        return int(self.max_message_chars.value)

    @property
    def command_limit(self) -> int:
        return int(self.max_command_chars.value)


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    name: str = "argus-telegram"
    settings: TelegramSettings = field(default_factory=TelegramSettings)

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "settings": self.settings.as_dict()}

    def content_checksum(self) -> str:
        payload = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def version_label(self) -> str:
        return f"{self.name}-{self.content_checksum()[:12]}"
