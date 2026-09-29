#! /usr/bin/env python3

"""Fetch available TWS API versions for Linux from interactivebrokers.github.io.

On Linux the TWS API ships as the Mac/Unix zip (twsapi_macunix.<ver>.zip);
there is no separate .sh Linux installer. This scrapes the download page and
returns those zips (the Linux-usable artifact), tagged Stable vs Latest.
"""
from __future__ import annotations

import re
import sys
import urllib.request
from dataclasses import dataclass

BASE = "https://interactivebrokers.github.io"
INDEX_URL = BASE + "/"

# twsapi_macunix.1045.01.zip -> ("1045", "01")
_MACUNIX_RE = re.compile(
    r'href\s*=\s*["\']([^"\']*?twsapi_macunix\.(\d+)\.(\d+)\.zip)["\']',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ApiRelease:
    version: str        # normalized, e.g. "10.45.01"
    raw_version: str    # as in filename, e.g. "1045.01"
    channel: str        # "stable", "latest", or "unknown"
    url: str            # absolute download URL
    os: str = "linux"   # macunix zip = the Linux-usable build


def _pretty_version(major_minor: str, build: str) -> str:
    mm = major_minor
    return f"{mm[:-2]}.{mm[-2:]}.{build}" if len(mm) >= 3 else f"{mm}.{build}"


def _classify_channel(html: str, match_start: int) -> str:
    """Best-effort: nearest 'Stable' / 'Latest'/'Beta' heading before the link."""
    window = html[max(0, match_start - 600): match_start].lower()
    stable = window.rfind("stable")
    latest = max(window.rfind("latest"), window.rfind("beta"))
    if stable == -1 and latest == -1:
        return "unknown"
    return "stable" if stable > latest else "latest"


def fetch_linux_api_versions(url: str = INDEX_URL, timeout: int = 20) -> list[ApiRelease]:
    """Return the Linux-usable (macunix zip) TWS API releases listed on the page."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ibapi-version-check)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    releases: dict[str, ApiRelease] = {}
    for m in _MACUNIX_RE.finditer(html):
        href, mm, build = m.group(1), m.group(2), m.group(3)
        abs_url = href if href.startswith("http") else BASE + "/" + href.lstrip("/")
        raw = f"{mm}.{build}"
        releases[raw] = ApiRelease(_pretty_version(mm, build), raw,
                                   _classify_channel(html, m.start()), abs_url)

    return sorted(releases.values(),
                  key=lambda r: [int(x) for x in r.raw_version.split(".")],
                  reverse=True)


if __name__ == "__main__":
    try:
        rels = fetch_linux_api_versions()
    except Exception as e:
        print(f"Failed to fetch: {e}", file=sys.stderr)
        sys.exit(1)
    if not rels:
        print("No macunix (Linux-usable) TWS API downloads found.", file=sys.stderr)
        sys.exit(2)
    for r in rels:
        print(f"{r.version:<12} {r.channel:<8} {r.url}")

