"""Human readable sizes, bitrates and speeds."""

from __future__ import annotations


def human_size(num_bytes: float | None) -> str:
    """Windows-style sizes (1 KB = 1024 bytes). '-' when unknown."""
    if num_bytes is None or num_bytes < 0:
        return "-"
    value = float(num_bytes)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "bytes":
                return f"{int(value)} bytes"
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover - unreachable


def human_bitrate(kbps: float | None) -> str:
    """'129 kbps' / '8.9 Mbps'; '-' when unknown or zero."""
    if not kbps or kbps <= 0:
        return "-"
    if kbps >= 1000:
        return f"{kbps / 1000:.1f} Mbps"
    return f"{kbps:.0f} kbps"


def human_speed(bytes_per_second: float | None) -> str:
    if not bytes_per_second or bytes_per_second <= 0:
        return ""
    return f"{human_size(bytes_per_second)}/s"


def human_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m left"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s left"
    return f"{seconds}s left"
