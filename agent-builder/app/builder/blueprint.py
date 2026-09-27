"""The blueprint: a YAML spec that the builder turns into an ADK agent project.

A blueprint describes *what* agent to build - its instruction, tools,
sub-agents, deployment target and eval cases. The generator
(``generator.py``) scaffolds the project with ``agents-cli create`` and then
renders the agent from this spec.

Everything here is validated before anything touches disk, so a blueprint
that loads without raising is one the generator can render into code that
imports cleanly. See ``reference_blueprint.yml`` for an annotated example
covering every field.
"""

from __future__ import annotations

import ast
import keyword
import re
import textwrap
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

API_VERSION = "agent-builder/v1"

# Same default the agents-cli ADK template ships with.
DEFAULT_MODEL = "gemini-3.8-flash"

_PROJECT_NAME = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
# agents-cli derives Cloud Run service / Agent Runtime display names from the
# project name, and Cloud Run caps service names at 49 characters.
_PROJECT_NAME_MAX = 40
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_VAR = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Module-level names the generated agent.py defines or imports itself. A tool
# or agent with one of these names would shadow it.
RESERVED_NAMES = frozenset(
    {
        "Agent", "App", "Gemini", "McpToolset", "StreamableHTTPConnectionParams",
        "GoogleSearchTool", "ToolContext", "app", "datetime", "json", "math",
        "os", "random", "root_agent", "time", "types", "urlparse", "user",
    }
)

BUILTIN_TOOLS = ("google_search", "url_context", "load_web_page")

# ADK's function name for delegating to a sub-agent. Valid in
# expected_tool_calls whenever the agent tree has sub-agents.
TRANSFER_TOOL = "transfer_to_agent"


class BlueprintError(ValueError):
    """A blueprint failed to parse or validate. ``errors`` lists every problem."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Invalid blueprint:\n" + "\n".join(f"  - {e}" for e in errors))


class _Spec(BaseModel):
    # Typos in a hand-written spec should fail loudly, not be ignored.
    model_config = ConfigDict(extra="forbid")


class Parameter(_Spec):
    """One argument of a function tool."""

    name: str
    type: Literal["string", "integer", "number", "boolean", "array", "object"] = "string"
    # Element type when type is "array".
    items: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = ""
    # A parameter with a default is optional regardless of this flag.
    required: bool = True
    default: Any = None

    @property
    def optional(self) -> bool:
        return not self.required or self.default is not None


class FunctionTool(_Spec):
    """A Python function the agent can call.

    ``implementation`` is the function *body*. Leave it out to get a stub that
    returns a not-implemented result, so the agent still runs end to end.
    """

    type: Literal["function"]
    name: str
    description: str
    parameters: list[Parameter] = []
    implementation: str | None = None


class McpTool(_Spec):
    """Tools served by a remote MCP server over Streamable HTTP.

    HTTP rather than stdio because Agent Runtime pickles the agent, and a
    stdio toolset holds a subprocess pipe that cannot be pickled.
    """

    type: Literal["mcp"]
    name: str
    url: str
    # Env var that overrides ``url`` at runtime. Defaults to <NAME>_URL.
    url_env: str | None = None
    # Whether to attach a Google ID token (for IAM-protected Cloud Run):
    # auto = for https:// URLs only, on = always, off = never.
    auth: Literal["auto", "on", "off"] = "auto"
    # Only expose these MCP tools to the agent. Also what lets eval cases
    # reference MCP tool names in expected_tool_calls.
    tool_filter: list[str] | None = None

    @property
    def env_var(self) -> str:
        return self.url_env or f"{self.name.upper()}_URL"


class BuiltinTool(_Spec):
    """A tool that ships with ADK."""

    type: Literal["builtin"]
    name: Literal["google_search", "url_context", "load_web_page"]


Tool = Annotated[FunctionTool | McpTool | BuiltinTool, Field(discriminator="type")]


class Generation(_Spec):
    """Sampling settings, passed through as GenerateContentConfig."""

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_output_tokens: int | None = Field(default=None, gt=0)


class AgentSpec(_Spec):
    """An LLM agent. Sub-agents use the same shape, recursively."""

    # Optional on the root agent (derived from the project name); required on
    # sub-agents.
    name: str | None = None
    # Defaults to the blueprint's top-level model.
    model: str | None = None
    description: str = ""
    instruction: str
    tools: list[Tool] = []
    sub_agents: list[AgentSpec] = []
    generation: Generation | None = None

    def walk(self):
        """Yields this agent and every descendant, depth first."""
        yield self
        for sub in self.sub_agents:
            yield from sub.walk()


class Deployment(_Spec):
    """How agents-cli scaffolds and deploys the generated project."""

    target: Literal["agent_runtime", "cloud_run"] = "agent_runtime"
    region: str = "us-central1"
    session_type: Literal["in_memory", "cloud_sql", "agent_platform_sessions"] = "in_memory"
    # Trust the Agent Gateway's TLS-inspection CA (agents-cli --agent-gateway).
    agent_gateway: bool = False


class EvalCase(_Spec):
    """One `agents-cli eval` case."""

    id: str
    prompt: str
    # Tools the agent must call to answer. Graded by the tool_call_match metric.
    expected_tool_calls: list[str] = []
    # Ground-truth answer for the LLM-as-judge metric.
    reference: str | None = None


class Blueprint(_Spec):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api_version: str = Field(default=API_VERSION, alias="apiVersion")
    name: str
    description: str = ""
    model: str = DEFAULT_MODEL
    deployment: Deployment = Deployment()
    agent: AgentSpec
    # Extra PyPI requirements, e.g. for imports in function implementations.
    dependencies: list[str] = []
    # Written to the generated project's .env / .env.example.
    env: dict[str, str] = {}
    evals: list[EvalCase] = []

    @property
    def root_agent_name(self) -> str:
        return self.agent.name or self.name.replace("-", "_")

    def agents(self) -> list[AgentSpec]:
        return list(self.agent.walk())

    def mcp_tools(self) -> list[McpTool]:
        return [t for a in self.agents() for t in a.tools if isinstance(t, McpTool)]

    def callable_tool_names(self) -> tuple[set[str], bool]:
        """Function names the model can emit, and whether that set is complete.

        It is incomplete when an MCP toolset has no tool_filter: its tools are
        only discoverable at runtime.
        """
        names: set[str] = set()
        complete = True
        for agent in self.agents():
            if agent.sub_agents:
                names.add(TRANSFER_TOOL)
            for tool in agent.tools:
                if isinstance(tool, McpTool):
                    if tool.tool_filter is None:
                        complete = False
                    else:
                        names.update(tool.tool_filter)
                else:
                    names.add(tool.name)
        return names, complete

    @model_validator(mode="after")
    def _check(self) -> Blueprint:
        errors = _semantic_errors(self)
        if errors:
            raise ValueError("\n".join(errors))
        return self


def _is_identifier(name: str) -> bool:
    return bool(_IDENTIFIER.match(name)) and not keyword.iskeyword(name)


def _semantic_errors(bp: Blueprint) -> list[str]:
    """Checks pydantic can't express: naming, uniqueness, cross-references."""
    errors: list[str] = []

    if bp.api_version != API_VERSION:
        errors.append(f"apiVersion must be '{API_VERSION}', got '{bp.api_version}'")

    if not _PROJECT_NAME.match(bp.name) or len(bp.name) > _PROJECT_NAME_MAX:
        errors.append(
            f"name '{bp.name}' must be lowercase letters, digits and hyphens, "
            f"start with a letter, and be at most {_PROJECT_NAME_MAX} characters"
        )

    # Every agent and tool becomes a module-level name in agent.py, so they
    # share one namespace.
    module_names: dict[str, str] = {}

    def claim(name: str, what: str) -> None:
        if not _is_identifier(name):
            errors.append(f"{what} name '{name}' must be a valid Python identifier")
        elif name in RESERVED_NAMES:
            errors.append(f"{what} name '{name}' is reserved by the generated code")
        elif name in module_names:
            errors.append(f"{what} name '{name}' is already used by {module_names[name]}")
        else:
            module_names[name] = what

    for depth, agent in enumerate(bp.agents()):
        if depth == 0:
            agent_name = bp.root_agent_name
        elif agent.name is None:
            errors.append("every sub-agent needs a name")
            continue
        else:
            agent_name = agent.name
        claim(f"{agent_name}_agent", f"agent '{agent_name}'")

        tool_names: set[str] = set()
        for tool in agent.tools:
            if tool.name in tool_names:
                errors.append(f"agent '{agent_name}' lists tool '{tool.name}' twice")
            tool_names.add(tool.name)

            if isinstance(tool, FunctionTool):
                claim(tool.name, "function tool")
                errors.extend(_function_errors(tool))
            elif isinstance(tool, McpTool):
                claim(tool.name, "MCP tool")
                if not tool.url.startswith(("http://", "https://")):
                    errors.append(f"MCP tool '{tool.name}': url must be http(s)://")
                if not _ENV_VAR.match(tool.env_var):
                    errors.append(
                        f"MCP tool '{tool.name}': url_env '{tool.env_var}' must be "
                        "an UPPER_SNAKE_CASE environment variable name"
                    )

    for req in bp.dependencies:
        try:
            Requirement(req)
        except InvalidRequirement:
            errors.append(f"dependency '{req}' is not a valid PEP 508 requirement")

    for key in bp.env:
        if not _ENV_VAR.match(key):
            errors.append(f"env key '{key}' must be UPPER_SNAKE_CASE")

    known, complete = bp.callable_tool_names()
    seen_ids: set[str] = set()
    for case in bp.evals:
        if case.id in seen_ids:
            errors.append(f"eval case id '{case.id}' is used twice")
        seen_ids.add(case.id)
        if complete:
            for name in case.expected_tool_calls:
                if name not in known:
                    errors.append(
                        f"eval case '{case.id}' expects tool '{name}', which no "
                        f"agent has (known: {', '.join(sorted(known)) or 'none'})"
                    )
    return errors


def _function_errors(tool: FunctionTool) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for param in tool.parameters:
        if not _is_identifier(param.name):
            errors.append(f"tool '{tool.name}': parameter '{param.name}' is not a valid identifier")
        if param.name in seen:
            errors.append(f"tool '{tool.name}': parameter '{param.name}' is listed twice")
        if param.name == "tool_context":
            errors.append(f"tool '{tool.name}': 'tool_context' is reserved by ADK")
        seen.add(param.name)
    if tool.implementation:
        # Parse it as the body of a function, exactly as it will be rendered.
        source = "def _f():\n" + textwrap.indent(textwrap.dedent(tool.implementation), "    ")
        try:
            ast.parse(source)
        except SyntaxError as e:
            errors.append(f"tool '{tool.name}': implementation is not valid Python: {e.msg} (line {e.lineno})")
    return errors


def warnings_for(bp: Blueprint) -> list[str]:
    """Non-fatal issues worth telling the user about."""
    warnings: list[str] = []
    for agent in bp.agents():
        name = agent.name or bp.root_agent_name
        for tool in agent.tools:
            if isinstance(tool, FunctionTool) and not tool.implementation:
                warnings.append(
                    f"function tool '{tool.name}' has no implementation - a stub "
                    "was generated; fill it in app/agent.py"
                )
        builtins = [t.name for t in agent.tools if isinstance(t, BuiltinTool)]
        if "url_context" in builtins and len(agent.tools) > 1:
            warnings.append(
                f"agent '{name}': url_context can't be combined with other tools "
                "in one agent on most Gemini models - move it to a sub-agent"
            )
    _, complete = bp.callable_tool_names()
    if not complete and any(c.expected_tool_calls for c in bp.evals):
        warnings.append(
            "an MCP tool has no tool_filter, so expected_tool_calls can't be "
            "checked against its tools until the eval runs"
        )
    if not bp.evals:
        warnings.append("no evals defined - the generated dataset has a single smoke case")
    return warnings


def _format_validation_error(error: ValidationError) -> list[str]:
    messages = []
    for item in error.errors():
        loc = ".".join(str(part) for part in item["loc"])
        msg = item["msg"].removeprefix("Value error, ")
        # _check() joins all semantic errors into one ValueError; split them back.
        for part in msg.splitlines():
            messages.append(f"{loc}: {part}" if loc else part)
    return messages


def parse_blueprint(text: str) -> Blueprint:
    """Parses and validates blueprint YAML. Raises BlueprintError."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise BlueprintError([f"not valid YAML: {e}"]) from e
    if not isinstance(data, dict):
        raise BlueprintError(["a blueprint must be a YAML mapping"])
    try:
        return Blueprint.model_validate(data)
    except ValidationError as e:
        raise BlueprintError(_format_validation_error(e)) from e


def load_blueprint(path: str | Path) -> Blueprint:
    return parse_blueprint(Path(path).read_text(encoding="utf-8"))


def dump_blueprint(bp: Blueprint) -> str:
    """Normalized YAML for the copy stored in the generated project."""
    data = bp.model_dump(by_alias=True, exclude_defaults=True, mode="json")
    data = {"apiVersion": bp.api_version, **data}
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


REFERENCE_BLUEPRINT = Path(__file__).with_name("reference_blueprint.yml")
