"""agent-builder: an ADK agent that builds ADK agent projects from blueprints.

Give it a blueprint.yml (pasted, attached, or drafted together from a plain
description) and it validates the spec, scaffolds a project with
`agents-cli create`, renders the agent, eval set and tests into it, and hands
the result back as a zip artifact (and optionally a GCS object).

The build itself is deterministic Python in app/builder/ - the model decides
*when* to build and helps write blueprints, but never writes the generated
code. The same pipeline runs without a model via the `agent-builder` CLI.
"""

from __future__ import annotations

import datetime
import os
import shutil
import tempfile
from pathlib import Path

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.tools import ToolContext
from google.genai import types

from app.builder.blueprint import (
    API_VERSION,
    REFERENCE_BLUEPRINT,
    Blueprint,
    BlueprintError,
    parse_blueprint,
    warnings_for,
)
from app.builder.generator import BuildError, build_project, project_files, zip_project

MODEL = "gemini-3.8-flash"

# Optional: also upload each build to gs://$BUILD_OUTPUT_BUCKET/agent-builder/...
BUILD_OUTPUT_BUCKET = os.environ.get("BUILD_OUTPUT_BUCKET")
# Optional: keep builds in this directory instead of a throwaway temp dir
# (handy locally; containers on Cloud Run / Agent Runtime lose it on restart).
BUILD_OUTPUT_DIR = os.environ.get("BUILD_OUTPUT_DIR")

_TEXT_MIME_TYPES = {"application/x-yaml", "application/yaml", "text/yaml", "text/x-yaml", "application/json"}
_BLUEPRINT_SUFFIXES = (".yml", ".yaml")


def _attachment_text(part: types.Part) -> str | None:
    """Decodes an uploaded blueprint-like file (YAML/text) in a message part."""
    blob = part.inline_data
    if blob is None or blob.data is None:
        return None
    mime = (blob.mime_type or "").lower()
    name = (blob.display_name or "").lower()
    if mime.startswith("text/") or mime in _TEXT_MIME_TYPES or name.endswith(_BLUEPRINT_SUFFIXES):
        try:
            return blob.data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _uploaded_blueprint(tool_context: ToolContext) -> str | None:
    """The most recent YAML file the user attached in this session, if any."""
    contents = []
    if tool_context.user_content:
        contents.append(tool_context.user_content)
    for event in reversed(tool_context.session.events):
        if event.author == "user" and event.content:
            contents.append(event.content)
    for content in contents:
        for part in content.parts or []:
            if (text := _attachment_text(part)) is not None:
                return text
    return None


def _resolve(blueprint_yaml: str, tool_context: ToolContext) -> tuple[Blueprint | None, dict | None]:
    """Parses the blueprint from the argument or, if empty, an attachment."""
    text = blueprint_yaml.strip() or _uploaded_blueprint(tool_context)
    if not text:
        return None, {
            "status": "missing",
            "errors": ["No blueprint given: pass the YAML, or attach a blueprint.yml file."],
        }
    try:
        return parse_blueprint(text), None
    except BlueprintError as e:
        return None, {"status": "invalid", "errors": e.errors}


def get_blueprint_reference() -> dict:
    """Returns the blueprint format: an annotated example covering every field,
    and the JSON schema. Use it before drafting or fixing a blueprint."""
    return {
        "api_version": API_VERSION,
        "annotated_example": REFERENCE_BLUEPRINT.read_text(),
        "json_schema": Blueprint.model_json_schema(by_alias=True),
    }


def validate_blueprint(blueprint_yaml: str, tool_context: ToolContext) -> dict:
    """Validates a blueprint without building anything.

    Args:
        blueprint_yaml: The full blueprint YAML. Pass an empty string to use
            the blueprint file the user attached.

    Returns:
        status "valid" with a summary and warnings, or "invalid"/"missing"
        with the list of errors to fix.
    """
    bp, problem = _resolve(blueprint_yaml, tool_context)
    if problem:
        return problem
    return {
        "status": "valid",
        "name": bp.name,
        "deployment_target": bp.deployment.target,
        "agents": [a.name or bp.root_agent_name for a in bp.agents()],
        "tools": sorted(bp.callable_tool_names()[0]),
        "eval_cases": [c.id for c in bp.evals],
        "warnings": warnings_for(bp),
    }


def _upload_to_gcs(bucket: str, name: str, data: bytes) -> str:
    from google.cloud import storage

    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    blob_name = f"agent-builder/{name}/{stamp}.zip"
    storage.Client().bucket(bucket).blob(blob_name).upload_from_string(
        data, content_type="application/zip"
    )
    return f"gs://{bucket}/{blob_name}"


async def build_agent(blueprint_yaml: str, tool_context: ToolContext) -> dict:
    """Generates an ADK agent project from a blueprint with agents-cli.

    Scaffolds with `agents-cli create`, renders the agent, eval dataset and
    tests, and saves the project as a downloadable zip artifact.

    Args:
        blueprint_yaml: The full blueprint YAML. Pass an empty string to use
            the blueprint file the user attached.

    Returns:
        status "success" with the artifact name, file list and next steps;
        "invalid"/"missing" with errors; or "error" if agents-cli failed.
    """
    bp, problem = _resolve(blueprint_yaml, tool_context)
    if problem:
        return problem

    keep = BUILD_OUTPUT_DIR is not None
    output_dir = Path(BUILD_OUTPUT_DIR) if keep else Path(tempfile.mkdtemp(prefix="agent-builder-"))
    try:
        try:
            result = build_project(bp, output_dir, overwrite=True)
        except BuildError as e:
            return {"status": "error", "errors": [str(e)]}
        archive = zip_project(result.project_dir)
        files = project_files(result.project_dir)
    finally:
        if not keep:
            shutil.rmtree(output_dir, ignore_errors=True)

    artifact = f"{bp.name}.zip"
    version = await tool_context.save_artifact(
        artifact, types.Part.from_bytes(data=archive, mime_type="application/zip")
    )
    response = {
        "status": "success",
        "project": bp.name,
        "root_agent": bp.root_agent_name,
        "deployment_target": bp.deployment.target,
        "artifact": {"filename": artifact, "version": version, "bytes": len(archive)},
        "rendered_files": result.files_written,
        "file_count": len(files),
        "warnings": result.warnings,
        "next_steps": [f"unzip {artifact}", *result.next_steps()],
    }
    if keep:
        response["local_path"] = str(result.project_dir)
    if BUILD_OUTPUT_BUCKET:
        try:
            response["gcs_uri"] = _upload_to_gcs(BUILD_OUTPUT_BUCKET, bp.name, archive)
        except Exception as e:  # the artifact is still there; report and go on
            response["warnings"].append(f"GCS upload to {BUILD_OUTPUT_BUCKET} failed: {e}")
    return response


def inline_text_attachments(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> None:
    """Shows attached YAML files to the model as text.

    Browsers upload .yml files with MIME types Gemini rejects as inline data
    (application/x-yaml, or none at all). Rewriting them as text parts lets the
    model read the blueprint; the tools still read the original attachment.
    """
    for i, content in enumerate(llm_request.contents):
        parts = content.parts or []
        texts = [_attachment_text(part) for part in parts]
        if all(text is None for text in texts):
            continue
        # Replace the Content rather than editing it in place: it may be the
        # session's own event object, which the tools read the file from.
        llm_request.contents[i] = types.Content(
            role=content.role,
            parts=[
                part
                if text is None
                else types.Part.from_text(
                    text=f"[Attached file {part.inline_data.display_name or 'blueprint'}]\n"
                    f"```yaml\n{text}\n```"
                )
                for part, text in zip(parts, texts, strict=True)
            ],
        )


INSTRUCTION = f"""\
You are agent-builder. You turn blueprints - YAML specs in the
`{API_VERSION}` format - into ready-to-deploy ADK agent projects, using
agents-cli under the hood.

Tools:
- get_blueprint_reference: the annotated example and JSON schema. Call it
  before drafting or repairing a blueprint rather than guessing field names.
- validate_blueprint: check a blueprint and summarize it. Builds nothing.
- build_agent: validate + generate the project, returned as a zip artifact.

How to work:
1. The user gives a complete blueprint (pasted or attached) and asks to build
   or generate it: call build_agent right away. If they only ask you to check
   or review it, call validate_blueprint instead.
2. The user describes an agent in plain language: call
   get_blueprint_reference, draft the blueprint, check it with
   validate_blueprint, and show it in a ```yaml block. Build only when they
   ask you to.
3. For an attached file, pass an empty string as blueprint_yaml - the tools
   read the attachment directly. Otherwise pass the complete YAML, never an
   excerpt.
4. When a tool returns errors, list them exactly as given and propose the
   fix. Never claim a build succeeded unless build_agent returned
   status "success".
5. After a successful build, report the artifact filename, the rendered
   files, any warnings, and the next steps build_agent returned.

Keep blueprints minimal: only set fields the user asked for, and include at
least one eval case per tool with expected_tool_calls.
"""

root_agent = Agent(
    name="agent_builder",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description="Builds ADK agent projects from blueprint.yml specs using agents-cli.",
    instruction=INSTRUCTION,
    tools=[get_blueprint_reference, validate_blueprint, build_agent],
    before_model_callback=inline_text_attachments,
    generate_content_config=types.GenerateContentConfig(temperature=0.1),
)

app = App(
    root_agent=root_agent,
    name="app",
)
