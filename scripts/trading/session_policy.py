#!/usr/bin/env python3
"""Session window evaluation and operational policy."""
from __future__ import annotations


from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


def hhmm_to_minute(raw: str) -> int:
    """Convert an HH:MM string to absolute minutes from midnight."""
    value = raw.strip().upper().replace("Z", "")
    parts = value.split(":")
    hour = int(parts[0]) % 24
    minute = int(parts[1]) % 60 if len(parts) > 1 else 0
    return hour * 60 + minute


@dataclass
class SessionWindow:
    """Represents a trading session window in minutes from midnight."""

    start_minute: int
    end_minute: int

    @classmethod
    def from_spec(cls, spec: Any) -> Optional["SessionWindow"]:
        if spec is None:
            return None
        if isinstance(spec, cls):
            return spec
        if isinstance(spec, dict):
            start = spec.get("start") or spec.get("open")
            end = spec.get("end") or spec.get("close")
            if not start or not end:
                return None
            return cls(hhmm_to_minute(start), hhmm_to_minute(end))
        if isinstance(spec, str):
            return cls(
                hhmm_to_minute(spec.split(",")[0]),
                hhmm_to_minute(spec.split(",")[-1]),
            )
        return None

    def contains(self, when_utc: datetime) -> bool:
        """Check if the given UTC timestamp falls within the window."""
        minute = when_utc.hour * 60 + when_utc.minute
        if self.start_minute <= self.end_minute:
            return self.start_minute <= minute < self.end_minute
        return minute >= self.start_minute or minute < self.end_minute

    def minutes_until_close(self, when_utc: datetime) -> Optional[int]:
        """Return minutes until session close, or None if outside session."""
        if not self.contains(when_utc):
            return None
        minute = when_utc.hour * 60 + when_utc.minute
        close = self.end_minute
        if self.start_minute <= close:
            return max(0, close - minute)
        if minute < close:
            return max(0, close - minute)
        return max(0, (24 * 60 - minute) + close)


@dataclass
class SessionDecision:
    """The result of evaluating the current session constraints."""

    tradable: bool
    minutes_to_exit: Optional[int]
    reason: str


class SessionPolicy:
    """Evaluates whether trading is allowed based on scheduled session windows."""

    def __init__(
        self, sessions: Dict[str, SessionWindow], exit_buffer_minutes: int = 5
    ) -> None:
        self._sessions = {
            key.upper(): window for key, window in sessions.items() if window
        }
        self._exit_buffer = max(0, int(exit_buffer_minutes))
        self._overrides: Dict[str, Dict[str, str]] = {}

    def update_overrides(self, overrides: Optional[Dict[str, Dict[str, str]]]) -> None:
        """Update active session overrides."""
        self._overrides = {
            key.upper(): value for key, value in (overrides or {}).items()
        }

    def evaluate(
        self, instrument: str, now_utc: datetime, has_position: bool
    ) -> SessionDecision:
        """Evaluate the session policy for an instrument at a given time."""
        inst = instrument.upper()
        minutes_remaining: Optional[int] = None
        reason = "session_closed"

        override = self._overrides.get(inst)
        window_override: Optional[SessionWindow] = None
        if override:
            start_raw = override.get("start") or override.get("open")
            end_raw = override.get("end") or override.get("close")
            if start_raw and end_raw:
                start_t = hhmm_to_minute(start_raw)
                end_t = hhmm_to_minute(end_raw)
                window_override = SessionWindow(start_t, end_t)
                minutes_remaining = window_override.minutes_until_close(now_utc)
                if minutes_remaining is not None:
                    reason = "ops_override"

        if minutes_remaining is None:
            window = self._sessions.get(inst)
            if window:
                minutes_remaining = window.minutes_until_close(now_utc)
                if minutes_remaining is not None:
                    reason = "profile_open"

        if (
            minutes_remaining is not None
            and self._exit_buffer
            and minutes_remaining <= self._exit_buffer
        ):
            minutes_remaining = None
            reason = "session_exit_window"

        if minutes_remaining is not None:
            return SessionDecision(
                tradable=True, minutes_to_exit=minutes_remaining, reason=reason
            )
        if has_position:
            return SessionDecision(
                tradable=True, minutes_to_exit=None, reason="position_persist"
            )
        return SessionDecision(tradable=False, minutes_to_exit=None, reason=reason)
