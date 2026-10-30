"""Byte-level decoding for what PSX actually serves.

The portal is a 2000s-era web application: daily files arrive as gzip, as ZIP
archives of CSVs, and as classic Unix ``.Z`` (LZW) blobs; pages arrive as
HTML tables.  This module turns all of that into plain text/rows without
pulling in a browser stack, so the desk stays installable on a broker's
locked-down Python.

Nothing here interprets market data — it only unpacks it.
"""

from __future__ import annotations

import bz2
import codecs
import csv
import gzip
import io
import re
import shutil
import subprocess
import zipfile
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser

from psx_desk.dataflows.http import PayloadRejected

# --------------------------------------------------------------------------- #
# Format sniffing
# --------------------------------------------------------------------------- #

_GZIP_MAGIC = b"\x1f\x8b"
_COMPRESS_MAGIC = b"\x1f\x9d"
_ZIP_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF"
_BZ2_MAGIC = b"BZh"

TEXT_SUFFIXES = (".csv", ".txt", ".lst")
TABLE_SUFFIXES = (".csv", ".txt", ".lst", ".xls", ".xlsx")


def sniff(data: bytes) -> str:
    """Classify a payload by its leading bytes (never by its URL)."""

    if not data:
        return "empty"
    head = data[:1024]
    if head.startswith(_GZIP_MAGIC):
        return "gzip"
    if head.startswith(_COMPRESS_MAGIC):
        return "compress"
    if head.startswith(_ZIP_MAGIC):
        return "zip"
    if head.startswith(_PDF_MAGIC):
        return "pdf"
    if head.startswith(_BZ2_MAGIC):
        return "bz2"
    stripped = head.lstrip().lower()
    if stripped.startswith((b"<!doctype html", b"<html", b"<table", b"<div")):
        return "html"
    if stripped.startswith(b"{"):
        return "json"
    if stripped.startswith(b"["):
        return "json"
    return "text"


# --------------------------------------------------------------------------- #
# Decompression
# --------------------------------------------------------------------------- #


class BitReader:
    """LSB-first bit reader, as required by the Unix ``compress`` format."""

    __slots__ = ("_data", "_pos", "_buffer", "_bits")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self._buffer = 0
        self._bits = 0

    def read(self, width: int) -> int | None:
        while self._bits < width:
            if self._pos >= len(self._data):
                return None
            self._buffer |= self._data[self._pos] << self._bits
            self._pos += 1
            self._bits += 8
        value = self._buffer & ((1 << width) - 1)
        self._buffer >>= width
        self._bits -= width
        return value


def decode_unix_compress(data: bytes) -> bytes:
    """Decompress a classic ``.Z`` (LZW) stream.

    Follows the on-disk format used by ``ncompress``: a 3-byte header
    (``1f 9d FLAGS``) where the low 5 bits of ``FLAGS`` are the maximum code
    width (9-16) and ``0x80`` marks block mode.  Codes are packed
    least-significant-bit first, start at 9 bits, and the width grows as the
    dictionary fills.

    This is the *portable fallback*.  :func:`external_uncompress` runs the
    platform's own decoder first, because the historical encoder changes code
    width lazily at byte refills and only a bit-exact implementation interops
    with every stream ever produced.  A stream this decoder cannot read raises
    :class:`PayloadRejected`, which downgrades that one optional dataset rather
    than corrupting a price.
    """

    if not data.startswith(_COMPRESS_MAGIC):
        raise PayloadRejected("not a .Z stream (bad magic)")
    if len(data) < 4:
        raise PayloadRejected("truncated .Z header")

    flags = data[2]
    max_bits = flags & 0x1F
    block_mode = bool(flags & 0x80)
    if not 9 <= max_bits <= 16:
        raise PayloadRejected(f".Z header declares {max_bits} bits per code; expected 9-16")

    max_max_code = 1 << max_bits
    reader = BitReader(data[3:])
    out = bytearray()

    table: list[bytes] = [bytes((i,)) for i in range(256)] + [b""]
    n_bits = 9
    max_code = (1 << n_bits) - 1
    free_ent = 257 if block_mode else 256

    def reset_dictionary() -> None:
        # gzip's unlzw (the reference decoder for this format) drops the CLEAR
        # slot and restarts allocation at index 256.
        nonlocal table, n_bits, max_code, free_ent
        table = table[:256]
        n_bits = 9
        max_code = (1 << n_bits) - 1
        free_ent = 256

    old_code: int | None = None
    fin_char = 0
    while True:
        current = reader.read(n_bits)
        if current is None:
            break

        if block_mode and current == 256:
            reset_dictionary()
            old_code = None
            continue

        if old_code is None:
            if current > 255:
                raise PayloadRejected(f".Z stream starts with non-literal code {current}")
            out += table[current]
            old_code = current
            fin_char = current
            continue

        if current >= free_ent:
            if current > free_ent:
                raise PayloadRejected(
                    f".Z stream references code {current} beyond dictionary ({free_ent})"
                )
            entry = table[old_code] + table[fin_char][:1]
        else:
            entry = table[current]

        out += entry
        if free_ent < max_max_code:
            table.append(table[old_code] + entry[:1])
            free_ent += 1
        old_code = current
        fin_char = entry[0]

        if free_ent > max_code and n_bits < max_bits:
            n_bits += 1
            max_code = max_max_code if n_bits == max_bits else (1 << n_bits) - 1

    return bytes(out)


def external_uncompress(data: bytes, *, timeout: float = 30.0) -> bytes | None:
    """Decompress ``.Z`` with the system's ``uncompress``/``gzip`` when present.

    The historical ``compress`` format packs variable-width codes with lazy
    buffer refills, and a system decoder is bit-exact by construction.  The pure
    Python decoder below is the portable fallback for hosts without the binary.
    """

    for binary, argv in (
        ("uncompress", ["uncompress", "-c"]),
        ("gzip", ["gzip", "-dc"]),
    ):
        path = shutil.which(binary)
        if not path:
            continue
        try:
            result = subprocess.run(
                argv,
                input=data,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - platform specific
            continue
        if result.returncode == 0 and result.stdout:
            return result.stdout
    return None


def decompress(data: bytes, *, url: str = "") -> bytes:
    """Undo whatever transport encoding a payload carries.

    Tries the magic bytes first, then a small chain of fallbacks so a
    mislabelled file still has a chance, and finally returns the bytes
    untouched (they may genuinely be plain CSV/HTML).
    """

    kind = sniff(data)
    try:
        if kind == "gzip":
            return gzip.decompress(data)
        if kind == "bz2":
            return bz2.decompress(data)
        if kind == "compress":
            # Prefer the platform tool (it is the reference implementation), then
            # fall back to the bundled decoder.
            return external_uncompress(data) or decode_unix_compress(data)
    except (OSError, EOFError, zlib.error, PayloadRejected) as exc:
        raise PayloadRejected(f"cannot decompress {url or 'payload'}: {exc}") from exc

    # Fallback chain: some PSX-era files are gzipped without the magic.
    for attempt in (gzip.decompress, bz2.decompress, zlib.decompress):
        try:
            return attempt(data)
        except Exception:
            continue
    return data


def text_of(data: bytes, *, url: str = "") -> str:
    """Decompress and decode to text, tolerating UTF-8/CP1252/UTF-16."""

    raw = decompress(data, url=url)
    for encoding in ("utf-8-sig", "cp1252", "utf-16"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# ZIP archives
# --------------------------------------------------------------------------- #


def zip_members(data: bytes) -> dict[str, bytes]:
    """Every file inside a ZIP payload, keyed by name (directories skipped)."""

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PayloadRejected(f"not a ZIP archive: {exc}") from exc
    members: dict[str, bytes] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        members[info.filename] = archive.read(info)
    return members


def tabular_member(members: dict[str, bytes]) -> tuple[str, bytes]:
    """Pick the most table-like member of an archive.

    PSX ships ``.csv`` inside some archives and ``.xls`` (often really
    tab-separated or HTML) inside others, so preference is by extension, then
    by size.
    """

    if not members:
        raise PayloadRejected("archive contains no files")
    ranked = sorted(
        members.items(),
        key=lambda kv: (
            0 if kv[0].lower().endswith(".csv") else 1 if kv[0].lower().endswith(TABLE_SUFFIXES) else 2,
            -len(kv[1]),
        ),
    )
    return ranked[0]


# --------------------------------------------------------------------------- #
# CSV / delimited text
# --------------------------------------------------------------------------- #


def read_delimited(text: str) -> list[list[str]]:
    """Parse CSV/TSV/pipe-delimited text into rows of stripped cells.

    The delimiter is guessed from the header line, because PSX uses commas in
    the portal files, tabs in the legacy ``.lis`` dumps and pipes in a few
    regulatory extracts.
    """

    head = next((ln for ln in text.splitlines() if ln.strip()), "")
    delimiter = max((",", "\t", "|", ";"), key=head.count)
    if head.count(delimiter) == 0:
        delimiter = ","
    rows = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        cells = [cell.strip() for cell in row]
        if any(cells):
            rows.append(cells)
    return rows


_WS = re.compile(r"[^a-z0-9]+")


def normalize_header(name: str) -> str:
    """``"% Change"`` -> ``pct_change``; ``"LTP"`` stays ``ltp``.

    A leading or trailing percent sign is spelled out before punctuation is
    stripped, otherwise ``"CHANGE"`` and ``"%CHANGE"`` collapse to the same key
    and one of the two PSX columns is lost.
    """

    text = (name or "").strip().lower().replace("%", " pct ")
    cleaned = _WS.sub("_", text).strip("_")
    return cleaned or "col"


#: Canonical column names and the spellings PSX uses for them.
COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "symbols", "scrip", "scrip_code", "ticker", "symbol_code"),
    "company_name": ("company_name", "company", "name", "long_name", "security_name"),
    "sector": ("sector", "sector_name", "industry"),
    "open": ("open", "open_price", "opening", "o"),
    "high": ("high", "high_price", "h"),
    "low": ("low", "low_price", "l"),
    "close": ("close", "closing", "closing_price", "close_price", "ltp", "last", "current", "c"),
    "prev_close": ("prev_close", "previous_close", "previous", "prev", "pc"),
    "change": ("change", "net_change", "difference", "chg"),
    "pct_change": ("pct_change", "change_pct", "percent_change", "change_percent", "chg_pct"),
    "volume": ("volume", "vol", "volume_traded", "traded_volume", "shares_traded"),
    "value": ("value", "value_traded", "turnover", "traded_value", "value_in_rs"),
    "trades": ("trades", "no_of_trades", "trade_count", "number_of_trades"),
    "upper_cap": (
        "upper_cap",
        "upper_circuit",
        "upper_limit",
        "upper_lock",
        "upper_price",
        "upper",
        "upper_cap_price",
    ),
    "lower_cap": (
        "lower_cap",
        "lower_circuit",
        "lower_limit",
        "lower_lock",
        "lower_price",
        "lower",
        "lower_cap_price",
    ),
    "free_float": ("free_float", "free_float_shares", "freefloat", "free_float_capital"),
    "free_float_mcap": ("free_float_mcap", "free_float_market_cap", "free_float_cap"),
    "market_cap": ("market_cap", "mcap", "market_capitalisation", "market_capitalization"),
    "board": ("board", "market", "board_type"),
    "date": ("date", "trade_date", "session", "business_date"),
}

_SYNONYM_LOOKUP: dict[str, str] = {
    alias: canonical for canonical, aliases in COLUMN_SYNONYMS.items() for alias in aliases
}


def map_columns(headers: list[str]) -> dict[int, str]:
    """Map each source column index to a canonical name (or ``col_<i>``)."""

    mapping: dict[int, str] = {}
    seen: dict[str, int] = {}
    for index, header in enumerate(headers):
        normalized = normalize_header(header)
        canonical = _SYNONYM_LOOKUP.get(normalized, normalized or f"col_{index}")
        if canonical in seen:
            canonical = f"{canonical}_{seen[canonical]}"
        seen[canonical] = seen.get(canonical, 0) + 1
        mapping[index] = canonical
    return mapping


@dataclass(slots=True)
class Table:
    """A parsed delimited table with canonical headers."""

    headers: list[str]
    rows: list[dict[str, str]]

    def __len__(self) -> int:
        return len(self.rows)

    def column(self, name: str) -> list[str]:
        return [row.get(name, "") for row in self.rows]

    def find_symbol_column(self) -> str | None:
        if "symbol" in self.headers:
            return "symbol"
        for header in self.headers:
            sample = [row.get(header, "") for row in self.rows[:20]]
            hits = sum(1 for value in sample if re.fullmatch(r"[A-Z][A-Z0-9]{1,11}", value.strip()))
            if sample and hits >= max(2, int(0.6 * len(sample))):
                return header
        return None


def parse_table(text: str, *, min_columns: int = 2) -> Table:
    """Parse delimited text into a :class:`Table`.

    The header row is the first row that has at least ``min_columns``
    non-empty cells; PSX files frequently begin with a title line or two.
    """

    raw_rows = read_delimited(text)
    for index, row in enumerate(raw_rows[:8]):
        if len([c for c in row if c]) >= min_columns:
            headers = [normalize_header(h) or f"col_{i}" for i, h in enumerate(row)]
            mapping = map_columns(row)
            headers = [mapping[i] for i in range(len(row))]
            body = raw_rows[index + 1 :]
            break
    else:
        raise PayloadRejected("no header row found in delimited payload")

    rows = []
    for raw in body:
        if not any(cell.strip() for cell in raw):
            continue
        record = {}
        for i, cell in enumerate(raw):
            record[headers[i] if i < len(headers) else f"col_{i}"] = cell
        rows.append(record)
    if not rows:
        raise PayloadRejected("delimited payload has a header but no data rows")
    return Table(headers=headers, rows=rows)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #


class _TableExtractor(HTMLParser):
    """Collects every ``<table>``'s rows as text cells."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._stack: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            if any(self._row):
                self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            table = self._stack.pop()
            if table:
                self.tables.append(table)
                if self._stack:
                    # Nested tables: fold the inner one into the parent so rows
                    # are not lost on PSX's layout-table-heavy pages.
                    self._stack[-1].extend(table)

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_tables(html: str) -> list[list[list[str]]]:
    parser = _TableExtractor()
    parser.feed(html)
    parser.close()
    return parser.tables


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        elif tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "td"}:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)


def html_text(html: str) -> str:
    """Readable text of a page, one line per block, whitespace collapsed."""

    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.chunks)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def table_as_records(rows: list[list[str]]) -> Table:
    """Convert an HTML table (list of rows) into a canonical :class:`Table`."""

    if not rows:
        raise PayloadRejected("empty HTML table")
    header_index = 0
    for index, row in enumerate(rows[:6]):
        if sum(1 for cell in row if cell) >= 3:
            header_index = index
            break
    headers_row = rows[header_index]
    mapping = map_columns(headers_row)
    headers = [mapping[i] for i in range(len(headers_row))]
    records = []
    for raw in rows[header_index + 1 :]:
        if len([c for c in raw if c]) < 2:
            continue
        records.append({headers[i] if i < len(headers) else f"col_{i}": cell for i, cell in enumerate(raw)})
    if not records:
        raise PayloadRejected("HTML table had no data rows")
    return Table(headers=headers, rows=records)


def decode_text_payload(text: str) -> str:
    """Repair mojibake produced by PSX's mixed ISO-8859/UTF-8 pages."""

    try:
        return codecs.decode(text.encode("latin-1", errors="strict"), "utf-8", errors="strict")
    except (UnicodeError, LookupError):
        return text
