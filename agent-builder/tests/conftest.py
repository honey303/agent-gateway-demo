"""Shared fixtures and test tiers.

    uv run pytest tests/unit                         # fast, offline, no agents-cli
    uv run pytest tests/integration -m agents_cli    # real `agents-cli create`
    uv run pytest tests/integration -m llm           # calls Gemini; needs credentials

Tests marked `agents_cli` skip when agents-cli isn't installed; tests marked
`llm` skip unless model credentials are available.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.builder.blueprint import REFERENCE_BLUEPRINT

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_BLUEPRINTS = sorted((PROJECT_ROOT / "blueprints").glob("*.yml")) + [REFERENCE_BLUEPRINT]

MINIMAL_BLUEPRINT = """\
apiVersion: agent-builder/v1
name: tiny-agent
agent:
  instruction: Be brief.
"""


def _agents_cli_available() -> bool:
    from app.builder.generator import BuildError, agents_cli_command

    try:
        agents_cli_command()
    except BuildError:
        return False
    return True


def _model_credentials_available() -> bool:
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return True
    try:
        import google.auth

        _, project = google.auth.default()
    except Exception:
        return False
    return bool(project or os.environ.get("GOOGLE_CLOUD_PROJECT"))


def pytest_configure(config):
    config.addinivalue_line("markers", "agents_cli: runs the real agents-cli binary")
    config.addinivalue_line("markers", "llm: calls a Gemini model; needs credentials")


def pytest_collection_modifyitems(config, items):
    skips = {
        "agents_cli": (_agents_cli_available, "agents-cli is not installed"),
        "llm": (_model_credentials_available, "no Gemini / Google Cloud credentials"),
    }
    cache: dict[str, bool] = {}
    for item in items:
        for marker, (check, reason) in skips.items():
            if marker in item.keywords:
                if marker not in cache:
                    cache[marker] = check()
                if not cache[marker]:
                    item.add_marker(pytest.mark.skip(reason=reason))


class FakeAgentsCli:
    """Stands in for subprocess.run: fakes `agents-cli create`, records every call.

    The scaffold is the handful of files the generator reads or appends to,
    shaped like what agents-cli 1.7 writes.
    """

    def __init__(self, fail_create: bool = False, fail_lock: bool = False):
        self.calls: list[list[str]] = []
        self.fail_create = fail_create
        self.fail_lock = fail_lock

    def __call__(self, cmd, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if "create" in cmd:
            if self.fail_create:
                return subprocess.CompletedProcess(cmd, 2, "", "Error: template not found\n")
            name = cmd[cmd.index("create") + 1]
            out = Path(cmd[cmd.index("--output-dir") + 1]) / name
            self._scaffold(out, name)
        if cmd[-1:] == ["lock"] and self.fail_lock:
            return subprocess.CompletedProcess(cmd, 1, "", "No network\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def commands(self, word: str) -> list[list[str]]:
        return [c for c in self.calls if word in c]

    @staticmethod
    def _scaffold(out: Path, name: str) -> None:
        (out / "app").mkdir(parents=True)
        (out / "tests/eval/datasets").mkdir(parents=True)
        (out / "tests/unit").mkdir(parents=True)
        (out / "app/__init__.py").write_text("from .agent import app\n")
        (out / "app/agent.py").write_text("# scaffold placeholder\n")
        (out / "tests/eval/response_quality.py").write_text("def evaluate(instance):\n    return {'score': 5}\n")
        (out / "README.md").write_text(f"# {name}\n")
        env = "GOOGLE_GENAI_USE_VERTEXAI=true\nGOOGLE_CLOUD_PROJECT=your-gcp-project-id\n"
        (out / ".env").write_text(env)
        (out / ".env.example").write_text(env)
        (out / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "0.1.0"\ndescription = ""\n'
            'dependencies = [\n    "google-adk[gcp]>=2.8.0,<2.9.0",\n]\n'
        )
        (out / ".venv/lib").mkdir(parents=True)
        (out / ".venv/lib/junk.py").write_text("")


@pytest.fixture
def fake_cli() -> FakeAgentsCli:
    return FakeAgentsCli()
