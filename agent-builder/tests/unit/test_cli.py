"""The agent-builder CLI."""

import functools
import json
import zipfile

import pytest

from app.builder import cli
from app.builder.generator import build_project
from tests.conftest import MINIMAL_BLUEPRINT, FakeAgentsCli


@pytest.fixture
def blueprint_file(tmp_path):
    path = tmp_path / "blueprint.yml"
    path.write_text(MINIMAL_BLUEPRINT)
    return path


@pytest.fixture
def fake_build(monkeypatch):
    monkeypatch.setenv("AGENTS_CLI", "agents-cli")
    monkeypatch.setattr(cli, "build_project", functools.partial(build_project, runner=FakeAgentsCli()))


def test_validate_ok(blueprint_file, capsys):
    assert cli.main(["validate", str(blueprint_file)]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("ok: tiny-agent (agent_runtime) - agents: tiny_agent; 0 eval case(s)")
    assert "warning: no evals defined" in err


def test_validate_invalid(tmp_path, capsys):
    path = tmp_path / "bad.yml"
    path.write_text(MINIMAL_BLUEPRINT.replace("Be brief.", "x\n  tool: []"))
    assert cli.main(["validate", str(path)]) == 1
    assert "agent.tool: Extra inputs are not permitted" in capsys.readouterr().err


def test_validate_missing_file(capsys):
    assert cli.main(["validate", "/nope.yml"]) == 1
    assert "not found" in capsys.readouterr().err


def test_build(blueprint_file, tmp_path, fake_build, capsys):
    zip_path = tmp_path / "out.zip"
    out_dir = tmp_path / "out"
    assert cli.main(["build", str(blueprint_file), "-o", str(out_dir), "--zip", str(zip_path)]) == 0
    out = capsys.readouterr().out
    assert f"Built {out_dir / 'tiny-agent'}" in out
    assert "rendered app/agent.py" in out
    assert "agents-cli deploy" in out
    assert "tiny-agent/app/agent.py" in zipfile.ZipFile(zip_path).namelist()

    # a second build needs --force
    assert cli.main(["build", str(blueprint_file), "-o", str(out_dir)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["build", str(blueprint_file), "-o", str(out_dir), "--force"]) == 0


def test_schema(capsys):
    assert cli.main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert {"apiVersion", "name", "agent", "evals"} <= set(schema["properties"])
