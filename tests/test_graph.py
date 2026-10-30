"""End-to-end: five nodes, typed state, and a CRO that cannot be talked out of a veto."""

from __future__ import annotations

import json

import pytest

from psx_desk.agents.research import build_transcript
from psx_desk.config import load_config
from psx_desk.dataflows.http import DiskCache, PortalClient
from psx_desk.dataflows.ingest import DeskData
from psx_desk.dataflows.portal import DumpStore
from psx_desk.graph.builder import build_desk_graph, graph_mermaid, node_sequence
from psx_desk.graph.desk import PSXTradingDesk
from psx_desk.schemas import Action, Decision, GateStatus
from tests.conftest import TRADE_DATE, ScriptedInvoker, World


def make_desk(config, invoker, world) -> PSXTradingDesk:
    client = PortalClient(
        config, transport=world.transport, cache=DiskCache(config.cache_dir), sleep=lambda _s: None
    )
    data = DeskData(client=client, config=config, dumps=DumpStore(client))
    return PSXTradingDesk(config, invoker=invoker, data=data)


def test_pipeline_runs_in_the_documented_order(config, invoker):
    graph = build_desk_graph(invoker=invoker, config=config, data=make_desk(config, invoker, World()).data)
    sequence = node_sequence(config)
    assert sequence == [
        "PSX Data Ingestion",
        "PSX Technical Analyst",
        "PSX Fundamental Analyst",
        "Bull Researcher",
        "Bear Researcher",
        "CRO Gatekeeper",
    ]
    assert graph is not None


def test_full_run_produces_typed_artifacts(config, invoker, tmp_path):
    desk = make_desk(config, invoker, World())
    run = desk.run("SYS", TRADE_DATE)

    # the LLM was asked for exactly the four structured schemas, in order
    assert invoker.calls == [
        "TechnicalReport",
        "FundamentalReport",
        "DebateArgument",
        "DebateArgument",
        "AllocationStatement",
    ]

    assert run.packet.symbol == "SYS"
    assert run.debate.rounds_completed == 1
    assert len(run.debate.bull) == 1 and len(run.debate.bear) == 1

    alloc = run.allocation
    assert alloc.decision in {Decision.APPROVE, Decision.APPROVE_REDUCED}
    assert alloc.action is Action.BUY
    assert alloc.allocation_pkr > 0
    assert alloc.shares > 0
    assert alloc.defaulter.symbol == "SYS" and not alloc.defaulter.is_defaulting
    assert alloc.circuit.close_pkr == pytest.approx(run.packet.quote.close_pkr)
    assert alloc.nav_pkr == config.risk.nav_pkr
    assert alloc.gates and all(gate.gate for gate in alloc.gates)

    folder = desk.save(run)
    note = (folder / "note.md").read_text()
    assert "PSX desk note" in note
    assert "Allocation statement" in note
    assert "Data provenance" in note
    assert "dps.psx.com.pk" in note

    allocation_json = json.loads((folder / "allocation.json").read_text())
    assert allocation_json["schema_version"] == "psx-alloc/1.0"
    assert json.loads((folder / "run.json").read_text())["symbol"] == "SYS"


def test_the_technical_numbers_are_the_desks_not_the_models(config, invoker):
    run = make_desk(config, invoker, World()).run("SYS", TRADE_DATE)
    # The scripted model claims momentum 62 and trend "uptrend" over a series
    # the desk computed itself; every measured field is overwritten.
    assert run.technical.rsi_14 is not None
    assert run.technical.circuit.close_pkr == pytest.approx(run.packet.quote.close_pkr)
    assert run.technical.circuit.band_pct == config.circuit_band_pct
    assert run.technical.tradability == "open"


def test_the_cro_cannot_size_above_the_desk_limit(config):
    greedy = ScriptedInvoker(allocation_pkr=999_999_999.0)
    run = make_desk(config, greedy, World()).run("SYS", TRADE_DATE)
    assert run.allocation.allocation_pkr <= config.risk.nav_pkr * 0.10 + 1
    assert run.allocation.weight_of_nav_pct <= config.risk.max_single_name_weight_pct + 0.01
    assert "clamp" in run.allocation.size_cap_reason.lower() or run.allocation.allocation_pkr <= 5_000_000


def test_a_defaulter_is_rejected_whatever_the_researchers_say(config, invoker):
    world = World(defaulters=("Systems Limited",))
    run = make_desk(config, invoker, world).run("SYS", TRADE_DATE)
    assert run.packet.defaulter.is_defaulting
    assert run.allocation.decision is Decision.REJECT
    assert run.allocation.action is Action.NO_TRADE
    assert run.allocation.allocation_pkr == 0
    assert run.allocation.shares == 0
    assert any("Veto active" in switch for switch in run.allocation.kill_switches)


def test_a_locked_scrip_keeps_a_thesis_but_gets_no_claim_of_a_fill(config, invoker):
    run = make_desk(config, invoker, World(lock="upper")).run("HUBC", TRADE_DATE)
    assert run.packet.circuit.locked.value == "upper"
    assert run.technical.tradability == "locked_upper"
    assert any("opening auction" in condition for condition in run.allocation.conditions)


def test_unverified_defaulters_halve_the_size(config, invoker):
    world = World()
    world.transport._map.pop(
        "https://www.psx.com.pk/psx/resources-and-tools/Defaulting-Companies-Profile-Portal"
    )
    run = make_desk(config, invoker, world).run("SYS", TRADE_DATE)
    assert run.packet.defaulter.checked is False
    gate = next(g for g in run.allocation.gates if g.gate == "defaulter_segment")
    assert gate.status is GateStatus.UNKNOWN
    assert any("Size halved" in condition for condition in run.allocation.conditions)


def test_a_reject_from_the_cro_is_honoured(config):
    cautious = ScriptedInvoker(decision=Decision.REJECT, action=Action.NO_TRADE, allocation_pkr=0.0)
    run = make_desk(config, cautious, World()).run("SYS", TRADE_DATE)
    assert run.allocation.decision is Decision.REJECT
    assert run.allocation.allocation_pkr == 0


def test_transcript_weights_evidence_not_rhetoric():
    from psx_desk.schemas import DebateArgument, EvidencePoint, Stance, Verification

    bull = [
        DebateArgument(
            stance=Stance.BULL, symbol="SYS", round=1, thesis="t", conviction=0.5,
            key_points=[
                EvidencePoint(claim="a", metric="adv", value="1", verification=Verification.HARD_DATA),
                EvidencePoint(claim="b", metric="pe", value="2", verification=Verification.HARD_DATA),
            ],
        )
    ]
    bear = [
        DebateArgument(
            stance=Stance.BEAR, symbol="SYS", round=1, thesis="t", conviction=0.9,
            key_points=[
                EvidencePoint(claim="c", metric="x", value="1", verification=Verification.INFERENCE),
                EvidencePoint(claim="d", metric="y", value="2", verification=Verification.INFERENCE),
            ],
        )
    ]
    transcript = build_transcript("SYS", bull, bear)
    assert transcript.stronger_case == "bull"
    assert "hard-data" in transcript.summary


def test_transcript_requires_both_sides():
    with pytest.raises(ValueError):
        build_transcript("SYS", [], [])


def test_mermaid_and_sequence_describe_the_same_pipeline(config):
    assert "CRO Gatekeeper" in graph_mermaid()
    assert "Bull Researcher" in graph_mermaid()
    assert node_sequence(config)[-1] == "CRO Gatekeeper"
    assert node_sequence(load_config(max_debate_rounds=2)).count("Bull Researcher") == 2
