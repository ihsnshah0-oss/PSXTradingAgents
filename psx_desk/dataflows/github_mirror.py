"""GitHub-hosted PSX data: live snapshot mirrors and historical OHLCV datasets.

Why this exists
---------------
``dps.psx.com.pk`` is the only authoritative source, and
:mod:`psx_desk.dataflows.portal` already scrapes it.  But a desk also needs a
plan for the days the exchange's portal is slow, rate-limited, behind a
corporate proxy, or serving a session that was never published as a dated file.
Several maintainers publish PSX data on GitHub for exactly that purpose, and
this module reads them through one narrow, well-labelled interface.

Two kinds of mirror are supported, both verified against their real payloads:

.. list-table::
   :header-rows: 1

   * - Preset
     - Repository
     - What it carries
   * - ``psx-data``
     - ``bigbajwatv-alt/psx-data``
     - Whole-board snapshot (484 scrips: close, prev close, OHLC, volume),
       symbol directory with sectors, KSE index levels, sector membership.
   * - ``psx-history``
     - ``Muhammad-Wasif/PSX-Stock-Market-Dataset``
     - ~10 years of daily OHLCV for 105 KSE-100 names (per-company CSVs plus a
       12 MB combined file), company-name → symbol map, index constituents.

**Mirror data is never exchange data.**  Every row and bar produced here carries
``mirror="<owner>/<repo>"`` and a normal ``SourceRef`` pointing at the exact
GitHub URL (plus a SHA-256), the ingestion node forces such packets to
``data_quality="partial"``, and the mirror is only consulted when the desk is
explicitly configured to allow it (``PSXDESK_GITHUB_MIRROR`` /
``PSXDESK_MIRROR_POLICY``).  With the default policy (``off``) nothing in this
module is ever called.

Access paths (first that works wins)
------------------------------------
1. ``raw.githubusercontent.com`` — cheapest, no rate limit worth worrying about.
2. ``api.github.com/repos/.../contents/...`` — base64 payload from the API;
   unauthenticated callers get 60 requests/hour, so a token is honoured via
   ``PSXDESK_GITHUB_TOKEN``.
3. ``codeload.github.com/{repo}/tar.gz/{ref}`` — one request for the whole
   repository, used for the history datasets where per-file fetching would be
   dozens of calls.

Nothing here writes to the desk's decisions by itself: the output types are the
same :class:`~psx_desk.dataflows.portal.MarketDump` and
:class:`~psx_desk.dataflows.timeseries.PriceHistory` the PSX scrapers return, so
downstream code cannot tell the difference except through the provenance it is
required to read.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote, unquote

from psx_desk.config import DeskConfig, load_config
from psx_desk.dataflows.archive import parse_table
from psx_desk.dataflows.http import Payload, PortalClient, PortalError
from psx_desk.dataflows.portal import MarketDump, MarketRow
from psx_desk.dataflows.timeseries import Bar, PriceHistory
from psx_desk.schemas import DataQuality, IndexSnapshot, SourceRef, coerce_pct, coerce_price
from psx_desk.sources import (
    SOURCE_MIRROR_GITHUB,
    SourceKind,
    mirror_url,
    normalize_symbol,
)

logger = logging.getLogger(__name__)

#: Role names a mirror file can play.
ROLE_BOARD = "board"
ROLE_SYMBOLS = "symbols"
ROLE_INDICES = "indices"
ROLE_SECTORS = "sectors"
ROLE_HISTORY = "history"
ROLE_HISTORY_MAP = "history_map"

DEFAULT_MIRROR_TTL_SECONDS = 900
TARBALL_TTL_SECONDS = 6 * 3600


class MirrorUnavailable(PortalError):
    """The configured GitHub mirror could not supply the requested data."""


class MirrorNotConfigured(MirrorUnavailable):
    """No mirror preset is configured, or the policy forbids using one."""


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MirrorFile:
    """One file this mirror exposes, and the role its contents play."""

    path: str
    role: str
    fmt: str = "json"          # json | csv
    is_template: bool = False  # path needs formatting (e.g. stocks/{filename}.csv)


@dataclass(frozen=True, slots=True)
class MirrorPreset:
    """A GitHub repository that publishes PSX data, plus how to read it."""

    key: str
    repo: str
    kind: str                       # snapshot | history
    files: tuple[MirrorFile, ...]
    ref: str = "main"
    attribution: str = ""
    licence: str = "see the repository"
    note: str = ""

    def file_for(self, role: str) -> MirrorFile | None:
        return next((f for f in self.files if f.role == role), None)

    @property
    def slug(self) -> str:
        return f"github:{self.repo}"


MIRROR_PRESETS: dict[str, MirrorPreset] = {
    # Live whole-board snapshot, refreshed by a scheduled collector.
    "psx-data": MirrorPreset(
        key="psx-data",
        repo="bigbajwatv-alt/psx-data",
        kind="snapshot",
        ref="main",
        attribution="bigbajwatv-alt/psx-data",
        licence="see the repository",
        note="Community mirror of dps.psx.com.pk market-watch; rows are undated intraday snapshots.",
        files=(
            MirrorFile("daily_summary.json", ROLE_BOARD),
            MirrorFile("symbols.json", ROLE_SYMBOLS),
            MirrorFile("indices_eod.json", ROLE_INDICES),
            MirrorFile("sectors.json", ROLE_SECTORS),
            MirrorFile("latest_snapshot.json", ROLE_BOARD),
        ),
    ),
    # ~10 years of daily OHLCV for KSE-100 constituents.
    "psx-history": MirrorPreset(
        key="psx-history",
        repo="Muhammad-Wasif/PSX-Stock-Market-Dataset",
        kind="history",
        ref="main",
        attribution="Muhammad-Wasif/PSX-Stock-Market-Dataset",
        licence="MIT (per the repository)",
        note="Static research dataset: 2016-2026 daily OHLCV, 105 KSE-100 names, anomaly-flagged.",
        files=(
            MirrorFile("metadata/PSX_Company_Name_To_Symbol_Mapping.csv", ROLE_HISTORY_MAP, "csv"),
            MirrorFile("stocks/{filename}", ROLE_HISTORY, "csv", is_template=True),
            MirrorFile("combined/PSX_KSE100_Full_Historical_Daily.csv", ROLE_HISTORY, "csv"),
            MirrorFile("metadata/PSX_All_Listed_Companies.csv", ROLE_SYMBOLS, "csv"),
            MirrorFile("metadata/PSX_Sectors_Summary.csv", ROLE_SECTORS, "csv"),
            MirrorFile("indices/KSE100_Constituents.csv", ROLE_INDICES, "csv"),
        ),
    ),
}

DEFAULT_SNAPSHOT_MIRROR = "psx-data"
DEFAULT_HISTORY_MIRROR = "psx-history"


def mirror_catalog() -> list[dict[str, Any]]:
    """Presets, for ``psx-desk mirror list`` and documentation."""

    return [
        {
            "key": preset.key,
            "repo": preset.repo,
            "kind": preset.kind,
            "ref": preset.ref,
            "files": [f.path for f in preset.files],
            "licence": preset.licence,
            "note": preset.note,
        }
        for preset in MIRROR_PRESETS.values()
    ]


def resolve_mirror(spec: str | None) -> MirrorPreset:
    """Turn a preset key or ``owner/repo`` string into a :class:`MirrorPreset`.

    Unknown repositories are accepted as generic snapshot mirrors (the two most
    common file names are probed), so a desk can point at its own fork without
    touching this file.
    """

    if spec is None or str(spec).strip() == "":
        raise MirrorNotConfigured(
            "no GitHub mirror configured; set PSXDESK_GITHUB_MIRROR to a preset "
            f"({', '.join(sorted(MIRROR_PRESETS))}) or 'owner/repo'"
        )
    key = str(spec).strip()
    if key.startswith("github:"):
        key = key.split(":", 1)[1].strip()
    if key in MIRROR_PRESETS:
        return MIRROR_PRESETS[key]
    if key.count("/") == 1 and " " not in key:
        return MirrorPreset(
            key=key,
            repo=key,
            kind="snapshot",
            ref="main",
            attribution=key,
            note="Generic GitHub mirror; board file auto-detected.",
            files=(
                MirrorFile("daily_summary.json", ROLE_BOARD),
                MirrorFile("latest_snapshot.json", ROLE_BOARD),
                MirrorFile("symbols.json", ROLE_SYMBOLS),
                MirrorFile("indices_eod.json", ROLE_INDICES),
            ),
        )
    raise MirrorNotConfigured(
        f"unknown mirror {spec!r}; use one of {sorted(MIRROR_PRESETS)} or 'owner/repo'"
    )


# --------------------------------------------------------------------------- #
# Parsers — pure functions over the mirrors' real schemas
# --------------------------------------------------------------------------- #


def _as_of(value: Any) -> date | None:
    """Parse the many ``as_of``/``timestamp`` shapes these repos use."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=UTC).date()
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _as_of(float(text))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def parse_snapshot_board(text: str) -> tuple[dict[str, MarketRow], date | None]:
    """Parse ``daily_summary.json`` or ``latest_snapshot.json``.

    ``daily_summary`` uses ``sym``/``ldcp`` (last day's close, i.e. the previous
    close) while ``latest_snapshot`` uses ``watchlist`` keyed by symbol with
    ``day_high``/``day_low``.  Both become the desk's :class:`MarketRow`.
    """

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MirrorUnavailable(f"mirror payload is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MirrorUnavailable("mirror payload is not a JSON object")

    raw_rows: list[dict[str, Any]] = []
    if isinstance(data.get("stocks"), list):
        raw_rows = [row for row in data["stocks"] if isinstance(row, dict)]
        as_of = _as_of(data.get("as_of"))
    elif isinstance(data.get("watchlist"), dict):
        raw_rows = [row for row in data["watchlist"].values() if isinstance(row, dict)]
        as_of = _as_of(data.get("timestamp") or data.get("as_of"))
    elif isinstance(data.get("data"), list):
        raw_rows = [row for row in data["data"] if isinstance(row, dict)]
        as_of = _as_of(data.get("as_of") or data.get("timestamp"))
    else:
        raise MirrorUnavailable("unrecognised mirror board schema")

    rows: dict[str, MarketRow] = {}
    for raw in raw_rows:
        symbol = str(raw.get("sym") or raw.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        close = coerce_price(raw.get("close"))
        prev_close = coerce_price(raw.get("ldcp") if raw.get("ldcp") is not None else raw.get("prev_close"))
        if close is None or close <= 0:
            close = prev_close
        if close is None or close <= 0:
            continue
        change = coerce_price(raw.get("change"))
        if change is None and prev_close:
            change = round(close - prev_close, 4)
        pct = coerce_pct(raw.get("pct") if raw.get("pct") is not None else raw.get("change_pct"))
        if pct is None and prev_close:
            pct = round(100.0 * (close - prev_close) / prev_close, 4)
        volume = coerce_price(raw.get("volume"))
        open_price = coerce_price(raw.get("open"))
        high = coerce_price(raw.get("high") if raw.get("high") is not None else raw.get("day_high"))
        low = coerce_price(raw.get("low") if raw.get("low") is not None else raw.get("day_low"))
        rows[symbol] = MarketRow(
            symbol=symbol,
            close=close,
            prev_close=prev_close,
            change=change,
            pct_change=pct,
            open=open_price,
            high=high,
            low=low,
            volume=volume,
            value=round(volume * close, 2) if volume else None,
            board="REG",
        )

    if not rows:
        raise MirrorUnavailable("mirror board parsed but contained no valid scrips")
    return rows, as_of


@dataclass(frozen=True, slots=True)
class MirrorSymbolInfo:
    """Reference data about a scrip (never a price)."""

    symbol: str
    name: str | None = None
    sector: str | None = None
    is_etf: bool = False
    is_debt: bool = False


def parse_symbol_directory(text: str) -> dict[str, MirrorSymbolInfo]:
    """Parse ``symbols.json`` (JSON) or a listed-companies CSV."""

    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        entries = data.get("symbols") if isinstance(data, dict) else data
        info: dict[str, MirrorSymbolInfo] = {}
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            symbol = str(entry.get("symbol") or entry.get("sym") or "").strip().upper()
            if not symbol:
                continue
            info[symbol] = MirrorSymbolInfo(
                symbol=symbol,
                name=(str(entry.get("name")).strip() if entry.get("name") else None),
                sector=(str(entry.get("sectorName") or entry.get("sector") or "").strip() or None),
                is_etf=bool(entry.get("isETF")),
                is_debt=bool(entry.get("isDebt")),
            )
        return info

    table = parse_table(text, min_columns=2)
    symbol_column = table.find_symbol_column() or "symbol"
    info = {}
    for record in table.rows:
        symbol = (record.get(symbol_column) or "").strip().upper()
        if not symbol:
            continue
        info[symbol] = MirrorSymbolInfo(
            symbol=symbol,
            name=(record.get("company_name") or record.get("name") or "").strip() or None,
            sector=(record.get("sector_name") or record.get("sector") or "").strip() or None,
        )
    return info


def parse_indices(text: str) -> list[IndexSnapshot]:
    """Parse ``indices_eod.json`` (dict of index → OHLC) into snapshots."""

    data = json.loads(text)
    if not isinstance(data, dict):
        return []
    indices = data.get("indices") if isinstance(data.get("indices"), dict) else {}
    snapshots: list[IndexSnapshot] = []
    for symbol, values in indices.items():
        if not isinstance(values, dict):
            continue
        close = coerce_price(values.get("close"))
        if close is None or close <= 0:
            continue
        snapshots.append(
            IndexSnapshot(
                symbol=str(symbol).upper(),
                close=close,
                change_pct=coerce_pct(values.get("pct") if values.get("pct") is not None else values.get("change_pct")),
                volume_shares=coerce_price(values.get("volume")),
                as_of=_as_of(values.get("ts") or values.get("as_of")),
            )
        )
    return snapshots


def parse_history_csv(
    text: str,
    *,
    symbol: str,
    as_of: date | None = None,
    days: int | None = None,
    drop_anomalies: bool = True,
) -> tuple[list[Bar], int]:
    """Parse a historical OHLCV CSV into bars, oldest first.

    Handles both the per-company files (``company_name,symbol,date,open,high,
    low,close,volume,is_anomaly``) and the combined file with the same header.
    Rows flagged ``is_anomaly`` are dropped by default and counted, because the
    dataset's own maintainer marks corporate-action artefacts there.
    """

    wanted = normalize_symbol(symbol)
    table = parse_table(text, min_columns=4)
    if "date" not in table.headers:
        raise MirrorUnavailable(f"history CSV for {wanted} has no date column")

    bars: list[Bar] = []
    dropped = 0
    for record in table.rows:
        row_symbol = (record.get("symbol") or record.get("sym") or "").strip().upper()
        if row_symbol and row_symbol != wanted:
            continue
        session = _as_of(record.get("date"))
        close = coerce_price(record.get("close"))
        if session is None or close is None or close <= 0:
            continue
        if as_of is not None and session > as_of:
            continue
        if drop_anomalies and str(record.get("is_anomaly", "")).strip().lower() in {"true", "1", "yes"}:
            dropped += 1
            continue
        volume = coerce_price(record.get("volume"))
        high = coerce_price(record.get("high"))
        low = coerce_price(record.get("low"))
        open_price = coerce_price(record.get("open"))
        # A flat row with high == low == close == open is a placeholder print in
        # this dataset; keep the close but do not pretend it had intraday range.
        if high is not None and low is not None and high == low == close and open_price == close:
            high = low = None
        bars.append(
            Bar(
                trade_date=session,
                close=close,
                open=open_price,
                high=high,
                low=low,
                volume=volume,
            )
        )

    if not bars:
        raise MirrorUnavailable(f"history CSV contained no usable rows for {wanted}")
    by_date = {bar.trade_date: bar for bar in bars}
    ordered = [by_date[key] for key in sorted(by_date)]
    if days:
        ordered = ordered[-days:]
    return ordered, dropped


def parse_history_index(text: str) -> dict[str, str]:
    """Map symbol → stock CSV filename from the dataset's mapping file."""

    table = parse_table(text, min_columns=2)
    mapping: dict[str, str] = {}
    for record in table.rows:
        symbol = (record.get("symbol") or "").strip().upper()
        filename = (record.get("filename") or record.get("file") or "").strip()
        if symbol and filename:
            mapping[symbol] = filename
    return mapping


# --------------------------------------------------------------------------- #
# Fetcher
# --------------------------------------------------------------------------- #


@dataclass
class GitHubMirror:
    """Reads one mirror preset through the shared PSX HTTP client."""

    preset: MirrorPreset
    client: PortalClient
    config: DeskConfig
    via: str = "auto"                      # auto | raw | api | tarball
    token: str | None = None
    _tarball: bytes | None = field(default=None, init=False, repr=False)
    _sources: list[SourceRef] = field(default_factory=list, init=False, repr=False)

    # -- transport --------------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        token = self.token or self.config.github_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _get(self, url: str, *, ttl: int, label: str, as_of: date | None) -> Payload:
        payload = self.client.get(
            url,
            kind=SourceKind.MIRROR,
            label=label,
            as_of=as_of,
            ttl_seconds=ttl,
            allow_missing=True,
            allow_mirror=True,
            headers=self._headers(),
        )
        if payload is None:
            raise MirrorUnavailable(f"404 at {url}")
        self._sources.append(payload.source_ref())
        return payload

    def text(self, path: str, *, ttl: int = DEFAULT_MIRROR_TTL_SECONDS) -> str:
        """Fetch one file's text, trying the configured access paths in order."""

        attempts: list[str] = []
        strategies = [self.via] if self.via != "auto" else ["raw", "api", "tarball"]
        for strategy in strategies:
            try:
                if strategy == "raw":
                    return self._raw_text(path, ttl=ttl)
                if strategy == "api":
                    return self._api_text(path, ttl=ttl)
                if strategy == "tarball":
                    return self._tarball_text(path, ttl=ttl)
            except (MirrorUnavailable, PortalError, ValueError, OSError) as exc:
                attempts.append(f"{strategy}: {exc}")
                continue
        raise MirrorUnavailable(
            f"could not read {self.preset.repo}/{path} — " + "; ".join(attempts)
        )

    def _raw_text(self, path: str, *, ttl: int) -> str:
        url = mirror_url(self.preset.repo, self.preset.ref, path, via="raw")
        payload = self._get(url, ttl=ttl, label=f"GitHub mirror {self.preset.repo}/{path}", as_of=None)
        return payload.text

    def _api_text(self, path: str, *, ttl: int) -> str:
        url = mirror_url(self.preset.repo, self.preset.ref, path, via="api")
        payload = self._get(url, ttl=ttl, label=f"GitHub API {self.preset.repo}/{path}", as_of=None)
        try:
            body = json.loads(payload.text)
        except json.JSONDecodeError as exc:
            raise MirrorUnavailable(f"GitHub API returned non-JSON for {path}") from exc
        if not isinstance(body, dict):
            raise MirrorUnavailable(f"GitHub API returned an unexpected shape for {path}")
        content = body.get("content")
        if not content:
            raise MirrorUnavailable(
                f"{path} is too large for the contents API ({body.get('size')} bytes); "
                "use via='tarball'"
            )
        return base64.b64decode(content).decode("utf-8", errors="replace")

    def _tarball_bytes(self, *, ttl: int) -> bytes:
        if self._tarball is not None:
            return self._tarball
        url = mirror_url(self.preset.repo, self.preset.ref, "", via="tarball")
        payload = self._get(
            url, ttl=ttl, label=f"GitHub tarball {self.preset.repo}@{self.preset.ref}", as_of=None
        )
        self._tarball = payload.content
        return self._tarball

    def _tarball_text(self, path: str, *, ttl: int) -> str:
        blob = self._tarball_bytes(ttl=ttl or TARBALL_TTL_SECONDS)
        wanted = {path.lower(), unquote(quote(path, safe="/")).lower()}
        try:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    name = member.name.split("/", 1)[-1]  # strip "<repo>-<ref>/"
                    if name.lower() in wanted or any(
                        name.lower().endswith("/" + w) for w in wanted
                    ):
                        handle = archive.extractfile(member)
                        if handle is None:
                            continue
                        return handle.read().decode("utf-8", errors="replace")
        except tarfile.TarError as exc:
            raise MirrorUnavailable(f"repository tarball is unreadable: {exc}") from exc
        raise MirrorUnavailable(f"{path} is not present in the repository tarball")

    # -- typed accessors ---------------------------------------------------- #

    def board(self, *, symbols: Sequence[str] | None = None) -> tuple[dict[str, MarketRow], date | None]:
        """Return the *newest dated* usable board the preset exposes.

        Mirrors accumulate files over time (a repo may hold both a stale
        ``daily_summary.json`` and a fresher ``latest_snapshot.json``), so the
        file order in the preset is a preference, not a decision: every board
        file is read and the freshest snapshot with the wanted scrips wins.
        """

        wanted = {normalize_symbol(s) for s in symbols} if symbols else None
        errors: list[str] = []
        candidates: list[tuple[date | None, dict[str, MarketRow]]] = []
        for mirror_file in self.preset.files:
            if mirror_file.role != ROLE_BOARD:
                continue
            try:
                rows, as_of = parse_snapshot_board(self.text(mirror_file.path))
            except (MirrorUnavailable, ValueError) as exc:
                errors.append(f"{mirror_file.path}: {exc}")
                continue
            if wanted:
                rows = {k: v for k, v in rows.items() if k in wanted}
            if rows:
                candidates.append((as_of, rows))
            else:
                errors.append(f"{mirror_file.path}: no scrips matched the filter")

        if not candidates:
            raise MirrorUnavailable(
                f"{self.preset.repo} supplied no usable board — "
                + "; ".join(errors or ["no board file"])
            )
        dated = [item for item in candidates if item[0] is not None]
        if dated:
            freshest = max(dated, key=lambda item: item[0])
            stale = sorted(
                {item[0].isoformat() for item in dated if item[0] < freshest[0]}
            )
            if stale:
                logger.info(
                    "mirror %s: ignored older snapshot(s) %s in favour of %s",
                    self.preset.repo,
                    ", ".join(stale),
                    freshest[0].isoformat(),
                )
            return freshest[1], freshest[0]
        return candidates[0]

    def symbols_directory(self) -> dict[str, MirrorSymbolInfo]:
        mirror_file = self.preset.file_for(ROLE_SYMBOLS)
        if mirror_file is None:
            return {}
        try:
            return parse_symbol_directory(self.text(mirror_file.path))
        except (MirrorUnavailable, ValueError) as exc:
            logger.info("mirror %s symbols unavailable: %s", self.preset.repo, exc)
            return {}

    def indices(self) -> list[IndexSnapshot]:
        mirror_file = self.preset.file_for(ROLE_INDICES)
        if mirror_file is None or mirror_file.fmt != "json":
            return []
        try:
            return parse_indices(self.text(mirror_file.path))
        except (MirrorUnavailable, ValueError) as exc:
            logger.info("mirror %s indices unavailable: %s", self.preset.repo, exc)
            return []

    def history(
        self, symbol: str, *, as_of: date | None = None, days: int | None = None
    ) -> PriceHistory:
        wanted = normalize_symbol(symbol)
        mapping_file = self.preset.file_for(ROLE_HISTORY_MAP)
        errors: list[str] = []

        if mapping_file is not None:
            try:
                index = parse_history_index(self.text(mapping_file.path))
            except (MirrorUnavailable, ValueError) as exc:
                index = {}
                errors.append(f"index: {exc}")
            filename = index.get(wanted)
            template = self.preset.file_for(ROLE_HISTORY)
            if filename and template is not None:
                path = template.path.format(filename=filename)
                try:
                    bars, dropped = parse_history_csv(
                        self.text(path), symbol=wanted, as_of=as_of, days=days
                    )
                except (MirrorUnavailable, ValueError) as exc:
                    errors.append(f"{path}: {exc}")
                else:
                    if dropped:
                        logger.info("mirror dropped %d anomaly-flagged rows for %s", dropped, wanted)
                    return PriceHistory(
                        symbol=wanted,
                        bars=tuple(bars),
                        source=self._sources[-1] if self._sources else None,
                        has_high_low=all(bar.high is not None for bar in bars[-60:]),
                        quality=DataQuality.PARTIAL,
                    )
            elif not filename:
                errors.append(f"{wanted} is not in the mirror's symbol map")

        # Fall back to the combined file (one request, then filter).
        for mirror_file in self.preset.files:
            if mirror_file.role != ROLE_HISTORY or mirror_file.is_template:
                continue
            try:
                bars, dropped = parse_history_csv(
                    self.text(mirror_file.path, ttl=TARBALL_TTL_SECONDS),
                    symbol=wanted,
                    as_of=as_of,
                    days=days,
                )
            except (MirrorUnavailable, ValueError) as exc:
                errors.append(f"{mirror_file.path}: {exc}")
                continue
            if dropped:
                logger.info("mirror dropped %d anomaly-flagged rows for %s", dropped, wanted)
            return PriceHistory(
                symbol=wanted,
                bars=tuple(bars),
                source=self._sources[-1] if self._sources else None,
                has_high_low=all(bar.high is not None for bar in bars[-60:]),
                quality=DataQuality.PARTIAL,
            )

        raise MirrorUnavailable(
            f"{self.preset.repo} has no history for {wanted} — " + "; ".join(errors)
        )

    @property
    def sources(self) -> list[SourceRef]:
        return list(self._sources)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def _build(
    mirror: str | None,
    config: DeskConfig | None,
    client: PortalClient | None,
    *,
    via: str = "auto",
) -> GitHubMirror:
    config = config or load_config()
    spec = mirror or config.github_mirror
    preset = resolve_mirror(spec)
    client = client or PortalClient(config)
    return GitHubMirror(preset=preset, client=client, config=config, via=via)


def scrape_psx_mirror(
    mirror: str | None = None,
    *,
    symbols: Sequence[str] | None = None,
    config: DeskConfig | None = None,
    client: PortalClient | None = None,
    via: str = "auto",
    as_of: date | None = None,
) -> MarketDump:
    """Scrape a GitHub mirror into the desk's :class:`MarketDump`.

    Args:
        mirror: Preset key (``psx-data``, ``psx-history``) or ``owner/repo``.
            Defaults to ``PSXDESK_GITHUB_MIRROR``.
        symbols: Optional allow-list of PSX scrip codes.
        config: Desk configuration (cache dir, throttle, token).
        client: Reuse an existing :class:`PortalClient` (shares cache/pool).
        via: ``auto`` (raw → API → tarball), ``raw``, ``api`` or ``tarball``.
        as_of: Session the caller asked for; recorded so a stale mirror is
            obvious downstream.

    Returns:
        A :class:`MarketDump` with ``mirror`` set to ``"<owner>/<repo>"`` and
        ``mirror_as_of`` holding the snapshot's own timestamp.  Prices are real
        mirror values, never interpolated; circuit caps and free float are
        reported missing because no GitHub mirror publishes them.

    Raises:
        MirrorNotConfigured: No mirror configured, or the policy is ``off``.
        MirrorUnavailable: The mirror exists but served nothing usable.
    """

    config = config or load_config()
    if config.mirror_policy == "off" and mirror is None:
        raise MirrorNotConfigured(
            "GitHub mirror policy is 'off'; set PSXDESK_MIRROR_POLICY=backfill|fallback "
            "or pass mirror=... explicitly"
        )

    fetcher = _build(mirror, config, client, via=via)
    rows, mirror_date = fetcher.board(symbols=symbols)

    # Reference data (sectors, names) makes the mirror board more useful to the
    # analysts without ever touching a price.
    directory = fetcher.symbols_directory()
    for symbol, row in list(rows.items()):
        info = directory.get(symbol)
        if info is None:
            continue
        rows[symbol] = replace(
            row,
            company_name=row.company_name or info.name,
            sector=row.sector or info.sector,
        )

    budget = config.mirror_max_staleness_days
    if as_of is not None and mirror_date is not None and (as_of - mirror_date).days > budget:
        raise MirrorUnavailable(
            f"mirror {fetcher.preset.repo} is {(as_of - mirror_date).days} days behind "
            f"{as_of.isoformat()} (limit {budget}); refusing to price a session from a "
            "stale snapshot. Raise PSXDESK_MIRROR_MAX_STALENESS_DAYS to override, or fix "
            "the exchange scrape."
        )

    missing = ["free_float_mcap", "exchange_circuit_caps", "trades"]
    if all(row.sector is None for row in rows.values()):
        missing.append("sector")

    rejected: list[str] = []
    if as_of and mirror_date and mirror_date < as_of:
        rejected.append(
            f"mirror {fetcher.preset.repo} :: snapshot dated {mirror_date.isoformat()} "
            f"is older than the requested session {as_of.isoformat()}"
        )
    elif mirror_date is None:
        rejected.append(f"mirror {fetcher.preset.repo} :: snapshot carries no timestamp")
    rejected.append(f"mirror {fetcher.preset.repo} :: community data, not exchange-verified")

    return MarketDump(
        trade_date=as_of or mirror_date or datetime.now(UTC).date(),
        rows=rows,
        sources=tuple(fetcher.sources),
        datasets_used=(f"mirror:{fetcher.preset.repo}",),
        rejected=tuple(rejected),
        missing_fields=tuple(sorted(missing)),
        mirror=fetcher.preset.repo,
        mirror_as_of=mirror_date,
    )


def fetch_mirror_history(
    symbol: str,
    *,
    mirror: str | None = None,
    config: DeskConfig | None = None,
    client: PortalClient | None = None,
    via: str = "auto",
    as_of: date | None = None,
    days: int | None = None,
) -> PriceHistory:
    """Load a symbol's daily history from the ``psx-history`` dataset.

    Returns bars oldest first, truncated at ``as_of`` so a backtest cannot see
    the future, with ``quality="partial"`` because the data is a third-party
    reconstruction rather than the exchange's own file.
    """

    config = config or load_config()
    if config.mirror_policy == "off" and mirror is None:
        raise MirrorNotConfigured(
            "GitHub mirror policy is 'off'; set PSXDESK_MIRROR_POLICY=backfill|fallback "
            "or pass mirror=... explicitly"
        )
    fetcher = _build(mirror or config.github_history_mirror, config, client, via=via)
    return fetcher.history(symbol, as_of=as_of, days=days)


def mirror_symbol_info(
    mirror: str | None = None,
    *,
    config: DeskConfig | None = None,
    client: PortalClient | None = None,
) -> dict[str, MirrorSymbolInfo]:
    """Reference directory (names + sectors) from the mirror, prices excluded."""

    config = config or load_config()
    fetcher = _build(mirror, config, client)
    return fetcher.symbols_directory()


def mirror_indices(
    mirror: str | None = None,
    *,
    config: DeskConfig | None = None,
    client: PortalClient | None = None,
) -> list[IndexSnapshot]:
    """Index levels from the mirror (empty when the mirror publishes none)."""

    config = config or load_config()
    fetcher = _build(mirror, config, client)
    return fetcher.indices()


def mirror_available(
    mirror: str | None = None,
    *,
    config: DeskConfig | None = None,
    client: PortalClient | None = None,
) -> bool:
    """Cheap health check: can this mirror serve a board right now?"""

    try:
        dump = scrape_psx_mirror(mirror, config=config, client=client, symbols=["SYS"])
    except (MirrorUnavailable, PortalError, ValueError):
        return False
    return dump.trade_date is not None and len(dump) > 0


def mirror_urls(preset: MirrorPreset) -> dict[str, str]:
    """Every URL this preset may fetch, for allow-list auditing and the docs."""

    urls = {}
    for mirror_file in preset.files:
        path = mirror_file.path.replace("{filename}", "Example Limited.csv")
        for via in ("raw", "api"):
            try:
                urls[f"{via}:{mirror_file.path}"] = mirror_url(preset.repo, preset.ref, path, via=via)
            except ValueError:  # pragma: no cover - defensive
                continue
    urls["tarball"] = mirror_url(preset.repo, preset.ref, "", via="tarball")
    return urls


# Re-exported so callers can assert the provenance class without importing the
# source registry.
__all__ = [
    "DEFAULT_HISTORY_MIRROR",
    "DEFAULT_SNAPSHOT_MIRROR",
    "GitHubMirror",
    "MIRROR_PRESETS",
    "MirrorFile",
    "MirrorNotConfigured",
    "MirrorPreset",
    "MirrorSymbolInfo",
    "MirrorUnavailable",
    "SOURCE_MIRROR_GITHUB",
    "fetch_mirror_history",
    "mirror_available",
    "mirror_catalog",
    "mirror_indices",
    "mirror_symbol_info",
    "mirror_urls",
    "parse_history_csv",
    "parse_history_index",
    "parse_indices",
    "parse_snapshot_board",
    "parse_symbol_directory",
    "resolve_mirror",
    "scrape_psx_mirror",
]
