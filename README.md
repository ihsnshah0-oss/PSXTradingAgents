# PSX Trading Desk

An autonomous, single-purpose multi-agent equity research desk for the **Pakistan Stock Exchange**,
run out of a brokerage house on **Stock Exchange Road, Karachi**.

Five LangGraph nodes, one exchange, one currency, one rulebook:

```
START
  │
  ▼
① PSX Data Ingestion ──► ② PSX Technical Analyst ──► ③ PSX Fundamental Analyst
                                                              │
                                                              ▼
                                                        ④ Bull Researcher
                                                              │
                                                              ▼
                                                        Bear Researcher
                                                              │
                                          (rounds left?) ─────┴──► Bull Researcher
                                                              │
                                                              ▼
                                                        ⑤ CRO Gatekeeper ──► END
```

| Node | What it does | Output (Pydantic) |
| --- | --- | --- |
| ① **PSX Data Ingestion** | Scrapes the real close, volume, circuit caps, disclosures, defaulters list and SBP backdrop | `PSXMarketPacket` |
| ② **PSX Technical Analyst** | Momentum, price action and **daily circuit-lock surveillance** | `TechnicalReport` |
| ③ **PSX Fundamental Analyst** | Corporate quality scored against **SBP policy rate, KIBOR and CPI** | `FundamentalReport` |
| ④ **Bull vs Bear Researchers** | Adversarial debate over the same packet, N rounds | `DebateArgument` → `DebateTranscript` |
| ⑤ **CRO Gatekeeper** | Free-float liquidity, Defaulter segment, circuit, sizing → **strict JSON allocation** | `AllocationStatement` |

Every node returns a validated Pydantic model through `with_structured_output`. There is no
free-text hand-off anywhere in the pipeline: a node either returns its schema or the run fails.

---

## 1. What was removed

This repository used to be a US-centric framework. **The entire previous stack is gone** — Finnhub,
SEC EDGAR, AlphaVantage, YFinance, Yahoo, StockTwits, Reddit, FRED, Polymarket and the multi-provider
LLM zoo are all deleted, not disabled. `psx_desk/sources.py` is now the only place a URL is written
down, and `assert_source_allowed()` rejects any host that is not `psx.com.pk`, `dps.psx.com.pk` or
`sbp.org.pk` at call time — so a stray import cannot smuggle a foreign price into a report.

`tests/test_source_discipline.py` enforces this by AST-scanning the package for vendor imports and by
validating every URL literal against the allow-list.

---

## 2. Data sources (all real, all PSX/SBP)

| Purpose | Endpoint | Format |
| --- | --- | --- |
| Daily closing board | `https://dps.psx.com.pk/download/{dataset}/{YYYY-MM-DD}.{ext}` | CSV / ZIP / `.Z` / PDF |
| Exchange circuit caps | `/download/symbol_price/{date}.zip` | ZIP of CSV |
| Company names | `/download/symbol_name/{date}.zip` | ZIP of CSV |
| Daily quotations (whole market) | `/download/quote/{date}.pdf` | PDF |
| PUCARS announcements | `/download/announce/{date}.pdf` | PDF |
| Per-scrip daily OHLCV | `https://dps.psx.com.pk/timeseries/eod/{SYMBOL}` | JSON |
| Per-scrip intraday tape | `https://dps.psx.com.pk/timeseries/int/{SYMBOL}` | JSON |
| Live board | `https://dps.psx.com.pk/market-watch` | HTML table |
| Symbol directory | `https://dps.psx.com.pk/symbols` | JSON |
| Defaulters / Non-Compliant | `https://www.psx.com.pk/psx/resources-and-tools/Defaulting-Companies-Profile-Portal` | HTML |
| Circuit-breaker rules in force | `https://www.psx.com.pk/psx/resources-and-tools/circuit-breakers` | HTML |
| Company announcements | `https://www.psx.com.pk/psx/announcement/companies` | HTML |
| Macro anchor | `https://www.sbp.org.pk/` (policy rate, corridor, CPI, USD/PKR) | HTML tiles |

> **On `data_dump.csv`.** PSX does not publish one stable public `data_dump.csv`. Rather than
> pretend it exists, `data_dump_candidates()` lists every URL that might serve a whole-market dump —
> the canonical `/download/{dataset}/{date}.{ext}` files first, then legacy
> `/download/history/data_dump.csv` paths, then the live board — and
> `scrape_market_dump()` probes them in order, **accepting a payload only if it parses into a table
> with a symbol column and a price column**. A probe that returns HTML, a PDF where a CSV was
> expected, or a five-row sector summary is rejected with a recorded reason. Nothing is fabricated
> when a probe fails; the caller gets an explicit `MarketDumpUnavailable` listing every URL and why
> each was rejected.

---

## 3. The scraper

```python
from datetime import date
from psx_desk.dataflows.portal import scrape_market_dump

dump = scrape_market_dump(date(2026, 10, 29))          # whole board
dump = scrape_market_dump(date(2026, 10, 29), symbols=["SYS", "ENGRO"])

row = dump.get("SYS")
row.close, row.upper_cap, row.lower_cap, row.volume
dump.sources            # every payload read, with SHA-256 and retrieval time
dump.missing_fields     # what PSX did not publish — never estimated
dump.rejected           # "url :: reason" for every probe that failed

dump.to_frame()         # pandas, only if you ask for it
```

How it stays fast and honest:

* one `requests.Session` with a keep-alive pool, a shared polite interval between hits and bounded
  concurrency (`PSXDESK_THROTTLE_SECONDS`, `PSXDESK_MAX_WORKERS`);
* date-keyed files are immutable, so they are cached permanently; the live board has a short TTL;
* `symbols=` filters while parsing, so memory is O(wanted) instead of O(board);
* each archive member is streamed straight into the delimited reader; parsing stops at the first
  payload that yields a usable board;
* the exchange's own `symbol_price` caps and `symbol_name` names are left-joined onto the price
  board by symbol, and **exchange caps always beat a computed band**;
* a response is recorded with its SHA-256 so a report can be audited back to bytes.

`psx_desk/dataflows/archive.py` does the byte work without a browser stack: gzip/bzip2/ZIP, classic
Unix `.Z` (LZW, with the platform `uncompress` preferred and a portable decoder as fallback),
delimiter sniffing, column-name normalisation (`CURRENT`, `LTP`, `%CHANGE`, `UPPER CIRCUIT` …) and a
stdlib HTML table reader.

---

## 4. GitHub mirrors — the labelled fallback tier

Some machines cannot reach `dps.psx.com.pk` (corporate proxy, exchange portal outage, a rate-limited
link). For those days there is a second, **opt-in** data path: community repositories on GitHub that
publish PSX data. It is deliberately not a source of truth, and it is off by default.

| Preset | Repository | Carries | Verified schema |
| --- | --- | --- | --- |
| `psx-data` | `bigbajwatv-alt/psx-data` | whole-board snapshot (484 scrips), symbol directory + sectors, KSE indices | `daily_summary.json`, `latest_snapshot.json`, `symbols.json`, `indices_eod.json`, `sectors.json` |
| `psx-history` | `Muhammad-Wasif/PSX-Stock-Market-Dataset` | ~10 years of daily OHLCV for 105 KSE-100 names, name→symbol map, index constituents | `stocks/<Company>.csv`, `combined/PSX_KSE100_Full_Historical_Daily.csv`, `metadata/*` |

```bash
psx-desk mirror list                     # presets, refs, files, licence
psx-desk mirror fetch SYS                # one scrip off the snapshot mirror
psx-desk mirror history SYS --days 250   # daily OHLCV from the history dataset
psx-desk mirror fetch SYS --as-of 2026-10-29 --via tarball
```

```python
from datetime import date
from psx_desk.dataflows.github_mirror import fetch_mirror_history, scrape_psx_mirror

dump = scrape_psx_mirror("psx-data", as_of=date(2026, 10, 29))
dump.mirror, dump.mirror_as_of, dump.missing_fields   # provenance is part of the type
bars = fetch_mirror_history("SYS", mirror="psx-history", days=250)
```

### What the code guarantees

* **Off by default.** `PSXDESK_MIRROR_POLICY=off` means the mirror module is never imported or called;
  `psx-desk doctor` prints `GitHub mirror tier disabled (PSX/SBP only)`. The default allow-list in
  `psx_desk/sources.py` still contains only PSX and SBP hosts.
* **A separate allow-list.** `MIRROR_HOSTS` (`raw.githubusercontent.com`, `api.github.com`,
  `codeload.github.com`) is disjoint from `ALLOWED_HOSTS`, and only
  `psx_desk/dataflows/github_mirror.py` can opt into it (`assert_source_allowed(url, allow_mirror=True)`).
  `tests/test_source_discipline.py` AST-scans the package to prove no other module does, and that
  foreign vendors stay blocked *even with* the mirror opt-in.
* **Three access paths, in order.** `raw.githubusercontent.com` → `api.github.com` (base64 contents,
  honouring `PSXDESK_GITHUB_TOKEN` for the 60 req/h anonymous limit) → `codeload.github.com` tarball
  (one request for the whole repo, used for the 12 MB history dataset). Each fetch is cached and
  carries a SHA-256 like every other payload.
* **Newest snapshot wins.** Mirrors accumulate files; the scraper reads every board file the preset
  lists and keeps the most recently dated one, logging the ones it ignored.
* **Staleness is a hard gate.** `PSXDESK_MIRROR_MAX_STALENESS_DAYS` (default `5`) refuses a snapshot
  older than the session being priced, with a message naming the gap. This matters in practice: the
  `psx-data` snapshot files were dated 2026-05-22 and 2026-08-19 when this integration was written,
  so a naive fallback would have quietly priced October 2026 from an August print.
* **Never a price it cannot vouch for.** A mirror row is a real number from a real publish or the row
  is absent — no interpolation, no carry-forward. Exchange circuit caps, trades and free-float mcap are
  reported missing because no mirror publishes them.
* **A session close must be a session close.** With no board row, the close can only come from the last
  bar, so `PSXDESK_SESSION_CLOSE_MAX_LAG_DAYS` (default `5`) bounds how old that bar may be. When this
  integration was written, `psx-desk scrape SYS --date 2026-10-29` with the fallback enabled refused
  with *"the most recent bar is 2026-10-08 (21 days earlier) … Refusing to quote a stale close"* —
  which is the correct answer, and the one a silent carry-forward would have hidden.
* **Mirrors can never upgrade a packet.** Any mirror-sourced price or bar forces
  `data_quality="partial"`, adds `exchange_verified_prices` / `exchange_verified_history` to
  `missing_fields`, and writes a note naming the repository and its own snapshot date.

### Policy matrix

| `PSXDESK_MIRROR_POLICY` | Prices/bars | Reference fields (sector, name, index levels) | `data_quality` |
| --- | --- | --- | --- |
| `off` (default) | PSX only | PSX only | as PSX reports |
| `backfill` | **PSX only** | PSX, then mirror for gaps | unchanged (mirror never prices) |
| `fallback` | PSX, else mirror | PSX, then mirror | forced `partial` when the mirror was used |

```bash
# Karachi proxy that terminates TLS? point the desk at the corporate CA instead of disabling checks.
PSXDESK_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

PSXDESK_MIRROR_POLICY=fallback
PSXDESK_GITHUB_MIRROR=psx-data
PSXDESK_GITHUB_HISTORY_MIRROR=psx-history
# PSXDESK_GITHUB_TOKEN=ghp_...     # raises the API rate limit
# PSXDESK_GITHUB_VIA=tarball       # force one access path
```

> **Attribution.** Mirror data belongs to its maintainers (`bigbajwatv-alt/psx-data`,
> `Muhammad-Wasif/PSX-Stock-Market-Dataset`, MIT for the latter) and to the exchange it mirrors. Read
> the repository's licence before redistributing, and remember that PSX's own `dps.psx.com.pk` file
> is what a compliance review will ask to see.

---

## 5. Pakistani market structure, in code

### Circuit locks (`psx_desk/market_rules.py`)

A PSX scrip cannot move more than the band in force from its previous close — **"5% or Rs.1,
whichever is higher"** on this desk's rulebook, which PSX Regulation 19.3 may widen (historically to
7.5%). The calculated price is rounded to the tick.

* `circuit_band_pct` is **configuration, never a hard-coded constant** (`PSXDESK_CIRCUIT_BAND_PCT`);
* when the session's `symbol_price` file exists, the **Exchange's own caps win** (`cap_source="exchange_file"`);
* lock detection tolerates half a tick, so a print *at* the cap is never reported as "0.004% away";
* `consecutive_locked_sessions` tracks a streak, because repeated lower locks are exactly when PSX
  tightens scrutiny and withdraws short-sale exemptions;
* a locked scrip still gets a thesis, but the CRO cannot claim a fill — it must add a pre-open
  condition instead.

### Defaulters / Non-Compliant segment (`psx_desk/dataflows/disclosures.py`)

The portal page is parsed into current non-compliant names and past defaulters. A match is an
**absolute veto** in the schema validator: the allocation is forced to `REJECT` / `NO_TRADE` with
zero size, whichever way the model voted. If the page cannot be read, the result is
`checked=False` → the CRO gate reports **UNKNOWN**, not PASS, and size is halved.

### SBP macro (`psx_desk/dataflows/macro.py`)

Policy rate, corridor, CPI, USD/PKR, KIBOR and T-bill cut-offs are scraped from sbp.org.pk. The
fundamental analyst must compare the **earnings yield (1/PE)** with the policy rate (equity risk
premium) and with CPI (real earnings yield) — the two tests that actually decide whether a Pakistani
equity deserves capital when a government-guaranteed deposit pays the policy rate. If SBP is
unreachable, the desk falls back to a **dated, labelled print** and marks the backdrop
`STALE` / `configured_fallback` instead of inventing a number.

---

## 6. Outputs

Each run writes three artifacts to `var/runs/<symbol>_<date>_<id>/`:

| File | Audience |
| --- | --- |
| `note.md` | Investment committee: session print, circuit, defaulters, every risk gate, both research notes, the debate, disclosures and a provenance table |
| `allocation.json` | Order pad: the strict `AllocationStatement` |
| `run.json` | Audit: the full typed run record — every PSX URL, payload hash, missing field and debate turn |

```json
{
  "action": "BUY",
  "allocation_pkr": 4999693.45,
  "approved_by": "Chief Risk Officer, PSX Equities",
  "as_of": "2026-10-29",
  "circuit": { "band_pct": 5.0, "cap_source": "exchange_file", "locked": "none",
               "lower_cap_pkr": 391.92, "upper_cap_pkr": 433.18, "tradable_at_close": true },
  "decision": "APPROVE",
  "defaulter": { "checked": true, "is_defaulting": false, "segment": "regular" },
  "entry_limit_pkr": 412.55,
  "gates": [ { "gate": "defaulter_segment", "status": "pass", "blocking": true },
             { "gate": "free_float_liquidity", "status": "pass", "blocking": true },
             { "gate": "data_integrity", "status": "fail", "blocking": false } ],
  "nav_pkr": 50000000.0,
  "reward_risk": 3.282,
  "risk_budget_pkr": 249045.45,
  "schema_version": "psx-alloc/1.0",
  "shares": 12119,
  "size_cap_reason": "single-name weight cap",
  "stop_pkr": 392.0,
  "symbol": "SYS",
  "target_pkr": 480.0,
  "weight_of_nav_pct": 9.9994
}
```

The statement is *repaired and re-validated*, not trusted: a REJECT cannot carry size, shares and
weight are recomputed from the allocation and entry limit, a long cannot sit below its own stop, and
the size is clamped to the desk's computed maximum before the JSON is emitted.

---

## 7. Quickstart

```bash
git clone <this repo> && cd PSXTradingAgents
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # then put your GOOGLE_API_KEY in it
```

```bash
psx-desk doctor                       # deps, key, psx.com.pk reachability
psx-desk describe                     # the LangGraph pipeline
psx-desk schema allocation            # the strict JSON schema the CRO must satisfy

psx-desk scrape ENGRO                 # node ① only: show the raw PSX packet
psx-desk run ENGRO                    # full five-node run + artifacts
psx-desk run SYS --date 2026-10-29 --json
```

Python API:

```python
from dotenv import load_dotenv
from psx_desk import PSXTradingDesk, load_config

load_dotenv()
desk = PSXTradingDesk(load_config())
run = desk.run("LUCK")                       # or desk.run("LUCK", "2026-10-29")

print(run.allocation.decision)               # APPROVE / APPROVE_REDUCED / REJECT
print(run.allocation.to_json())              # strict JSON, sorted keys
print(run.packet.circuit.locked)             # upper / lower / none
```

Docker:

```bash
docker compose run --rm desk run HUBC
```

### LLM backends

* **`google-genai`** (default) — the official Google Gen AI SDK, called with
  `response_mime_type="application/json"` plus the Pydantic JSON schema, then validated locally.
* **`langchain`** — `ChatGoogleGenerativeAI(...).with_structured_output(Schema)`; set
  `PSXDESK_LLM_BACKEND=langchain` and install the `[langchain]` extra.

Both implement the same `StructuredInvoker` protocol, so the graph is backend-agnostic and the tests
substitute a scripted invoker.

---

## 8. Configuration

Everything is a frozen dataclass in `psx_desk/config.py`, overridable via `PSXDESK_*` env vars —
see `.env.example` for the annotated list. The ones that matter most:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PSXDESK_CIRCUIT_BAND_PCT` | `5.0` | Scrip-level circuit band in force |
| `PSXDESK_CIRCUIT_FLOOR_PKR` | `1.0` | "or Rs.1, whichever is higher" |
| `PSXDESK_NAV_PKR` | `50,000,000` | Desk NAV the weights are measured against |
| `PSXDESK_MAX_NAME_WEIGHT_PCT` | `10` | Concentration cap |
| `PSXDESK_MAX_RISK_PER_TRADE_PCT` | `1` | 1R budget at the stop |
| `PSXDESK_MAX_ADV_PARTICIPATION_PCT` | `15` | Participation in 20-day ADV |
| `PSXDESK_MIN_ADV_VALUE_PKR` | `5,000,000` | Daily turnover floor (Rs 5m) |
| `PSXDESK_MIN_REWARD_RISK` | `1.5` | Below this, the idea is rejected |
| `PSXDESK_MAX_DEBATE_ROUNDS` | `2` | Bull/bear rounds |
| `PSXDESK_OFFLINE` | `0` | Replay cache only, never touch the network |
| `PSXDESK_MIRROR_POLICY` | `off` | `off` / `backfill` / `fallback` — the GitHub mirror tier |
| `PSXDESK_GITHUB_MIRROR` | unset | Snapshot preset (`psx-data`) or `owner/repo` |
| `PSXDESK_GITHUB_HISTORY_MIRROR` | unset | History preset (`psx-history`) or `owner/repo` |
| `PSXDESK_MIRROR_MAX_STALENESS_DAYS` | `5` | Refuse a mirror snapshot older than this |
| `PSXDESK_SESSION_CLOSE_MAX_LAG_DAYS` | `5` | A session close may not come from an older bar |
| `PSXDESK_CA_BUNDLE` | unset | Corporate TLS bundle for a proxy that intercepts TLS |

---

## 9. Tests

```bash
pytest -q          # 198 hermetic tests, no network, no API key
ruff check .
```

Every test runs against **recorded payloads** through `FixtureTransport`: a synthetic PSX board,
`symbol_price`/`symbol_name` ZIPs, DPS-style EOD JSON (including a scrip locked at its upper cap), a
Defaulters portal page, PUCARS notices and an SBP tile page. The scraper walks its real probe list,
the parsers do real work, and the five agents run against a `ScriptedInvoker` that answers with
Pydantic models — so the graph, the gates and the JSON contract are all exercised end to end.

Coverage highlights: circuit arithmetic (including PSX's own tick-rounding example), `.Z`/gzip/ZIP
decoding, table parsing, probe-failure reporting, point-in-time truncation, defaulter vetoes,
liquidity substitution rules, size clamping, and a source-discipline test that fails the build if a
foreign vendor ever reappears.

---

## 10. Known limits (stated, not hidden)

* Financial results are published by PSX as **PDFs**; the desk does not OCR them. It uses the
  disclosures list as evidence and leaves unreported metrics `null` with `data_quality="partial"`.
  Point a PDF extractor at `DisclosureItem.url` if you need the line items.
* Exchange holidays are not modelled: a date with no published file falls back to the live board and
  is flagged in `packet.notes`.
* The pure-Python `.Z` decoder is a fallback; the platform `uncompress` is preferred. The `.Z`
  `mkt_summary` dataset is optional — the primary board path is CSV/ZIP/JSON/HTML.
* `PSXDESK_OFFLINE=1` replays the cache, which is what a backtest or a plane seat needs.
* The GitHub mirrors are **community datasets**, not exchange files: the snapshot repo is refreshed
  irregularly (its files were dated 2026-05-22 and 2026-08-19 during this build), and the history
  dataset ends at its last rebuild. That is why the policy defaults to `off`, the staleness budget is
  enforced, and a mirror-sourced packet can never be graded better than `partial`.

---

## 11. Disclaimer

Research tooling, not investment advice. Market data belongs to the Pakistan Stock Exchange;
respect its terms of use, keep the throttle on, and do not redistribute the payloads. Nothing here
executes an order — the CRO emits a JSON proposal that a human still has to sign off.
