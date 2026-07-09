from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchResult:
    title: str
    size: str
    detail_url: str


@dataclass(frozen=True)
class MagnetDetail:
    title: str
    size: str
    magnet: str
    info_hash: str
