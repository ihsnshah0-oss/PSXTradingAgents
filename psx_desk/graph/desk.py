"""The desk runner: one symbol in, one research note and one allocation out.

    desk = PSXTradingDesk()
    run  = desk.run("ENGRO")

``PSXTradingDesk`` wires the scrapers, the LLM backend and the LangGraph
pipeline together, then persists three artifacts per run:

    var/runs/<symbol>_<date>_<runid>/note.md            investment-committee note
    var/runs/<symbol>_<date>_<runid>/allocation.json    strict CRO allocation
    var/runs/<symbol>_<date>_<runid>/run.json           the full typed run record

The run record is the audit trail: it contains every PSX URL read, the SHA-256 of
each payload, what was missing, and the exact debate that produced the size.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from psx_desk.agents.llm import StructuredInvoker, build_invoker
from psx_desk.config import DeskConfig, load_config
from psx_desk.dataflows.http import PortalClient
from psx_desk.dataflows.ingest import DeskData, IngestionError, build_desk_data, today_pkt
from psx_desk.graph.builder import build_desk_graph
from psx_desk.report import render_decision_json, render_markdown
from psx_desk.schemas import DeskRun
from psx_desk.sources import normalize_symbol

logger = logging.getLogger(__name__)


class PSXTradingDesk:
    """A single-purpose PSX trading desk: five nodes, one exchange, one currency."""

    def __init__(
        self,
        config: DeskConfig | None = None,
        *,
        invoker: StructuredInvoker | None = None,
        data: DeskData | None = None,
        client: PortalClient | None = None,
    ) -> None:
        self.config = config or load_config()
        self._invoker = invoker
        self._data = data
        self._client = client
        self._graph = None

    # -- lazily built collaborators ---------------------------------------- #

    @property
    def client(self) -> PortalClient:
        if self._client is None:
            self._client = PortalClient(self.config)
        return self._client

    @property
    def data(self) -> DeskData:
        if self._data is None:
            self._data = build_desk_data(self.config, client=self.client)
        return self._data

    @property
    def invoker(self) -> StructuredInvoker:
        if self._invoker is None:
            self._invoker = build_invoker(self.config)
        return self._invoker

    @property
    def graph(self):
        if self._graph is None:
            self._graph = build_desk_graph(
                invoker=self.invoker, config=self.config, data=self.data
            )
        return self._graph

    # -- public API -------------------------------------------------------- #

    def run(
        self,
        symbol: str,
        trade_date: date | str | None = None,
        *,
        save: bool = True,
    ) -> DeskRun:
        """Run the full pipeline for ``symbol`` as of ``trade_date``."""

        sym = normalize_symbol(symbol)
        when = _coerce_date(trade_date)
        run_id = f"{when.isoformat()}-{uuid.uuid4().hex[:8]}"
        started = datetime.now(UTC)
        logger.info("desk run %s: %s as of %s", run_id, sym, when)

        # Five fixed nodes plus two per debate round, with headroom.
        recursion_limit = 10 + 2 * self.config.max_debate_rounds
        state = self.graph.invoke(
            {"symbol": sym, "trade_date": when, "notes": []},
            config={"recursion_limit": recursion_limit},
        )

        missing = [
            key
            for key in ("packet", "technical", "fundamental", "debate", "allocation")
            if key not in state
        ]
        if missing:
            raise IngestionError(
                f"{sym}: the graph finished without {', '.join(missing)}; "
                "the run is not usable and no artifact was written."
            )

        run = DeskRun(
            symbol=sym,
            trade_date=when,
            started_at=started,
            finished_at=datetime.now(UTC),
            packet=state["packet"],
            technical=state["technical"],
            fundamental=state["fundamental"],
            debate=state["debate"],
            allocation=state["allocation"],
            run_id=run_id,
            notes=list(dict.fromkeys(state.get("notes") or [])),
        )
        if save:
            self.save(run)
        return run

    def allocate(self, symbol: str, trade_date: date | str | None = None) -> str:
        """Return only the strict allocation JSON (for an order-management hook)."""

        return render_decision_json(self.run(symbol, trade_date, save=False))

    def save(self, run: DeskRun) -> Path:
        """Persist note.md, allocation.json and run.json for one run."""

        folder = self.config.runs_dir / f"{run.symbol}_{run.trade_date.isoformat()}_{run.run_id[-8:]}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "note.md").write_text(render_markdown(run), "utf-8")
        (folder / "allocation.json").write_text(render_decision_json(run), "utf-8")
        (folder / "run.json").write_text(run.to_json(), "utf-8")
        logger.info("artifacts written to %s", folder)
        return folder


def _coerce_date(value: date | str | None) -> date:
    if value is None:
        return today_pkt()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"trade_date must be YYYY-MM-DD, got {value!r}") from exc


__all__ = ["PSXTradingDesk"]
