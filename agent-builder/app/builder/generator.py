"""Builds an ADK agent project from a Blueprint.

Two stages:

1. **Scaffold** with ``agents-cli create`` - the project layout, FastAPI
   server, Dockerfile, deployment config, eval harness and uv lockfile all
   come from agents-cli, so the result is an ordinary agents-cli project
   that ``agents-cli install / playground / eval run / deploy`` work on.
2. **Render** the blueprint over it (see ``render.py``): the agent itself,
   eval dataset and config, a conformance unit test, dependencies and env.
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from app.builder import render
from app.builder.blueprint import Blueprint, warnings_for

# Signature of subprocess.run, injectable so tests can fake agents-cli.
Runner = Callable[..., subprocess.CompletedProcess]

# Never shipped in a packaged project: local state, caches, and .env (the
# generated .env.example carries the same keys).
_ZIP_EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "artifacts", ".google-agents-cli"}
_ZIP_EXCLUDE_FILES = {".env"}

MCP_REQUIREMENT = "mcp>=1.24,<2"


class BuildError(RuntimeError):
    """agents-cli (or uv) failed while building the project."""


@dataclass
class BuildResult:
    project_dir: Path
    blueprint: Blueprint
    files_written: list[str]
    warnings: list[str] = field(default_factory=list)
    scaffold_command: list[str] = field(default_factory=list)

    def next_steps(self) -> list[str]:
        target = self.blueprint.deployment.target
        steps = [
            f"cd {self.project_dir.name}",
            "agents-cli install",
            "uv run pytest tests/unit",
            "agents-cli playground",
            "agents-cli eval run",
            f"agents-cli deploy  # -> {target}",
        ]
        if self.blueprint.mcp_tools():
            steps.insert(
                1,
                "set the MCP server URL(s) in .env - agents-cli deploy ships .env "
                "as the deployed agent's environment",
            )
        return steps


def agents_cli_command() -> list[str]:
    """How to invoke agents-cli: $AGENTS_CLI, then PATH, then this interpreter."""
    if override := os.environ.get("AGENTS_CLI"):
        return override.split()
    if exe := shutil.which("agents-cli"):
        return [exe]
    if importlib.util.find_spec("google.agents.cli") is not None:
        return [sys.executable, "-m", "google.agents.cli.main"]
    raise BuildError(
        "agents-cli not found. Install it with `uv tool install google-agents-cli` "
        "or set AGENTS_CLI to its path."
    )


def scaffold_command(bp: Blueprint, output_dir: Path) -> list[str]:
    cmd = [
        *agents_cli_command(),
        "create",
        bp.name,
        "--agent", "adk",
        "--deployment-target", bp.deployment.target,
        "--region", bp.deployment.region,
        "--session-type", bp.deployment.session_type,
        "--root-agent-name", bp.root_agent_name,
        "--output-dir", str(output_dir),
        "--agent-guidance-filename", "AGENTS.md",
        # No CI/CD pipeline; `agents-cli scaffold enhance` adds one later.
        "--prototype",
        "--yes",
        # Skips the live GCP / Vertex AI checks, so building needs no credentials.
        "--skip-checks",
    ]
    if bp.deployment.agent_gateway:
        cmd.append("--agent-gateway")
    return cmd


def _run(cmd: Sequence[str], runner: Runner, what: str, **kwargs) -> None:
    try:
        proc = runner(list(cmd), capture_output=True, text=True, **kwargs)
    except FileNotFoundError as e:
        raise BuildError(f"{what} failed: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise BuildError(f"{what} timed out after {e.timeout}s") from e
    if proc.returncode != 0:
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        tail = "\n".join(output.splitlines()[-20:])
        raise BuildError(f"{what} exited with {proc.returncode}:\n{tail}")


def _requirement_name(req: str) -> str:
    return canonicalize_name(Requirement(req).name)


def _update_pyproject(path: Path, bp: Blueprint) -> bool:
    """Adds the blueprint's dependencies. Returns whether any were added."""
    doc = tomlkit.parse(path.read_text())
    project = doc["project"]
    if bp.description:
        project["description"] = bp.description
    deps = project["dependencies"]
    wanted = list(bp.dependencies)
    if bp.mcp_tools():
        wanted.append(MCP_REQUIREMENT)
    present = {_requirement_name(str(d)) for d in deps}
    added = False
    for req in wanted:
        if _requirement_name(req) not in present:
            deps.append(req)
            present.add(_requirement_name(req))
            added = True
    path.write_text(tomlkit.dumps(doc))
    return added


def _append_env(path: Path, entries: dict[str, str]) -> None:
    if not entries:
        return
    text = path.read_text() if path.exists() else ""
    present = {line.split("=", 1)[0].strip() for line in text.splitlines() if "=" in line}
    lines = [f"{k}={v}" for k, v in entries.items() if k not in present]
    if not lines:
        return
    if text and not text.endswith("\n"):
        text += "\n"
    text += "\n# From blueprint.yml (agent-builder)\n" + "\n".join(lines) + "\n"
    path.write_text(text)


def _ruff_command() -> list[str] | None:
    if exe := shutil.which("ruff"):
        return [exe]
    try:
        from ruff.__main__ import find_ruff_bin

        return [find_ruff_bin()]
    except (ImportError, FileNotFoundError):
        return None


def _format_python(project_dir: Path, files: list[str], runner: Runner) -> list[str]:
    """Formats rendered Python with the project's own ruff settings, so
    `agents-cli lint` passes on a fresh build. Returns warnings."""
    ruff = _ruff_command()
    if ruff is None:
        return ["ruff not found: rendered code is unformatted (`agents-cli lint --fix` fixes it)"]
    try:
        _run([*ruff, "check", "--select", "I", "--fix", *files], runner, "ruff check", cwd=project_dir, timeout=120)
        _run([*ruff, "format", *files], runner, "ruff format", cwd=project_dir, timeout=120)
    except BuildError as e:
        return [str(e)]
    return []


def build_project(
    bp: Blueprint,
    output_dir: str | Path,
    *,
    overwrite: bool = False,
    lock: bool = True,
    runner: Runner = subprocess.run,
) -> BuildResult:
    """Scaffolds and renders `bp` into ``output_dir/<bp.name>``.

    lock: re-lock with ``uv lock`` when the blueprint adds dependencies, so
        the Dockerfile's ``uv sync --frozen`` installs them. Needs uv and
        PyPI access; on failure the build still succeeds, with a warning.
    """
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    project_dir = output_dir / bp.name
    if project_dir.exists():
        if not overwrite:
            raise FileExistsError(f"{project_dir} already exists (pass overwrite=True / --force)")
        shutil.rmtree(project_dir)

    cmd = scaffold_command(bp, output_dir)
    _run(cmd, runner, "agents-cli create", timeout=600)
    if not (project_dir / "pyproject.toml").exists():
        raise BuildError(f"agents-cli create did not produce {project_dir}/pyproject.toml")

    written = []
    for rel, content in render.render_all(bp).items():
        target = project_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel)

    warnings = warnings_for(bp)
    warnings.extend(_format_python(project_dir, [f for f in written if f.endswith(".py")], runner))

    env = render.env_entries(bp)
    for name in (".env", ".env.example"):
        _append_env(project_dir / name, env)

    readme = project_dir / "README.md"
    if readme.exists():
        readme.write_text(readme.read_text() + render.render_readme_section(bp))

    if _update_pyproject(project_dir / "pyproject.toml", bp) and lock:
        uv = shutil.which("uv")
        if uv is None:
            warnings.append("uv not found: run `uv lock` before deploying, or the image misses new dependencies")
        else:
            try:
                _run([uv, "lock"], runner, "uv lock", cwd=project_dir, timeout=300)
            except BuildError as e:
                warnings.append(f"{e} - run `uv lock` in the project before deploying")

    return BuildResult(
        project_dir=project_dir,
        blueprint=bp,
        files_written=written,
        warnings=warnings,
        scaffold_command=cmd,
    )


def project_files(project_dir: Path) -> list[str]:
    """Files that belong in a packaged copy of the project, sorted."""
    files = []
    for path in project_dir.rglob("*"):
        rel = path.relative_to(project_dir)
        if any(part in _ZIP_EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.is_file() and path.name not in _ZIP_EXCLUDE_FILES:
            files.append(rel.as_posix())
    return sorted(files)


def zip_project(project_dir: Path) -> bytes:
    """The project as a zip whose entries sit under ``<project name>/``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in project_files(project_dir):
            archive.write(project_dir / rel, f"{project_dir.name}/{rel}")
    return buffer.getvalue()
