"""The custom eval metrics, run on hand-built trace instances.

tests/integration/test_agents_cli_build.py runs the same files through
`agents-cli eval grade` itself.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(path: str):
    spec = importlib.util.spec_from_file_location(Path(path).stem, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool_calls = load("app/builder/templates/tool_calls.py")
tool_status = load("tests/eval/tool_status.py")


def trace(*parts):
    """An agents-cli trace instance whose events carry the given parts."""
    return {"agent_data": {"turns": [{"events": [
        {"author": "agent", "content": {"role": "model", "parts": [p]}} for p in parts
    ]}]}}


def call(name):
    return {"function_call": {"name": name, "args": {}}}


def response(name, **payload):
    return {"function_response": {"name": name, "response": payload}}


@pytest.mark.parametrize(
    ("expected", "forbidden", "called", "score"),
    [
        ([], [], [], 1.0),
        (["roll_dice"], [], ["roll_dice"], 1.0),
        (["roll_dice"], [], [], 0.0),
        (["a", "b"], [], ["b", "c"], 0.5),
        (["a"], ["build_agent"], ["a", "build_agent"], 0.0),
        ([], ["build_agent"], ["validate_blueprint"], 1.0),
    ],
)
def test_tool_call_match(expected, forbidden, called, score):
    instance = trace(*[call(n) for n in called], {"text": "done"})
    instance["expected_tool_calls"] = expected
    instance["forbidden_tool_calls"] = forbidden
    result = tool_calls.evaluate(instance)
    assert result["score"] == score
    assert result["explanation"]


def test_tool_call_match_accepts_camel_case_events():
    instance = trace({"functionCall": {"name": "x"}})
    instance["expected_tool_calls"] = ["x"]
    assert tool_calls.evaluate(instance)["score"] == 1.0


def test_tool_call_match_on_empty_trace():
    assert tool_calls.evaluate({"expected_tool_calls": ["x"]})["score"] == 0.0


@pytest.mark.parametrize(
    ("expected", "responses", "score"),
    [
        ({}, [], 1.0),
        ({"build_agent": "success"}, [response("build_agent", status="success")], 1.0),
        ({"build_agent": "success"}, [], 0.0),
        # the *last* response counts: a failed build that was retried and fixed passes
        (
            {"build_agent": "success"},
            [response("build_agent", status="invalid"), response("build_agent", status="success")],
            1.0,
        ),
        (
            {"validate_blueprint": "valid", "build_agent": "success"},
            [response("validate_blueprint", status="valid"), response("build_agent", status="error")],
            0.5,
        ),
    ],
)
def test_tool_status_match(expected, responses, score):
    instance = trace(*responses)
    instance["expected_tool_status"] = expected
    assert tool_status.evaluate(instance)["score"] == score
