"""The builder agent's tools and callback, without a model."""

import functools
import io
import zipfile
from types import SimpleNamespace

import pytest
import yaml
from google.adk.models.llm_request import LlmRequest
from google.genai import types

import app.agent as builder
from app.builder.generator import BuildError, build_project
from tests.conftest import MINIMAL_BLUEPRINT, FakeAgentsCli


class FakeToolContext:
    """What the tools use of ADK's ToolContext."""

    def __init__(self, user_content=None, history=()):
        self.user_content = user_content
        self.session = SimpleNamespace(events=list(history))
        self.artifacts: dict[str, types.Part] = {}

    async def save_artifact(self, filename, artifact):
        self.artifacts[filename] = artifact
        return 0


def attachment(text: str, mime="application/x-yaml", name="blueprint.yml") -> types.Part:
    return types.Part(inline_data=types.Blob(data=text.encode(), mime_type=mime, display_name=name))


def user_message(*parts) -> types.Content:
    return types.Content(role="user", parts=list(parts))


@pytest.fixture
def fake_build(monkeypatch):
    """build_agent runs the real generator against a fake agents-cli."""
    monkeypatch.setenv("AGENTS_CLI", "agents-cli")
    cli = FakeAgentsCli()
    monkeypatch.setattr(builder, "build_project", functools.partial(build_project, runner=cli, lock=False))
    return cli


def test_reference_is_a_valid_blueprint():
    from app.builder.blueprint import parse_blueprint

    ref = builder.get_blueprint_reference()
    assert ref["api_version"] == "agent-builder/v1"
    assert parse_blueprint(ref["annotated_example"]).name == "trip-helper"
    assert "agent" in ref["json_schema"]["properties"]


def test_validate_valid():
    result = builder.validate_blueprint(MINIMAL_BLUEPRINT, FakeToolContext())
    assert result["status"] == "valid"
    assert result["agents"] == ["tiny_agent"]
    assert result["deployment_target"] == "agent_runtime"


def test_validate_invalid_lists_errors():
    result = builder.validate_blueprint(MINIMAL_BLUEPRINT.replace("tiny-agent", "Tiny"), FakeToolContext())
    assert result["status"] == "invalid"
    assert any("name 'Tiny'" in e for e in result["errors"])


def test_validate_missing():
    assert builder.validate_blueprint("  ", FakeToolContext())["status"] == "missing"


@pytest.mark.parametrize(
    ("mime", "name"),
    [("application/x-yaml", "bp.yml"), ("text/plain", "notes.txt"), ("application/octet-stream", "bp.yaml")],
)
def test_validate_reads_attachment(mime, name):
    ctx = FakeToolContext(user_message(types.Part.from_text(text="check this"), attachment(MINIMAL_BLUEPRINT, mime, name)))
    assert builder.validate_blueprint("", ctx)["status"] == "valid"


def test_attachment_from_earlier_turn():
    earlier = SimpleNamespace(author="user", content=user_message(attachment(MINIMAL_BLUEPRINT)))
    ctx = FakeToolContext(user_message(types.Part.from_text(text="now build it")), history=[earlier])
    assert builder.validate_blueprint("", ctx)["status"] == "valid"


def test_binary_attachment_ignored():
    image = types.Part(inline_data=types.Blob(data=b"\x89PNG\x00\xff", mime_type="image/png"))
    assert builder.validate_blueprint("", FakeToolContext(user_message(image)))["status"] == "missing"


async def test_build_success(fake_build, monkeypatch):
    monkeypatch.setattr(builder, "BUILD_OUTPUT_DIR", None)
    monkeypatch.setattr(builder, "BUILD_OUTPUT_BUCKET", None)
    ctx = FakeToolContext()
    blueprint = open("blueprints/dice_mcp_agent.yml").read()

    result = await builder.build_agent(blueprint, ctx)

    assert result["status"] == "success", result
    assert result["artifact"]["filename"] == "dice-mcp-agent.zip"
    assert "app/agent.py" in result["rendered_files"]
    assert result["next_steps"][0] == "unzip dice-mcp-agent.zip"
    assert "local_path" not in result and "gcs_uri" not in result

    part = ctx.artifacts["dice-mcp-agent.zip"]
    assert part.inline_data.mime_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(part.inline_data.data)) as archive:
        agent_py = archive.read("dice-mcp-agent/app/agent.py").decode()
    assert 'os.environ.get("MCP_SERVER_URL"' in agent_py

    # the scaffold went to a temp dir that is cleaned up afterwards
    out_dir = fake_build.commands("create")[0]
    assert not builder.Path(out_dir[out_dir.index("--output-dir") + 1]).exists()


async def test_build_keeps_output_dir_and_uploads(fake_build, monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "BUILD_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(builder, "BUILD_OUTPUT_BUCKET", "my-bucket")
    uploads = []
    monkeypatch.setattr(
        builder, "_upload_to_gcs",
        lambda bucket, name, data: uploads.append((bucket, name)) or f"gs://{bucket}/{name}.zip",
    )
    result = await builder.build_agent(MINIMAL_BLUEPRINT, FakeToolContext())
    assert result["local_path"] == str(tmp_path / "tiny-agent")
    assert (tmp_path / "tiny-agent/app/agent.py").exists()
    assert result["gcs_uri"] == "gs://my-bucket/tiny-agent.zip"
    assert uploads == [("my-bucket", "tiny-agent")]


async def test_build_upload_failure_is_a_warning(fake_build, monkeypatch):
    monkeypatch.setattr(builder, "BUILD_OUTPUT_DIR", None)
    monkeypatch.setattr(builder, "BUILD_OUTPUT_BUCKET", "my-bucket")

    def fail(*args):
        raise PermissionError("403 storage.objects.create denied")

    monkeypatch.setattr(builder, "_upload_to_gcs", fail)
    result = await builder.build_agent(MINIMAL_BLUEPRINT, FakeToolContext())
    assert result["status"] == "success"
    assert any("403" in w for w in result["warnings"])


async def test_build_invalid_does_not_scaffold(fake_build):
    bad = yaml.safe_load(MINIMAL_BLUEPRINT) | {"evals": [{"id": "a", "prompt": "p", "expected_tool_calls": ["x"]}]}
    result = await builder.build_agent(yaml.safe_dump(bad), FakeToolContext())
    assert result["status"] == "invalid"
    assert not fake_build.calls


async def test_build_error(monkeypatch):
    def broken(*args, **kwargs):
        raise BuildError("agents-cli create exited with 1")

    monkeypatch.setattr(builder, "build_project", broken)
    ctx = FakeToolContext()
    result = await builder.build_agent(MINIMAL_BLUEPRINT, ctx)
    assert result == {"status": "error", "errors": ["agents-cli create exited with 1"]}
    assert not ctx.artifacts


def test_callback_shows_yaml_attachments_as_text():
    original = user_message(types.Part.from_text(text="build this"), attachment(MINIMAL_BLUEPRINT))
    image = types.Part(inline_data=types.Blob(data=b"\x89PNG", mime_type="image/png"))
    other = user_message(image)
    request = LlmRequest(contents=[original, other])

    builder.inline_text_attachments(callback_context=None, llm_request=request)

    rewritten = request.contents[0]
    assert rewritten is not original, "the session's own event must not be modified"
    assert original.parts[1].inline_data is not None
    assert rewritten.parts[0].text == "build this"
    assert rewritten.parts[1].text.startswith("[Attached file blueprint.yml]\n```yaml\napiVersion")
    assert request.contents[1] is other


def test_agent_wiring():
    assert builder.app.root_agent is builder.root_agent
    assert [t.__name__ for t in builder.root_agent.tools] == [
        "get_blueprint_reference", "validate_blueprint", "build_agent",
    ]
    # ADK treats {name} in instructions as state placeholders.
    assert "{" not in builder.INSTRUCTION
