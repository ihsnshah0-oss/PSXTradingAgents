"""The CLI surface: schemas, pipeline description and argument handling."""

from __future__ import annotations

import json

import pytest

from psx_desk.cli import main
from psx_desk.graph.builder import node_sequence


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "psx_desk" in capsys.readouterr().out


def test_describe_prints_the_pipeline(capsys):
    assert main(["describe"]) == 0
    out = capsys.readouterr().out
    assert "CRO Gatekeeper" in out
    assert "PSX Data Ingestion" in out


def test_schema_command_emits_valid_json_schema(capsys):
    assert main(["schema", "allocation"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["properties"]["decision"]["$ref"].endswith("Decision")
    assert schema["additionalProperties"] is False


def test_unknown_command_is_an_error():
    with pytest.raises(SystemExit):
        main(["frobnicate"])


def test_node_sequence_matches_configured_rounds():
    from psx_desk.config import load_config

    config = load_config(max_debate_rounds=3)
    assert node_sequence(config).count("Bear Researcher") == 3
