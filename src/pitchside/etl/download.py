"""Fetch files from Cricsheet."""

from __future__ import annotations

from pathlib import Path

import httpx

from pitchside.config import CRICSHEET_BASE


def download(url: str, dest: Path, timeout: float = 120.0) -> Path:
    """Download `url` to `dest`, writing to a temp file first so a failed
    transfer never leaves a truncated file behind."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    tmp.replace(dest)
    return dest


def register_url(name: str) -> str:
    """URL of a Cricsheet register file, e.g. people.csv or names.csv."""
    return f"{CRICSHEET_BASE}/register/{name}"
