"""Read-only GitHub Trending and Trendshift ranking retrieval."""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


SOURCES = {
    "github": {
        "label": "GitHub Trending（全部語言）",
        "host": "github.com",
    },
    "trendshift": {
        "label": "Trendshift（熱門榜）",
        "host": "trendshift.io",
    },
}
VALID_SINCES = ("daily", "weekly", "monthly")
_REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TRENDING_ARTICLE = re.compile(r'<article class="Box-row"[\s\S]*?</article>', re.I)
_TRENDING_HREF = re.compile(r'<h2\b[\s\S]*?<a\b[^>]*href="/([^"]+)"', re.I)
_TRENDING_DESCRIPTION = re.compile(
    r'<p\b[^>]*class="col-9[^"]*"[^>]*>([\s\S]*?)</p>', re.I
)
_TRENDING_LANGUAGE = re.compile(
    r'<span\b[^>]*itemprop="programmingLanguage"[^>]*>([\s\S]*?)</span>', re.I
)
_TRENDING_STARS = re.compile(
    r'href="/[^"]+/stargazers"[^>]*>[\s\S]*?([\d,]+)\s*</a>', re.I
)
_TRENDING_GAIN = re.compile(r"([\d,]+)\s+stars?\s+(?:today|this week|this month)", re.I)
_RSC_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
_TAG = re.compile(r"<[^>]+>")
_CACHE_SECONDS = 45
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_USER_AGENT = "ade-agent-environment/0.1 (read-only GitHub Trending view)"


class TrendingRequestError(ValueError):
    """A request used an unsupported ranking source or period."""


class TrendingFetchError(RuntimeError):
    """An upstream ranking page could not be fetched or parsed."""


def _validate(source: str, since: str) -> tuple[str, str]:
    if source not in SOURCES:
        raise TrendingRequestError("source must be github or trendshift")
    if since not in VALID_SINCES:
        raise TrendingRequestError("since must be daily, weekly, or monthly")
    return source, since


def _source_url(source: str, since: str) -> str:
    if source == "github":
        return "https://github.com/trending?" + urllib.parse.urlencode({"since": since})
    return "https://trendshift.io/" if since == "daily" else f"https://trendshift.io/{since}"


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, host: str):
        super().__init__()
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.hostname != self.host:
            raise urllib.error.URLError("upstream attempted to redirect outside its source host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_html(url: str, host: str) -> str:
    opener = urllib.request.build_opener(_SameHostRedirect(host))
    last_error = "network request failed"
    for attempt in range(2):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with opener.open(request, timeout=20) as response:
                content = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(content) > _MAX_RESPONSE_BYTES:
                    raise TrendingFetchError("upstream response exceeded the 5 MiB limit")
                charset = response.headers.get_content_charset() or "utf-8"
                return content.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} {exc.reason}"
            if exc.code != 429 and exc.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            last_error = str(getattr(exc, "reason", exc)) or "network request failed"
        if attempt == 0:
            time.sleep(0.5)
    raise TrendingFetchError(f"Could not fetch {host}: {last_error}")


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", value))).strip()


def _integer(value: str | int | float | None) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    compact = value.strip().lower().replace(",", "")
    try:
        if compact.endswith("k"):
            return round(float(compact[:-1]) * 1_000)
        if compact.endswith("m"):
            return round(float(compact[:-1]) * 1_000_000)
        return int(float(compact))
    except ValueError:
        return 0


def _candidate(
    full_name: str,
    *,
    language: str | None,
    stars_today: int,
    stars_total: int,
    description: str,
    category: str,
    source: str,
) -> dict[str, Any] | None:
    full_name = html.unescape(full_name).strip().strip("/")
    if not _REPO_NAME.fullmatch(full_name):
        return None
    author, name = full_name.split("/", 1)
    return {
        "fullName": full_name,
        "name": name,
        "author": author,
        "url": f"https://github.com/{full_name}",
        "language": html.unescape(language).strip() if isinstance(language, str) and language else None,
        "starsToday": stars_today,
        "starsTotal": stars_total,
        "description": html.unescape(description).strip(),
        "category": category,
        "source": source,
    }


def _parse_github_trending(document: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for match in _TRENDING_ARTICLE.finditer(document):
        article = match.group(0)
        href = _TRENDING_HREF.search(article)
        if not href:
            continue
        full_name = urllib.parse.unquote(href.group(1).removesuffix("/"))
        description_match = _TRENDING_DESCRIPTION.search(article)
        language_match = _TRENDING_LANGUAGE.search(article)
        total_match = _TRENDING_STARS.search(article)
        gain_match = _TRENDING_GAIN.search(article)
        item = _candidate(
            full_name,
            language=_clean_text(language_match.group(1)) if language_match else None,
            stars_today=_integer(gain_match.group(1)) if gain_match else 0,
            stars_total=_integer(total_match.group(1)) if total_match else 0,
            description=_clean_text(description_match.group(1)) if description_match else "",
            category="All Languages",
            source="trending",
        )
        if item:
            items.append(item)
    return items


def _parse_trendshift(document: str) -> list[dict[str, Any]] | None:
    decoder = json.JSONDecoder()
    for match in _RSC_PUSH.finditer(document):
        try:
            decoded = json.loads('"' + match.group(1) + '"')
        except json.JSONDecodeError:
            continue
        key_index = decoded.find('"initialData":')
        if key_index < 0:
            continue
        array_start = decoded.find("[", key_index)
        if array_start < 0:
            continue
        try:
            rows, _ = decoder.raw_decode(decoded[array_start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(rows, list):
            continue

        ranked: list[tuple[int, int, dict[str, Any]]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            item = _candidate(
                str(row.get("full_name") or ""),
                language=(row.get("repository_language") or row.get("language")),
                stars_today=_integer(row.get("repository_stars_gained")),
                stars_total=_integer(row.get("repository_stars")),
                description=str(row.get("repository_description") or ""),
                category="Trendshift",
                source="trendshift",
            )
            if item:
                rank = _integer(row.get("rank")) or index + 1
                ranked.append((rank, index, item))
        ranked.sort(key=lambda entry: (entry[0], entry[1]))
        return [entry[2] for entry in ranked]
    return None


class TrendingService:
    """Fetch fixed public ranking sources and cache each source/period briefly."""

    def __init__(self, cache_seconds: int = _CACHE_SECONDS):
        self.cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}

    def get(self, source: str = "github", since: str = "daily") -> dict[str, Any]:
        source, since = _validate(source, since)
        key = (source, since)
        with self._cache_lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
            cached = self._cached(key)
            if cached is not None:
                return cached

        with key_lock:
            with self._cache_lock:
                cached = self._cached(key)
                if cached is not None:
                    return cached

            url = _source_url(source, since)
            document = _fetch_html(url, SOURCES[source]["host"])
            if source == "github":
                items = _parse_github_trending(document)
                if not items:
                    raise TrendingFetchError("GitHub Trending page structure changed or returned no repositories")
            else:
                items = _parse_trendshift(document)
                if items is None:
                    raise TrendingFetchError("Trendshift page structure changed: initialData was not found")

            result = {
                "source": source,
                "label": SOURCES[source]["label"],
                "since": since,
                "sourceUrl": url,
                "fetchedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "items": items,
            }
            with self._cache_lock:
                self._cache[key] = (time.monotonic(), result)
            return json.loads(json.dumps(result, ensure_ascii=False))

    def _cached(self, key: tuple[str, str]) -> dict[str, Any] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, result = entry
        if time.monotonic() - stored_at >= self.cache_seconds:
            return None
        return json.loads(json.dumps(result, ensure_ascii=False))
