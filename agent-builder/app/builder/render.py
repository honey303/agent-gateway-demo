"""Renders a Blueprint into the files the builder lays over an agents-cli scaffold.

Pure functions from a Blueprint to file contents - nothing here touches disk
or runs agents-cli, so every generated file can be unit-tested in isolation.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.builder.blueprint import (
    AgentSpec,
    Blueprint,
    BuiltinTool,
    FunctionTool,
    McpTool,
    Parameter,
    dump_blueprint,
)

TEMPLATE_DIR = Path(__file__).with_name("templates")

_PY_TYPES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "object": "dict",
}

_BUILTIN_IMPORTS = {
    "google_search": "from google.adk.tools.google_search_tool import GoogleSearchTool",
    "url_context": "from google.adk.tools import url_context",
    "load_web_page": "from google.adk.tools.load_web_page import load_web_page",
}


def py_str(value: str, indent: int = 0) -> str:
    """A Python string literal for `value`; multi-line strings are split into
    implicitly concatenated lines so the generated code stays readable."""
    lines = value.splitlines(keepends=True)
    if len(lines) <= 1:
        return json.dumps(value, ensure_ascii=False)
    pad = " " * (indent + 4)
    body = "\n".join(pad + json.dumps(line, ensure_ascii=False) for line in lines)
    return "(\n" + body + "\n" + " " * indent + ")"


def _docstring(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def _annotation(param: Parameter) -> str:
    if param.type == "array":
        base = f"list[{_PY_TYPES[param.items]}]"
    else:
        base = _PY_TYPES[param.type]
    return f"{base} | None" if param.optional and param.default is None else base


def _signature(tool: FunctionTool) -> str:
    # Python needs required parameters before defaulted ones; keep the
    # blueprint's order otherwise.
    ordered = [p for p in tool.parameters if not p.optional] + [
        p for p in tool.parameters if p.optional
    ]
    parts = []
    for p in ordered:
        part = f"{p.name}: {_annotation(p)}"
        if p.optional:
            part += f" = {p.default!r}"
        parts.append(part)
    return ", ".join(parts)


def _function_body(tool: FunctionTool) -> str:
    if tool.implementation:
        body = textwrap.dedent(tool.implementation).strip("\n")
    else:
        args = ", ".join(f'"{p.name}": {p.name}' for p in tool.parameters)
        body = (
            "# TODO: implement. The blueprint gave no implementation, so this\n"
            "# stub lets the agent run end to end until you do.\n"
            f'return {{"status": "not_implemented", "tool": "{tool.name}", '
            f'"arguments": {{{args}}}}}'
        )
    return textwrap.indent(body, "    ")


def _agent_var(bp: Blueprint, agent: AgentSpec) -> str:
    return "root_agent" if agent is bp.agent else f"{agent.name}_agent"


def _tool_expr(tool, *, search_needs_bypass: bool) -> str:
    if isinstance(tool, BuiltinTool):
        if tool.name == "google_search":
            # Gemini won't mix its built-in search with function calling in one
            # request; the bypass runs search as its own model call.
            return (
                "GoogleSearchTool(bypass_multi_tools_limit=True)"
                if search_needs_bypass
                else "GoogleSearchTool()"
            )
        return tool.name
    return tool.name


def _post_order(agent: AgentSpec):
    for sub in agent.sub_agents:
        yield from _post_order(sub)
    yield agent


def _agent_context(bp: Blueprint) -> list[dict]:
    multi_agent = len(bp.agents()) > 1
    agents = []
    # Sub-agents must be defined before the agents that reference them.
    for agent in _post_order(bp.agent):
        name = bp.root_agent_name if agent is bp.agent else agent.name
        # Every agent in a multi-agent tree gets ADK's transfer_to_agent tool.
        bypass = multi_agent or len(agent.tools) > 1
        generation = None
        if agent.generation:
            fields = agent.generation.model_dump(exclude_none=True)
            if fields:
                generation = ", ".join(f"{k}={v!r}" for k, v in fields.items())
        agents.append(
            {
                "var": _agent_var(bp, agent),
                "name": name,
                "model": py_str(agent.model) if agent.model else "MODEL",
                "description": py_str(agent.description or bp.description),
                "instruction": py_str(agent.instruction.strip(), indent=4),
                "tools": [_tool_expr(t, search_needs_bypass=bypass) for t in agent.tools],
                "sub_agents": [_agent_var(bp, s) for s in agent.sub_agents],
                "generation": generation,
            }
        )
    return agents


def _jinja() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["py_str"] = py_str
    env.filters["py_repr"] = repr
    env.filters["py_list"] = lambda v: json.dumps(v, ensure_ascii=False)
    return env


def render_agent(bp: Blueprint) -> str:
    """app/agent.py for the generated project."""
    functions, mcp_tools, builtins = [], [], set()
    for agent in bp.agents():
        for tool in agent.tools:
            if isinstance(tool, FunctionTool):
                functions.append(
                    {
                        "name": tool.name,
                        "signature": _signature(tool),
                        "description": _docstring(tool.description.strip()),
                        "params": [
                            {"name": p.name, "description": _docstring(p.description.strip())}
                            for p in tool.parameters
                        ],
                        "body": _function_body(tool),
                    }
                )
            elif isinstance(tool, McpTool):
                mcp_tools.append(tool)
            else:
                builtins.add(tool.name)

    return _jinja().get_template("agent.py.j2").render(
        bp=bp,
        description=_docstring(bp.description or f"The {bp.root_agent_name} agent."),
        functions=functions,
        mcp_tools=mcp_tools,
        builtin_imports=[_BUILTIN_IMPORTS[b] for b in sorted(builtins)],
        agents=_agent_context(bp),
    )


def render_eval_dataset(bp: Blueprint) -> str:
    """tests/eval/datasets/basic-dataset.json in the agents-cli eval format.

    ``expected_tool_calls`` is an extra field on each case; agents-cli keeps
    unknown case fields through generate and hands them to custom metrics,
    which is how tool_calls.py reads it.
    """
    cases = []
    for case in bp.evals:
        entry: dict = {
            "eval_case_id": case.id,
            "prompt": {"role": "user", "parts": [{"text": case.prompt}]},
        }
        if case.expected_tool_calls:
            entry["expected_tool_calls"] = case.expected_tool_calls
        if case.reference:
            entry["reference"] = {
                "response": {"role": "model", "parts": [{"text": case.reference}]}
            }
        cases.append(entry)
    if not cases:
        cases.append(
            {
                "eval_case_id": "smoke",
                "prompt": {
                    "role": "user",
                    "parts": [{"text": "Hello, what can you help me with?"}],
                },
            }
        )
    return json.dumps({"eval_cases": cases}, indent=2, ensure_ascii=False) + "\n"


def render_eval_config(bp: Blueprint) -> str:
    """tests/eval/eval_config.yaml for `agents-cli eval run`."""
    metrics = []
    if any(case.expected_tool_calls for case in bp.evals):
        metrics.append(
            {"name": "tool_call_match", "custom_function_file": "tool_calls.py"}
        )
    # response_quality.py is the LLM-as-judge that agents-cli scaffolds.
    metrics.append(
        {"name": "custom_response_quality", "custom_function_file": "response_quality.py"}
    )
    header = (
        "# Generated by agent-builder from blueprint.yml.\n"
        "#   tool_call_match          deterministic: did the agent call the tools\n"
        "#                            each case lists in expected_tool_calls?\n"
        "#   custom_response_quality  LLM-as-judge, 1-5 (uses reference when given)\n"
    )
    body = yaml.safe_dump(
        {"metrics_to_run": [m["name"] for m in metrics], "custom_metrics": metrics},
        sort_keys=False,
    )
    return header + body


def render_unit_test(bp: Blueprint) -> str:
    """tests/unit/test_agent_config.py - checks the agent matches the blueprint."""
    expected: dict[str, list[str]] = {}
    for agent in bp.agents():
        name = bp.root_agent_name if agent is bp.agent else agent.name
        expected[name] = [
            t.name
            for t in agent.tools
            if isinstance(t, FunctionTool)
            or (isinstance(t, BuiltinTool) and t.name == "load_web_page")
        ]
    return _jinja().get_template("test_agent_config.py.j2").render(
        root_name=bp.root_agent_name,
        expected=expected,
        eval_ids=[c.id for c in bp.evals] or ["smoke"],
        mcp_count=len(bp.mcp_tools()),
    )


def env_entries(bp: Blueprint) -> dict[str, str]:
    """Variables for .env: the blueprint's env plus each MCP URL's default."""
    entries = dict(bp.env)
    for tool in bp.mcp_tools():
        entries.setdefault(tool.env_var, tool.url)
    return entries


def render_readme_section(bp: Blueprint) -> str:
    agents = ", ".join(
        f"`{a.name or bp.root_agent_name}`" for a in bp.agents()
    )
    target = bp.deployment.target
    return textwrap.dedent(
        f"""

        ## Generated by agent-builder

        This project was scaffolded with `agents-cli create` and rendered from
        [`blueprint.yml`](blueprint.yml) by agent-builder. Agents: {agents}.

        ```bash
        agents-cli install                 # uv sync
        uv run pytest tests/unit           # blueprint conformance, no model calls
        agents-cli playground              # chat with it locally
        agents-cli eval run                # tool_call_match + response quality
        agents-cli deploy                  # -> {target}
        ```

        Set real values in `.env` before deploying: `agents-cli deploy` ships
        `.env` as the deployed agent's environment.
        """
    )


def render_all(bp: Blueprint) -> dict[str, str]:
    """Every file the builder writes, keyed by path relative to the project."""
    return {
        "app/agent.py": render_agent(bp),
        "blueprint.yml": dump_blueprint(bp),
        "tests/eval/datasets/basic-dataset.json": render_eval_dataset(bp),
        "tests/eval/eval_config.yaml": render_eval_config(bp),
        "tests/eval/tool_calls.py": (TEMPLATE_DIR / "tool_calls.py").read_text(),
        "tests/unit/test_agent_config.py": render_unit_test(bp),
    }
