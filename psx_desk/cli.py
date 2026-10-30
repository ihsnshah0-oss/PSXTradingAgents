"""Command line for the PSX desk.

    psx-desk run ENGRO                 # full five-node run, writes artifacts
    psx-desk run SYS --date 2026-10-29 --json
    psx-desk scrape LUCK               # node 1 only: show the raw PSX packet
    psx-desk schema allocation         # the strict JSON schema the CRO must satisfy
    psx-desk describe                  # the LangGraph pipeline
    psx-desk doctor                    # dependency / key / PSX reachability checks
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date

from psx_desk import __version__
from psx_desk.agents.llm import StructuredOutputError
from psx_desk.config import load_config
from psx_desk.dataflows.http import PortalError
from psx_desk.dataflows.ingest import (
    IngestionError,
    build_desk_data,
    build_market_packet,
    today_pkt,
)
from psx_desk.graph.builder import graph_mermaid, node_sequence
from psx_desk.graph.desk import PSXTradingDesk
from psx_desk.report import render_markdown
from psx_desk.schemas import AllocationStatement, DeskRun
from psx_desk.sources import normalize_symbol

SCHEMAS = {
    "allocation": AllocationStatement,
    "run": DeskRun,
}


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config()
    desk = PSXTradingDesk(config)
    run = desk.run(args.symbol, args.date, save=not args.no_save)
    if args.json:
        print(run.to_json())
    else:
        print(render_markdown(run))
    return 0


def cmd_scrape(args: argparse.Namespace) -> int:
    config = load_config()
    data = build_desk_data(config)
    packet = build_market_packet(
        normalize_symbol(args.symbol),
        _date(args.date) or today_pkt(),
        data=data,
    )
    print(packet.model_dump_json(indent=2) if args.json else _packet_summary(packet))
    return 0


def _packet_summary(packet) -> str:
    quote = packet.quote
    lines = [
        f"{packet.symbol} @ {packet.trade_date}  ({packet.quote.company_name or 'unnamed'})",
        f"  close      PKR {quote.close_pkr:,.2f}  ({quote.change_pct:+.2f}%)" if quote.change_pct is not None else f"  close      PKR {quote.close_pkr:,.2f}",
        f"  volume     {quote.volume_shares or 0:,.0f} shares   turnover PKR {quote.value_pkr or 0:,.0f}",
        f"  circuit    {packet.circuit.locked.value}  caps PKR {packet.circuit.lower_cap_pkr:,.2f}"
        f" - {packet.circuit.upper_cap_pkr:,.2f}  ({packet.circuit.cap_source})",
        f"  segment    {packet.defaulter.segment} (checked={packet.defaulter.checked})",
        f"  ADV(20d)   {packet.adv_shares_20d or 0:,.0f} shares / PKR {packet.adv_value_pkr_20d or 0:,.0f}",
        f"  quality    {packet.data_quality.value}   bars={len(packet.history)}",
        f"  missing    {', '.join(packet.missing_fields) or 'none'}",
    ]
    if packet.macro:
        lines.append(
            f"  macro      SBP {packet.macro.sbp_policy_rate_pct:.2f}%  CPI "
            f"{packet.macro.cpi_yoy_pct}%  ({packet.macro.basis})"
        )
    for note in packet.notes:
        lines.append(f"  note       {note}")
    return "\n".join(lines)


def cmd_schema(args: argparse.Namespace) -> int:
    model = SCHEMAS[args.model]
    print(json.dumps(model.model_json_schema(), indent=2))
    return 0


def cmd_describe(_args: argparse.Namespace) -> int:
    config = load_config()
    print(graph_mermaid())
    print("Node order for this configuration (max_debate_rounds="
          f"{config.max_debate_rounds}):")
    for index, node in enumerate(node_sequence(config), start=1):
        print(f"  {index}. {node}")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    config = load_config()
    ok = True
    print(f"psx_desk {__version__}")
    for module, hint in (
        ("pydantic", "pip install pydantic"),
        ("pandas", "pip install pandas"),
        ("requests", "pip install requests"),
        ("langgraph", "pip install langgraph"),
    ):
        try:
            __import__(module)
            print(f"  [ok]   {module}")
        except ImportError:
            ok = False
            print(f"  [miss] {module}  -> {hint}")

    if config.api_key:
        print("  [ok]   LLM API key present")
    else:
        ok = False
        print("  [miss] GOOGLE_API_KEY (required to run the agents)")

    if config.offline:
        print("  [info] offline mode: only cached PSX payloads will be used")
    else:
        try:
            from psx_desk.dataflows.http import PortalClient
            from psx_desk.sources import MARKET_SUMMARY_URL

            client = PortalClient(config)
            payload = client.get(MARKET_SUMMARY_URL, label="reachability probe", allow_missing=True)
            if payload is None:
                ok = False
                print("  [miss] psx.com.pk did not answer the market-summary probe")
            else:
                print(f"  [ok]   psx.com.pk reachable ({len(payload.content):,} bytes)")
        except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
            ok = False
            print(f"  [miss] psx.com.pk unreachable: {exc}")
    print("\nstatus:", "ready" if ok else "not ready")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="psx-desk",
        description="Autonomous PSX-only multi-agent trading desk (Karachi).",
    )
    parser.add_argument("--version", action="version", version=f"psx_desk {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the five-node desk for one PSX scrip")
    run.add_argument("symbol")
    run.add_argument("--date", default=None, help="session date, YYYY-MM-DD (default: today PKT)")
    run.add_argument("--json", action="store_true", help="print the full run record")
    run.add_argument("--no-save", action="store_true", help="do not write artifacts to disk")
    run.set_defaults(func=cmd_run)

    scrape = sub.add_parser("scrape", help="run node 1 only and show the PSX packet")
    scrape.add_argument("symbol")
    scrape.add_argument("--date", default=None)
    scrape.add_argument("--json", action="store_true")
    scrape.set_defaults(func=cmd_scrape)

    schema = sub.add_parser("schema", help="print a Pydantic JSON schema")
    schema.add_argument("model", nargs="?", default="allocation", choices=sorted(SCHEMAS))
    schema.set_defaults(func=cmd_schema)

    describe = sub.add_parser("describe", help="show the LangGraph pipeline")
    describe.set_defaults(func=cmd_describe)

    doctor = sub.add_parser("doctor", help="check dependencies, key and PSX reachability")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return int(args.func(args))
    except (IngestionError, PortalError, StructuredOutputError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
