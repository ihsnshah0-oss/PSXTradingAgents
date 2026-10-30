"""The five desk agents plus their prompt and LLM plumbing."""

from psx_desk.agents.fundamental import make_fundamental_analyst
from psx_desk.agents.llm import StructuredInvoker, build_invoker
from psx_desk.agents.research import (
    build_transcript,
    make_bear_researcher,
    make_bull_researcher,
)
from psx_desk.agents.risk import make_cro
from psx_desk.agents.technical import make_technical_analyst

__all__ = [
    "StructuredInvoker",
    "build_invoker",
    "build_transcript",
    "make_bear_researcher",
    "make_bull_researcher",
    "make_cro",
    "make_fundamental_analyst",
    "make_technical_analyst",
]
