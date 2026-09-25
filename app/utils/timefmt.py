"""Parsing and formatting of times such as ``01:02:03.500``."""

from __future__ import annotations

import math


def parse_time(text: str) -> float:
    """Parse ``SS``, ``MM:SS`` or ``HH:MM:SS`` (seconds may have decimals).

    The leading field may be any size ("90" or "75:00" are fine); later
    fields must be below 60. Raises ``ValueError`` with a readable message.
    """
    value = (text or "").strip().replace(",", ".")
    if not value:
        raise ValueError("Please enter a time, for example 00:01:30.")
    parts = value.split(":")
    if len(parts) > 3:
        raise ValueError(f"'{text}' is not a valid time. Use HH:MM:SS.")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"'{text}' is not a valid time. Use HH:MM:SS.") from None
    if any(math.isnan(n) or math.isinf(n) or n < 0 for n in numbers):
        raise ValueError(f"'{text}' is not a valid time.")
    if any(not n.is_integer() for n in numbers[:-1]):
        raise ValueError(f"'{text}' is not a valid time. Only seconds can have decimals.")
    if len(numbers) >= 2 and numbers[-1] >= 60:
        raise ValueError(f"'{text}': seconds must be below 60.")
    if len(numbers) == 3 and numbers[1] >= 60:
        raise ValueError(f"'{text}': minutes must be below 60.")
    total = 0.0
    for number in numbers:
        total = total * 60 + number
    return total


def format_timestamp(seconds: float | None, with_ms: bool = True) -> str:
    """``3723.5`` -> ``01:02:03.500`` (or ``01:02:03`` without milliseconds)."""
    if seconds is None or seconds < 0 or math.isnan(seconds):
        seconds = 0.0
    millis_total = round(seconds * 1000)
    hours, rem = divmod(millis_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    if with_ms:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_duration(seconds: float | None) -> str:
    """Human friendly duration: ``2:05`` or ``1:02:03``; '-' when unknown."""
    if seconds is None or seconds < 0 or math.isnan(seconds):
        return "-"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
