"""HTTP access to the PSX/SBP surface, with provenance and an offline mode.

Design goals, in order:

1. **Never invent data.**  A missing file returns ``None`` (or raises in strict
   mode); it never becomes a zero-filled row.
2. **Polite.**  One keep-alive session, a shared minimum interval between
   requests, and bounded concurrency.  PSX is a public exchange site, not an
   API with a quota we own.
3. **Reproducible.**  Every response is cached with its SHA-256, retrieval time
   and content type, and every downstream :class:`SourceRef` points back at it.
4. **Testable.**  The transport is a seam: tests drive the whole desk from
   recorded payloads with zero network access.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from psx_desk.config import DeskConfig
from psx_desk.schemas import SourceRef
from psx_desk.sources import SourceKind, assert_source_allowed

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class PortalError(RuntimeError):
    """Base class for a failed PSX fetch."""


class NetworkError(PortalError):
    """Transport-level failure after all retries."""


class HttpStatusError(PortalError):
    """The server answered, but not with a usable status."""

    def __init__(self, url: str, status: int) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.url = url
        self.status = status


class OfflineDataMissing(PortalError):
    """``offline=True`` and the payload was neither cached nor provided."""


class PayloadRejected(PortalError):
    """The bytes arrived but are not the payload we asked for.

    A 200 with an HTML error page, a truncated archive or a PDF where a CSV was
    expected all land here.  This is what stops a scrape from silently turning
    an error page into a price.
    """


# --------------------------------------------------------------------------- #
# Transport seam
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RawResponse:
    """Bytes plus the metadata the desk audits."""

    url: str
    status: int
    content: bytes
    content_type: str = ""
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    from_cache: bool = False
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def is_html(self) -> bool:
        head = self.content[:512].lstrip().lower()
        return self.content_type.startswith("text/html") or head.startswith(b"<!doctype html") or head.startswith(b"<html")


@runtime_checkable
class Transport(Protocol):
    """Anything that can turn a URL into :class:`RawResponse`."""

    def get(self, url: str, *, timeout: float, headers: Mapping[str, str]) -> RawResponse: ...


class RequestsTransport:
    """Keep-alive ``requests`` transport with a bounded connection pool."""

    def __init__(self, pool_size: int = 8) -> None:
        try:
            import requests
            from requests.adapters import HTTPAdapter
        except ImportError as exc:  # pragma: no cover - exercised only on bare installs
            raise PortalError("requests is required for live PSX access: pip install requests") from exc

        self._requests = requests
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        self._session.headers.update({
            "Accept": "*/*",
            "Accept-Language": "en-PK,en;q=0.9",
            "Connection": "keep-alive",
        })

    def get(self, url: str, *, timeout: float, headers: Mapping[str, str]) -> RawResponse:
        try:
            resp = self._session.get(url, timeout=timeout, headers=dict(headers), stream=False)
        except Exception as exc:  # requests.RequestException without importing it
            raise NetworkError(f"GET {url} failed: {type(exc).__name__}: {exc}") from exc
        return RawResponse(
            url=str(resp.url),
            status=resp.status_code,
            content=resp.content,
            content_type=resp.headers.get("Content-Type", ""),
            retrieved_at=datetime.now(UTC),
            headers={k.lower(): v for k, v in resp.headers.items()},
        )

    def close(self) -> None:  # pragma: no cover - process teardown
        with contextlib.suppress(Exception):
            self._session.close()


class FixtureTransport:
    """Offline transport: an explicit URL→payload map, used by the tests.

    Values may be ``bytes``, ``str`` or a :class:`pathlib.Path` on disk.  A URL
    that is not in the map is recorded in :attr:`probes` and answered with a
    404, which is exactly how the portal behaves for a dataset that does not
    exist on that date.
    """

    def __init__(
        self,
        mapping: Mapping[str, bytes | str | Path] | None = None,
        *,
        defaults: Mapping[str, bytes | str | Path] | None = None,
    ) -> None:
        self._map = dict(mapping or {})
        self._defaults = dict(defaults or {})
        self.probes: list[str] = []

    def lookup(self, url: str) -> bytes | None:
        raw = self._map.get(url)
        if raw is None:
            for needle, payload in self._defaults.items():
                if needle in url:
                    raw = payload
                    break
        if raw is None:
            return None
        if isinstance(raw, Path):
            return raw.read_bytes()
        if isinstance(raw, str):
            return raw.encode()
        return raw

    def get(self, url: str, *, timeout: float, headers: Mapping[str, str]) -> RawResponse:
        self.probes.append(url)
        payload = self.lookup(url)
        if payload is None:
            return RawResponse(url=url, status=404, content=b"", content_type="text/plain")
        content_type = "text/html" if payload.lstrip().lower().startswith(b"<!doctype") else "application/octet-stream"
        if url.endswith(".csv"):
            content_type = "text/csv"
        elif url.endswith(".json") or "/timeseries/" in url or url.endswith("/symbols"):
            content_type = "application/json"
        return RawResponse(
            url=url, status=200, content=payload, content_type=content_type, retrieved_at=datetime.now(UTC)
        )


# --------------------------------------------------------------------------- #
# Disk cache
# --------------------------------------------------------------------------- #


class DiskCache:
    """Content-addressed payload cache with a TTL and an as-of date."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = self._key(url)
        return self.root / f"{key}.bin", self.root / f"{key}.json"

    def read(self, url: str, *, ttl_seconds: int | None) -> RawResponse | None:
        body, meta_path = self._paths(url)
        if not body.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        retrieved = datetime.fromisoformat(meta["retrieved_at"])
        if ttl_seconds is not None and datetime.now(UTC) - retrieved > timedelta(seconds=ttl_seconds):
            return None
        try:
            content = body.read_bytes()
        except OSError:
            return None
        return RawResponse(
            url=url,
            status=int(meta.get("status", 200)),
            content=content,
            content_type=meta.get("content_type", ""),
            retrieved_at=retrieved,
            from_cache=True,
        )

    def write(self, response: RawResponse) -> None:
        body, meta_path = self._paths(response.url)
        with self._lock:
            tmp = body.with_suffix(".tmp")
            tmp.write_bytes(response.content)
            tmp.replace(body)
            meta_path.write_text(
                json.dumps(
                    {
                        "url": response.url,
                        "status": response.status,
                        "content_type": response.content_type,
                        "retrieved_at": response.retrieved_at.isoformat(),
                        "sha256": response.sha256,
                        "bytes": len(response.content),
                    },
                    indent=2,
                ),
                "utf-8",
            )


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Payload:
    """A validated payload plus everything needed to cite it."""

    url: str
    content: bytes
    content_type: str
    status: int
    as_of: date | None
    kind: SourceKind
    label: str
    from_cache: bool
    retrieved_at: datetime
    sha256: str

    def source_ref(self) -> SourceRef:
        return SourceRef(
            label=self.label,
            url=self.url,
            source_kind=str(self.kind),
            retrieved_at=self.retrieved_at,
            http_status=None if self.from_cache else self.status,
            sha256=self.sha256,
            as_of=self.as_of,
            from_cache=self.from_cache,
        )

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class PortalClient:
    """Throttled, cached, retrying access to psx.com.pk and sbp.org.pk.

    ``client.get(...)`` returns ``None`` for a 404 (the normal answer when a
    date-keyed dataset has not been published) and raises for anything else
    unexpected, so a probe loop stays readable.
    """

    RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(
        self,
        config: DeskConfig,
        *,
        transport: Transport | None = None,
        cache: DiskCache | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.transport = transport or RequestsTransport()
        self.cache = cache if cache is not None else DiskCache(config.cache_dir)
        self._sleep = sleep
        self._clock = clock
        self._throttle_lock = threading.Lock()
        self._last_request = 0.0
        self.probes: list[str] = []

    # -- politeness ------------------------------------------------------- #

    def _wait_turn(self) -> None:
        if self.config.throttle_seconds <= 0:
            return
        with self._throttle_lock:
            elapsed = self._clock() - self._last_request
            wait = self.config.throttle_seconds - elapsed
            if wait > 0:
                self._sleep(wait)
            self._last_request = self._clock()

    # -- public API ------------------------------------------------------- #

    def get(
        self,
        url: str,
        *,
        kind: SourceKind = SourceKind.MARKET_DUMP,
        label: str | None = None,
        as_of: date | None = None,
        ttl_seconds: int | None = -1,
        allow_missing: bool = True,
    ) -> Payload | None:
        """Fetch ``url``, or return ``None`` when PSX says it does not exist.

        ``ttl_seconds=-1`` uses the configured freshness window; ``None`` means
        "cached is fine regardless of age" (used for immutable date-keyed files).
        """

        assert_source_allowed(url)
        self.probes.append(url)
        ttl = self.config.cache_ttl_seconds if ttl_seconds == -1 else ttl_seconds

        cached = self.cache.read(url, ttl_seconds=ttl)
        if cached is not None and cached.status == 200:
            return self._wrap(cached, kind=kind, label=label, as_of=as_of)

        if self.config.offline:
            raise OfflineDataMissing(f"offline mode: {url} is not in the local cache")

        response = self._fetch_with_retries(url)
        if response.status == 404:
            if allow_missing:
                logger.info("PSX has no payload at %s", url)
                return None
            raise HttpStatusError(url, 404)
        if response.status >= 400:
            raise HttpStatusError(url, response.status)

        self.cache.write(response)
        return self._wrap(response, kind=kind, label=label, as_of=as_of)

    def probe(
        self,
        urls: list[str] | tuple[str, ...],
        *,
        kind: SourceKind = SourceKind.MARKET_DUMP,
        validator: Callable[[Payload], bool] | None = None,
        as_of: date | None = None,
    ) -> tuple[Payload | None, list[str]]:
        """Try each URL in order, returning the first payload that validates.

        Returns ``(payload, rejected)`` where ``rejected`` lists
        ``"url :: reason"`` strings, so a failed scrape explains itself instead
        of collapsing into "no data".
        """

        rejected: list[str] = []
        for url in urls:
            try:
                payload = self.get(url, kind=kind, as_of=as_of, allow_missing=True)
            except (PortalError, ValueError) as exc:
                rejected.append(f"{url} :: {exc}")
                continue
            if payload is None:
                rejected.append(f"{url} :: 404")
                continue
            if validator is not None:
                try:
                    if not validator(payload):
                        rejected.append(f"{url} :: payload did not validate")
                        continue
                except PayloadRejected as exc:
                    rejected.append(f"{url} :: {exc}")
                    continue
            return payload, rejected
        return None, rejected

    # -- internals -------------------------------------------------------- #

    def _fetch_with_retries(self, url: str) -> RawResponse:
        headers = {
            "User-Agent": self.config.user_agent,
            "Referer": "https://www.psx.com.pk/",
            "Accept": "*/*",
        }
        last: RawResponse | None = None
        for attempt in range(self.config.http_retries):
            self._wait_turn()
            try:
                response = self.transport.get(
                    url, timeout=self.config.request_timeout_s, headers=headers
                )
            except NetworkError as exc:
                last = None
                if attempt == self.config.http_retries - 1:
                    raise NetworkError(f"{url}: {exc}") from exc
            else:
                if response.status not in self.RETRY_STATUSES:
                    return response
                last = response
                if attempt == self.config.http_retries - 1:
                    return response
            backoff = min(8.0, 0.6 * (2**attempt)) + random.uniform(0, 0.3)
            retry_after = (last.headers.get("retry-after") if last else None) or ""
            if retry_after.isdigit():
                backoff = max(backoff, min(30.0, float(retry_after)))
            logger.debug("retrying %s in %.1fs (attempt %d)", url, backoff, attempt + 1)
            self._sleep(backoff)
        raise NetworkError(f"{url}: exhausted retries")

    @staticmethod
    def _wrap(
        response: RawResponse, *, kind: SourceKind, label: str | None, as_of: date | None
    ) -> Payload:
        return Payload(
            url=response.url,
            content=response.content,
            content_type=response.content_type,
            status=response.status,
            as_of=as_of,
            kind=kind,
            label=label or f"{kind.value}: {response.url}",
            from_cache=response.from_cache,
            retrieved_at=response.retrieved_at,
            sha256=response.sha256,
        )


# --------------------------------------------------------------------------- #
# Validation helpers shared by the scrapers
# --------------------------------------------------------------------------- #


def require_not_html(payload: Payload) -> None:
    """Reject an HTML error/landing page where a data file was expected."""

    head = payload.content[:400].lstrip().lower()
    is_html = (
        payload.content_type.startswith("text/html")
        or head.startswith(b"<!doctype html")
        or head.startswith(b"<html")
    )
    page_endpoint = payload.url.endswith(
        ("market-watch", "announcement", "Defaulting", "circuit-breakers", "/psx/", "/market-summary/")
    )
    if is_html and not page_endpoint:
        raise PayloadRejected(f"HTML page returned for a data endpoint: {payload.url}")


def require_csv_like(payload: Payload, *, min_rows: int = 5) -> None:
    """Reject a payload that is not a CSV with at least ``min_rows`` lines."""

    text = payload.content.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < min_rows:
        raise PayloadRejected(f"fewer than {min_rows} non-empty lines at {payload.url}")
    if "," not in lines[0]:
        raise PayloadRejected(f"no comma-separated header at {payload.url}")
