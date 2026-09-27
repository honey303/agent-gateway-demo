"""Blueprint parsing and validation."""

import pytest
import yaml

from app.builder.blueprint import (
    DEFAULT_MODEL,
    BlueprintError,
    dump_blueprint,
    load_blueprint,
    parse_blueprint,
    warnings_for,
)
from tests.conftest import EXAMPLE_BLUEPRINTS, MINIMAL_BLUEPRINT


def _with(**changes) -> str:
    """MINIMAL_BLUEPRINT with top-level keys replaced."""
    data = yaml.safe_load(MINIMAL_BLUEPRINT)
    data.update(changes)
    return yaml.safe_dump(data)


def _errors(text: str) -> list[str]:
    with pytest.raises(BlueprintError) as exc:
        parse_blueprint(text)
    return exc.value.errors


@pytest.mark.parametrize("path", EXAMPLE_BLUEPRINTS, ids=lambda p: p.name)
def test_examples_are_valid(path):
    bp = load_blueprint(path)
    assert bp.agent.instruction
    assert bp.evals, "every example should ship eval cases"


def test_minimal_blueprint_defaults():
    bp = parse_blueprint(MINIMAL_BLUEPRINT)
    assert bp.root_agent_name == "tiny_agent"
    assert bp.model == DEFAULT_MODEL
    assert bp.deployment.target == "agent_runtime"
    assert bp.deployment.region == "us-central1"
    assert bp.agents() == [bp.agent]


def test_dump_round_trips():
    for path in EXAMPLE_BLUEPRINTS:
        bp = load_blueprint(path)
        assert parse_blueprint(dump_blueprint(bp)) == bp


def fn(name="do_it", **extra):
    return {"type": "function", "name": name, "description": "Does it.", **extra}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"name": "Bad_Name"}, "name 'Bad_Name' must be lowercase"),
        ({"name": "x" * 41}, "at most 40 characters"),
        ({"apiVersion": "agent-builder/v2"}, "apiVersion must be 'agent-builder/v1'"),
        ({"agent": {"instructions": "typo"}}, "Extra inputs are not permitted"),
        ({"agent": {"instruction": "x", "tools": [fn(), fn()]}}, "lists tool 'do_it' twice"),
        ({"agent": {"instruction": "x", "tools": [fn("os")]}}, "'os' is reserved"),
        ({"agent": {"instruction": "x", "tools": [fn("class")]}}, "valid Python identifier"),
        (
            {"agent": {"instruction": "x", "tools": [fn(parameters=[{"name": "order-id"}])]}},
            "parameter 'order-id' is not a valid identifier",
        ),
        (
            {"agent": {"instruction": "x", "tools": [fn(parameters=[{"name": "tool_context"}])]}},
            "'tool_context' is reserved by ADK",
        ),
        (
            {"agent": {"instruction": "x", "tools": [fn(implementation="return (")]}},
            "implementation is not valid Python",
        ),
        (
            {"agent": {"instruction": "x", "sub_agents": [{"instruction": "y"}]}},
            "every sub-agent needs a name",
        ),
        (
            {
                "agent": {
                    "instruction": "x",
                    "tools": [fn("helper_agent")],
                    "sub_agents": [{"name": "helper", "instruction": "y"}],
                }
            },
            "already used",
        ),
        (
            {"agent": {"instruction": "x", "tools": [{"type": "mcp", "name": "srv", "url": "ftp://x"}]}},
            "url must be http(s)://",
        ),
        (
            {"agent": {"instruction": "x", "tools": [{"type": "mcp", "name": "srv", "url": "http://x", "url_env": "lower"}]}},
            "UPPER_SNAKE_CASE",
        ),
        (
            {"agent": {"instruction": "x", "tools": [{"type": "builtin", "name": "calculator"}]}},
            "google_search",
        ),
        ({"env": {"lower": "x"}}, "env key 'lower' must be UPPER_SNAKE_CASE"),
        ({"dependencies": ["not a requirement!!"]}, "not a valid PEP 508 requirement"),
        (
            {"evals": [{"id": "a", "prompt": "p"}, {"id": "a", "prompt": "q"}]},
            "eval case id 'a' is used twice",
        ),
        (
            {"evals": [{"id": "a", "prompt": "p", "expected_tool_calls": ["nope"]}]},
            "expects tool 'nope', which no agent has",
        ),
    ],
)
def test_invalid_blueprints(changes, expected):
    errors = _errors(_with(**changes))
    assert any(expected in e for e in errors), errors


def test_all_semantic_errors_reported_together():
    errors = _errors(_with(name="BAD", env={"bad": "1"}))
    assert len(errors) == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [("key: [unclosed", "not valid YAML"), ("- a\n- b\n", "must be a YAML mapping")],
)
def test_unparseable(text, expected):
    assert expected in _errors(text)[0]


def test_transfer_to_agent_is_a_known_tool_with_sub_agents():
    bp = parse_blueprint(
        _with(
            agent={"instruction": "x", "sub_agents": [{"name": "helper", "instruction": "y"}]},
            evals=[{"id": "a", "prompt": "p", "expected_tool_calls": ["transfer_to_agent"]}],
        )
    )
    assert "transfer_to_agent" in bp.callable_tool_names()[0]


def test_mcp_without_filter_defers_tool_name_checks():
    bp = parse_blueprint(
        _with(
            agent={"instruction": "x", "tools": [{"type": "mcp", "name": "srv", "url": "http://h/mcp"}]},
            evals=[{"id": "a", "prompt": "p", "expected_tool_calls": ["anything"]}],
        )
    )
    assert bp.mcp_tools()[0].env_var == "SRV_URL"
    assert any("no tool_filter" in w for w in warnings_for(bp))


def test_warnings():
    bp = parse_blueprint(
        _with(agent={"instruction": "x", "tools": [fn(), {"type": "builtin", "name": "url_context"}]})
    )
    warnings = warnings_for(bp)
    assert any("'do_it' has no implementation" in w for w in warnings)
    assert any("url_context can't be combined" in w for w in warnings)
    assert any("no evals defined" in w for w in warnings)


def test_optional_parameters():
    bp = parse_blueprint(
        _with(
            agent={
                "instruction": "x",
                "tools": [
                    fn(parameters=[
                        {"name": "a"},
                        {"name": "b", "default": 3, "type": "integer"},
                        {"name": "c", "required": False},
                    ])
                ],
            }
        )
    )
    params = bp.agent.tools[0].parameters
    assert [p.optional for p in params] == [False, True, True]
