"""Rendering: the generated code must import cleanly and match the blueprint."""

import json
import types as pytypes

import pytest
import yaml
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.google_search_tool import GoogleSearchTool
from google.adk.tools.mcp_tool import McpToolset

from app.builder import render
from app.builder.blueprint import load_blueprint, parse_blueprint
from tests.conftest import EXAMPLE_BLUEPRINTS, MINIMAL_BLUEPRINT


def load_agent_module(bp) -> pytypes.ModuleType:
    """Executes the rendered app/agent.py as a throwaway module."""
    module = pytypes.ModuleType(f"generated_{bp.root_agent_name}")
    exec(compile(render.render_agent(bp), f"<{bp.name}/app/agent.py>", "exec"), module.__dict__)
    return module


def blueprint(**agent) -> str:
    data = yaml.safe_load(MINIMAL_BLUEPRINT)
    data["agent"].update(agent)
    return yaml.safe_dump(data)


def walk(agent):
    yield agent
    for sub in agent.sub_agents:
        yield from walk(sub)


@pytest.mark.parametrize("path", EXAMPLE_BLUEPRINTS, ids=lambda p: p.name)
def test_rendered_agent_matches_blueprint(path):
    bp = load_blueprint(path)
    module = load_agent_module(bp)

    assert module.app.root_agent is module.root_agent
    assert module.app.name == "app"
    rendered = {a.name: a for a in walk(module.root_agent)}
    assert set(rendered) == {a.name or bp.root_agent_name for a in bp.agents()}

    for spec in bp.agents():
        agent = rendered[spec.name or bp.root_agent_name]
        assert agent.instruction == spec.instruction.strip()
        assert agent.model.model == (spec.model or bp.model)
        assert [s.name for s in agent.sub_agents] == [s.name for s in spec.sub_agents]
        assert len(agent.tools) == len(spec.tools)
        for tool_spec, tool in zip(spec.tools, agent.tools, strict=True):
            if tool_spec.type == "function":
                declaration = FunctionTool(tool)._get_declaration()
                assert declaration.name == tool_spec.name
                assert declaration.description.startswith(tool_spec.description)
            elif tool_spec.type == "mcp":
                assert isinstance(tool, McpToolset)


def test_implementations_run():
    support = load_agent_module(load_blueprint("blueprints/support_team.yml"))
    assert support.lookup_order("a1001") == {
        "status": "found", "order_id": "A1001", "order_status": "shipped", "eta": "2 days"
    }
    assert support.lookup_order("zzz")["status"] == "not_found"

    ref = load_agent_module(load_blueprint("app/builder/reference_blueprint.yml"))
    assert ref.convert_currency(100, "USD", "EUR") == {"status": "success", "amount": 92.0, "currency": "EUR"}


def test_stub_for_missing_implementation():
    bp = parse_blueprint(blueprint(tools=[{
        "type": "function", "name": "do_it", "description": "d",
        "parameters": [{"name": "x", "type": "integer"}],
    }]))
    result = load_agent_module(bp).do_it(x=3)
    assert result == {"status": "not_implemented", "tool": "do_it", "arguments": {"x": 3}}


def test_parameters_become_a_typed_signature():
    bp = parse_blueprint(blueprint(tools=[{
        "type": "function", "name": "search", "description": "Search things.",
        "parameters": [
            {"name": "limit", "type": "integer", "default": 5, "description": "Max results."},
            {"name": "query", "description": "What to find."},
            {"name": "tags", "type": "array", "items": "string", "required": False},
            {"name": "exact", "type": "boolean"},
        ],
        "implementation": "return {'query': query, 'limit': limit, 'tags': tags, 'exact': exact}",
    }]))
    module = load_agent_module(bp)
    # Required parameters are moved ahead of defaulted ones.
    assert list(module.search.__annotations__) == ["query", "exact", "limit", "tags"]
    assert module.search("q", True) == {"query": "q", "limit": 5, "tags": None, "exact": True}

    declaration = FunctionTool(module.search)._get_declaration()
    schema = declaration.parameters_json_schema
    assert set(schema["required"]) == {"query", "exact"}
    assert schema["properties"]["limit"] == {"default": 5, "title": "Limit", "type": "integer"}
    assert {"items": {"type": "string"}, "type": "array"} in schema["properties"]["tags"]["anyOf"]
    assert "Max results." in declaration.description


def test_awkward_strings_survive():
    text = 'Say "hi" \\ use """triple""" quotes\n  keep indentation\nünïcödé ✓'
    bp = parse_blueprint(blueprint(
        instruction=text,
        description='A "quoted" \\ description',
        tools=[{"type": "function", "name": "f", "description": 'Uses """ and \\n'}],
    ))
    module = load_agent_module(bp)
    assert module.root_agent.instruction == text.strip()
    assert module.root_agent.description == 'A "quoted" \\ description'
    assert module.f.__doc__.startswith('Uses """ and \\n')


def test_google_search_bypass_only_when_mixed():
    alone = load_agent_module(parse_blueprint(blueprint(tools=[{"type": "builtin", "name": "google_search"}])))
    assert isinstance(alone.root_agent.tools[0], GoogleSearchTool)
    assert alone.root_agent.tools[0].bypass_multi_tools_limit is False

    ref = load_agent_module(load_blueprint("app/builder/reference_blueprint.yml"))
    researcher = ref.root_agent.sub_agents[0]
    assert researcher.tools[0].bypass_multi_tools_limit is True


def test_generation_config():
    bp = parse_blueprint(blueprint(generation={"temperature": 0.3, "max_output_tokens": 256}))
    config = load_agent_module(bp).root_agent.generate_content_config
    assert config.temperature == 0.3
    assert config.max_output_tokens == 256


class TestMcp:
    def bp(self, url="http://127.0.0.1:8080/mcp", auth="auto"):
        return parse_blueprint(blueprint(tools=[{
            "type": "mcp", "name": "srv", "url": url, "url_env": "SRV_MCP_URL",
            "auth": auth, "tool_filter": ["roll_dice"],
        }]))

    def test_env_var_overrides_url(self, monkeypatch):
        monkeypatch.setenv("SRV_MCP_URL", "https://override.example/mcp")
        module = load_agent_module(self.bp())
        assert module.srv._connection_params.url == "https://override.example/mcp"
        assert module.srv.tool_filter == ["roll_dice"]

    def test_no_token_for_local_http(self, monkeypatch):
        monkeypatch.delenv("SRV_MCP_URL", raising=False)
        assert load_agent_module(self.bp())._mcp_auth("http://localhost/mcp", "auto") is None

    def test_id_token_for_https(self, monkeypatch):
        import google.oauth2.id_token

        audiences = []

        def fake_fetch(request, audience):
            audiences.append(audience)
            return "tok"

        monkeypatch.setattr(google.oauth2.id_token, "fetch_id_token", fake_fetch)
        module = load_agent_module(self.bp())
        provider = module._mcp_auth("https://svc-abc.run.app/mcp", "auto")
        assert provider(None) == {"Authorization": "Bearer tok"}
        assert provider(None) == {"Authorization": "Bearer tok"}
        assert audiences == ["https://svc-abc.run.app"], "token should be cached"

    def test_auth_failure_modes(self, monkeypatch):
        import google.oauth2.id_token

        def boom(request, audience):
            raise RuntimeError("no credentials")

        monkeypatch.setattr(google.oauth2.id_token, "fetch_id_token", boom)
        module = load_agent_module(self.bp())
        assert module._mcp_auth("https://svc.run.app/mcp", "auto")(None) == {}
        with pytest.raises(RuntimeError):
            module._mcp_auth("https://svc.run.app/mcp", "on")(None)
        assert module._mcp_auth("https://svc.run.app/mcp", "off") is None


def test_eval_dataset():
    bp = load_blueprint("blueprints/dice_mcp_agent.yml")
    cases = json.loads(render.render_eval_dataset(bp))["eval_cases"]
    assert [c["eval_case_id"] for c in cases] == ["roll_d20", "count_words", "no_tool_needed"]
    assert cases[0]["expected_tool_calls"] == ["roll_dice"]
    assert "reference" not in cases[0]
    assert cases[1]["reference"]["response"]["parts"][0]["text"] == "There are 5 words."
    assert "expected_tool_calls" not in cases[2]


def test_eval_dataset_without_evals_has_smoke_case():
    cases = json.loads(render.render_eval_dataset(parse_blueprint(MINIMAL_BLUEPRINT)))["eval_cases"]
    assert [c["eval_case_id"] for c in cases] == ["smoke"]


def test_eval_config_only_uses_tool_metric_when_needed():
    with_tools = yaml.safe_load(render.render_eval_config(load_blueprint("blueprints/dice_mcp_agent.yml")))
    assert with_tools["metrics_to_run"] == ["tool_call_match", "custom_response_quality"]
    without = yaml.safe_load(render.render_eval_config(parse_blueprint(MINIMAL_BLUEPRINT)))
    assert without["metrics_to_run"] == ["custom_response_quality"]


def test_env_entries():
    bp = load_blueprint("app/builder/reference_blueprint.yml")
    assert render.env_entries(bp) == {"WEATHER_MCP_URL": "http://127.0.0.1:8080/mcp"}
    bp = load_blueprint("blueprints/dice_mcp_agent.yml")
    assert render.env_entries(bp) == {"MCP_SERVER_URL": "http://127.0.0.1:8080/mcp"}


@pytest.mark.parametrize("path", EXAMPLE_BLUEPRINTS, ids=lambda p: p.name)
def test_every_rendered_python_file_compiles(path):
    for rel, content in render.render_all(load_blueprint(path)).items():
        if rel.endswith(".py"):
            compile(content, rel, "exec")
