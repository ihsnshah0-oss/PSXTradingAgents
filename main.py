"""Run the PSX desk for one scrip.

    python main.py                 # ENGRO, today's Karachi session
    python main.py SYS 2026-10-29  # explicit symbol and date

The full pipeline writes var/runs/<symbol>_<date>_<id>/{note.md,allocation.json,run.json}.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

from psx_desk.config import load_config
from psx_desk.graph.desk import PSXTradingDesk
from psx_desk.report import render_markdown


def main(argv: list[str]) -> int:
    load_dotenv()
    symbol = argv[1] if len(argv) > 1 else "ENGRO"
    trade_date = argv[2] if len(argv) > 2 else None

    desk = PSXTradingDesk(load_config())
    run = desk.run(symbol, trade_date)
    print(render_markdown(run))
    print("\nallocation JSON:\n" + run.allocation.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
