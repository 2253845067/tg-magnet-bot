from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Iterable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from models import MagnetDetail, SearchResult


logger = logging.getLogger(__name__)


# Transient status codes worth retrying (rate-limit / server errors).
# 403 and 404 are not retried: a block or missing page won't change on retry.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 520, 521, 522, 523, 524})


class CiliError(RuntimeError):
    pass


class CiliClient:
    def __init__(
        self,
        base_urls: str | list[str],
        timeout_secs: int = 15,
        max_retries: int = 2,
        retry_backoff_secs: float = 1.0,
    ) -> None:
        if isinstance(base_urls, str):
            base_urls = [base_urls]
        self._base_urls = [url.rstrip("/") for url in base_urls if url.strip()]
        self._timeout = timeout_secs
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff = float(retry_backoff_secs)
        # Last site that successfully answered a search; tried first next time
        # so a flaky-but-working site isn't re-probed from scratch every query.
        self._preferred: str | None = None
        self._client = httpx.AsyncClient(
            timeout=timeout_secs,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Upgrade-Insecure-Requests": "1",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, limit: int = 8) -> list[SearchResult]:
        response = await self._search_response(query)
        response_url = str(response.url)
        soup = BeautifulSoup(response.text, "html.parser")

        results: list[SearchResult] = []
        for row in soup.select("table.file-list tbody tr"):
            link = row.select_one("td a[href]")
            if not link:
                continue

            href = link.get("href", "")
            if not href.startswith("/!"):
                continue

            cells = row.select("td")
            size = _clean_text(cells[1].get_text(" ", strip=True)) if len(cells) > 1 else ""
            title = _result_title(link)
            if not title:
                continue

            results.append(
                SearchResult(
                    title=title,
                    size=size,
                    detail_url=urljoin(response_url, href),
                )
            )
            if len(results) >= limit:
                break

        return results

    async def _search_response(self, query: str) -> httpx.Response:
        failures: list[str] = []
        for base_url in self._ordered_base_urls():
            try:
                response = await self._get_with_retry(
                    f"{base_url}/search",
                    params={"q": query},
                    headers={"Referer": f"{base_url}/"},
                )
            except httpx.HTTPError as exc:
                failures.append(f"{base_url}: {_http_error_message(exc)}")
                continue

            if response.status_code >= 400:
                failures.append(f"{base_url}: HTTP {response.status_code}")
                continue

            self._set_preferred(base_url)
            return response

        detail = "；".join(failures) if failures else "未配置搜索站点"
        raise CiliError(f"所有磁力搜索站点都失败了（{detail}）")

    async def detail(self, detail_url: str) -> MagnetDetail:
        # The stored detail_url already points at the originating site, so we
        # only fetch from there. Fanning the same path out across every
        # configured base URL used to produce 404s because each site's
        # /!xxxx paths are not portable between domains.
        try:
            response = await self._get_with_retry(
                detail_url,
                headers={"Referer": _referer_for(detail_url)},
            )
        except httpx.HTTPError as exc:
            raise CiliError(f"磁力详情页请求失败：{_http_error_message(exc)}") from exc
        if response.status_code >= 400:
            raise CiliError(f"磁力详情页返回 HTTP {response.status_code}")
        soup = BeautifulSoup(response.text, "html.parser")

        magnet = _extract_magnet(soup, response.text)
        if not magnet:
            raise CiliError("详情页没有找到磁力链接")

        title_node = soup.select_one(".magnet-title")
        title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
        size = _extract_info_value(soup, "\u6587\u4ef6\u5927\u5c0f")
        info_hash = _extract_info_value(soup, "\u79cd\u5b50\u7279\u5f81\u7801") or _btih(magnet)

        return MagnetDetail(
            title=title or _magnet_display_name(magnet) or "Untitled",
            size=size,
            magnet=magnet,
            info_hash=info_hash,
        )

    async def _get_with_retry(
        self,
        url: str,
        *,
        params=None,
        headers=None,
    ) -> httpx.Response:
        """GET with retry/backoff for transient failures.

        Retries network errors (timeouts, connection failures) and retryable
        HTTP statuses (429, 5xx) with exponential backoff. Honors a
        ``Retry-After`` header on 429. Non-retryable responses (2xx, 403, 404)
        are returned as-is for the caller to judge. Retry attempts use a
        shorter timeout than the first try to bound total elapsed time.
        """
        last_error: BaseException | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self._timeout if attempt == 0 else min(self._timeout, 8),
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self._max_retries:
                    wait = self._retry_backoff * (2 ** attempt)
                    logger.warning(
                        "cili request failed on %s (attempt %d/%d, retrying in %.1fs): %s",
                        url,
                        attempt + 1,
                        self._max_retries + 1,
                        wait,
                        _http_error_message(exc),
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.warning(
                    "cili request failed on %s after %d attempts: %s",
                    url,
                    self._max_retries + 1,
                    _http_error_message(exc),
                )
                raise
            # We got a response; decide whether to retry it.
            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                wait = _retry_after_secs(response) or self._retry_backoff * (2 ** attempt)
                logger.warning(
                    "cili got HTTP %s on %s (attempt %d/%d, retrying in %.1fs)",
                    response.status_code,
                    url,
                    attempt + 1,
                    self._max_retries + 1,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            return response
        # Defensive: the loop only ends via `return` or `raise` above.
        raise last_error or RuntimeError("cili: request exhausted without response")

    def _ordered_base_urls(self) -> list[str]:
        if self._preferred and self._preferred in self._base_urls:
            return [self._preferred, *[u for u in self._base_urls if u != self._preferred]]
        return self._base_urls

    def _set_preferred(self, base_url: str) -> None:
        self._preferred = base_url


def _result_title(link) -> str:
    pieces: list[str] = []
    for child in link.children:
        if getattr(child, "name", None) == "p":
            continue
        if hasattr(child, "get_text"):
            text = child.get_text("", strip=True)
        else:
            text = str(child).strip()
        if text:
            pieces.append(text)

    title = _clean_text("".join(pieces))
    if title:
        return title

    sample = link.select_one(".sample")
    if sample:
        return _clean_text(sample.get_text(" ", strip=True))
    return _clean_text(link.get_text(" ", strip=True))


def _extract_magnet(soup: BeautifulSoup, raw_html: str) -> str:
    input_node = soup.select_one("#input-magnet")
    if input_node and input_node.get("value"):
        return html.unescape(input_node["value"])

    link = soup.select_one('a[href^="magnet:?"]')
    if link and link.get("href"):
        return html.unescape(link["href"])

    match = re.search(r"magnet:\?xt=urn:btih:[^\"'<>\s]+", raw_html)
    return html.unescape(match.group(0)) if match else ""


def _extract_info_value(soup: BeautifulSoup, label: str) -> str:
    for dt in soup.select("dl.torrent-info dt"):
        text = _clean_text(dt.get_text(" ", strip=True)).replace(":", "").replace("\uff1a", "")
        if label in text:
            dd = dt.find_next_sibling("dd")
            if dd:
                return _clean_text(dd.get_text(" ", strip=True))
    return ""


def _btih(magnet: str) -> str:
    match = re.search(r"xt=urn:btih:([a-zA-Z0-9]+)", magnet)
    return match.group(1).lower() if match else ""


def _magnet_display_name(magnet: str) -> str:
    match = re.search(r"(?:^|[?&])dn=([^&]+)", magnet)
    if not match:
        return ""
    return _clean_text(httpx.QueryParams(f"dn={match.group(1)}").get("dn", ""))


def _clean_text(text: str | Iterable[str]) -> str:
    if not isinstance(text, str):
        text = " ".join(text)
    return re.sub(r"\s+", " ", text).strip()


def _referer_for(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return f"{parsed.scheme}://{parsed.netloc}/"


def _http_error_message(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时"
    return str(exc) or exc.__class__.__name__


def _retry_after_secs(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
