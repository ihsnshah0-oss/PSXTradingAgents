"""Corporate disclosures and the PSX Defaulters / Non-Compliant segment.

Two scrapes live here:

1. :func:`load_defaulter_index` — the exchange's own
   *Defaulting-Companies-Profile-Portal*.  The CRO must know whether a scrip is
   on the Defaulters or Non-Compliant segment before it sizes anything; this is
   a hard veto, not a preference.
2. :func:`fetch_disclosures` — PSX/PUCARS company announcements, which is where
   results, board meetings, material information and defaults actually surface
   first for a PSX investor.

Both are conservative: if the page does not parse into the expected structure
the result is marked ``checked=False``, so the CRO reports the gate as UNKNOWN
instead of clearing a company it never verified.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

from psx_desk.dataflows.archive import html_text
from psx_desk.dataflows.http import PortalClient, PortalError
from psx_desk.schemas import DefaulterSegment, DisclosureItem, SourceRef
from psx_desk.sources import (
    DEFAULTER_PORTAL_URL,
    PSX_WEB,
    PUCARS_URL,
    SourceKind,
    normalize_symbol,
)

logger = logging.getLogger(__name__)

_SECTION_STATE = ("current", "past")
_CURRENT_MARKERS = (
    "current non-compliant segment",
    "currently in the non-compliant segment",
    "non-compliant companies and securities",
)
_PAST_MARKERS = (
    "previously quoted in defaulters",
    "previously quoted in defaulter",
    "past default",
    "date of removal from the defaulter",
)
_NOISE = re.compile(r"^[^A-Za-z]*$")
#: Legal-form words only.  Industry words ("power", "bank", "textile") are real
#: parts of the name and must survive, or two different issuers would collide.
_COMPANY_SUFFIXES = (
    "limited", "ltd", "ltd.", "company", "co.", "co", "corporation", "corp",
    "plc", "inc", "incorporated",
)
_DATE_RE = re.compile(r"(\d{1,2})[-/\s]([A-Za-z]{3,9}|\d{1,2})[-/\s](\d{2,4})")


def normalize_company(name: str) -> str:
    """Fold a company name to a comparable key.

    ``"Systems Limited"``, ``"SYSTEMS LTD."`` and ``"Systems Ltd"`` all become
    ``"systems"``.  Legal suffixes and punctuation are dropped because PSX lists
    the same company three different ways across its pages.
    """

    text = re.sub(r"\(.*?\)", " ", (name or "").lower())
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    tokens = [tok for tok in text.split() if tok and tok not in _COMPANY_SUFFIXES]
    # "systems" survives suffix stripping ("limited" is the last token here).
    return " ".join(tokens) or re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).strip()


@dataclass(frozen=True, slots=True)
class DefaulterIndex:
    """The names PSX currently lists as non-compliant or past defaulters."""

    checked: bool
    current: frozenset[str] = frozenset()
    past: frozenset[str] = frozenset()
    source: SourceRef | None = None
    as_of: date | None = None
    note: str = ""

    def check(self, symbol: str, company_name: str | None = None) -> DefaulterSegment:
        """Build the :class:`DefaulterSegment` the CRO reads."""

        sym = normalize_symbol(symbol)
        key = normalize_company(company_name) if company_name else ""
        hits_current = _matches(key, self.current) or _matches(sym, self.current)
        hits_past = _matches(key, self.past) or _matches(sym, self.past)

        if not self.checked:
            return DefaulterSegment(
                symbol=sym,
                company_name=company_name,
                checked=False,
                is_defaulting=False,
                segment="unknown",
                evidence="PSX Defaulters portal could not be read; segment unverified.",
                source=self.source,
                as_of=self.as_of,
            )
        if hits_current:
            return DefaulterSegment(
                symbol=sym,
                company_name=company_name,
                checked=True,
                is_defaulting=True,
                segment="non_compliant",
                evidence=f"Listed in the Current Non-Compliant Segment on the PSX portal ({hits_current}).",
                source=self.source,
                as_of=self.as_of,
            )
        if hits_past:
            return DefaulterSegment(
                symbol=sym,
                company_name=company_name,
                checked=True,
                is_defaulting=True,
                segment="defaulters",
                evidence=f"Appears in the PSX 'Previously Quoted in Defaulters' Segment' list ({hits_past}).",
                source=self.source,
                as_of=self.as_of,
            )
        return DefaulterSegment(
            symbol=sym,
            company_name=company_name,
            checked=True,
            is_defaulting=False,
            segment="regular",
            evidence="Not named on the PSX Defaulters / Non-Compliant portal.",
            source=self.source,
            as_of=self.as_of,
        )


def _matches(needle: str, haystack: frozenset[str]) -> str | None:
    """Exact match first, then a whole-token containment match both ways."""

    if not needle:
        return None
    if needle in haystack:
        return needle
    for candidate in haystack:
        if len(needle) >= 4 and (needle in candidate or candidate in needle):
            return candidate
    return None


def parse_defaulter_page(html: str) -> tuple[set[str], set[str], str]:
    """Split the portal page into current non-compliant and past-defaulter names."""

    text = html_text(html)
    current: set[str] = set()
    past: set[str] = set()
    section: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _NOISE.match(line) or len(line) < 3:
            continue
        lowered = line.lower()
        if any(marker in lowered for marker in _CURRENT_MARKERS):
            section = "current"
            continue
        if any(marker in lowered for marker in _PAST_MARKERS):
            section = "past"
            continue
        if section is None:
            continue
        if len(line) > 160 or line.endswith(":"):
            continue
        # A company line is mostly letters.  Skip table headings and dates.
        letters = sum(1 for ch in line if ch.isalpha())
        if letters < max(5, len(line) * 0.5):
            continue
        target = current if section == "current" else past
        target.add(normalize_company(line))

    note = ""
    if not current and not past:
        note = "Portal page parsed but no company names were recovered."
    return current, past, note


def load_defaulter_index(
    client: PortalClient, *, as_of: date | None = None
) -> DefaulterIndex:
    """Fetch and parse the PSX Defaulting-Companies portal."""

    as_of = as_of or datetime.now(UTC).astimezone().date()
    try:
        payload = client.get(
            DEFAULTER_PORTAL_URL,
            kind=SourceKind.DEFAULTER_LIST,
            label="PSX Defaulting Companies / Non-Compliant portal",
            as_of=as_of,
            ttl_seconds=6 * 3600,
            allow_missing=True,
        )
    except PortalError as exc:
        logger.warning("defaulter portal unavailable: %s", exc)
        return DefaulterIndex(checked=False, as_of=as_of, note=str(exc))

    if payload is None:
        return DefaulterIndex(checked=False, as_of=as_of, note="portal returned 404")

    try:
        current, past, note = parse_defaulter_page(payload.text)
    except Exception as exc:  # a malformed page must not kill the run
        logger.warning("defaulter portal parse failed: %s", exc)
        return DefaulterIndex(
            checked=False, as_of=as_of, source=payload.source_ref(), note=f"parse failed: {exc}"
        )

    checked = bool(current or past)
    return DefaulterIndex(
        checked=checked,
        current=frozenset(current),
        past=frozenset(past),
        source=payload.source_ref(),
        as_of=as_of,
        note=note if not checked else f"{len(current)} current, {len(past)} past names",
    )


# --------------------------------------------------------------------------- #
# PUCARS / company announcements
# --------------------------------------------------------------------------- #


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._text).split())
            if text:
                self.anchors.append((self._href, text))
            self._href = None
            self._text = []


def _categorise(title: str) -> str:
    lowered = title.lower()
    if any(word in lowered for word in ("financial result", "financial statement", "accounts", "eps", "profit")):
        return "financial_results"
    if "board meeting" in lowered or "board of directors" in lowered:
        return "board_meeting"
    if any(word in lowered for word in ("dividend", "bonus", "right issue", "entitlement")):
        return "dividend"
    if any(word in lowered for word in ("default", "non-compliance", "penalty", "suspension")):
        return "compliance"
    if any(word in lowered for word in ("material information", "acquisition", "merger", "disinvestment")):
        return "material_information"
    return "general"


def _hint(category: str) -> str:
    return {
        "financial_results": "neutral",
        "dividend": "positive",
        "compliance": "negative",
        "material_information": "unclear",
    }.get(category, "neutral")


def _parse_date(text: str) -> date | None:
    match = _DATE_RE.search(text)
    if not match:
        return None
    day, month, year = match.groups()
    months = {
        m: i
        for i, m in enumerate(
            ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
        )
    }
    month_num = int(month) if month.isdigit() else months.get(month[:3].lower())
    if not month_num or not 1 <= month_num <= 12:
        return None
    year_num = int(year)
    if year_num < 100:
        year_num += 2000
    try:
        return date(year_num, month_num, int(day))
    except ValueError:
        return None


def fetch_disclosures(
    symbol: str,
    client: PortalClient,
    *,
    company_name: str | None = None,
    limit: int = 12,
    as_of: date | None = None,
) -> list[DisclosureItem]:
    """Announcements for one scrip from the PSX company-announcement page."""

    sym = normalize_symbol(symbol)
    as_of = as_of or datetime.now(UTC).astimezone().date()
    try:
        payload = client.get(
            PUCARS_URL,
            kind=SourceKind.DISCLOSURE,
            label="PSX company announcements (PUCARS)",
            as_of=as_of,
            ttl_seconds=1800,
            allow_missing=True,
        )
    except PortalError as exc:
        logger.info("announcements unavailable: %s", exc)
        return []
    if payload is None:
        return []

    parser = _AnchorParser()
    try:
        parser.feed(payload.text)
    except Exception:  # pragma: no cover - malformed markup only
        return []

    needles = {sym}
    if company_name:
        needles.add(company_name.upper())
        needles.add(normalize_company(company_name).upper())

    items: list[DisclosureItem] = []
    seen: set[str] = set()
    for href, text in parser.anchors:
        haystack = text.upper()
        if not any(needle and needle in haystack for needle in needles):
            continue
        if text in seen or len(text) < 6:
            continue
        seen.add(text)
        category = _categorise(text)
        try:
            url = urljoin(PSX_WEB + "/", href)
        except ValueError:
            continue
        items.append(
            DisclosureItem(
                title=text[:400],
                published=_parse_date(text),
                category=category,
                url=url,
                summary="",
                sentiment_hint=_hint(category),
            )
        )
        if len(items) >= limit:
            break
    return items


__all__ = [
    "DefaulterIndex",
    "fetch_disclosures",
    "load_defaulter_index",
    "normalize_company",
    "parse_defaulter_page",
]
