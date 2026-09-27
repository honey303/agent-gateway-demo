"""agent-builder CLI: blueprint.yml in, agents-cli project out. No LLM involved.

    agent-builder validate blueprint.yml
    agent-builder build blueprint.yml [-o OUTPUT_DIR] [--force] [--zip PATH] [--no-lock]
    agent-builder schema
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.builder.blueprint import (
    Blueprint,
    BlueprintError,
    load_blueprint,
    warnings_for,
)
from app.builder.generator import BuildError, build_project, zip_project


def _load(path: str) -> Blueprint | None:
    try:
        return load_blueprint(path)
    except FileNotFoundError:
        print(f"error: {path} not found", file=sys.stderr)
    except BlueprintError as e:
        print(f"error: {path} is not a valid blueprint:", file=sys.stderr)
        for err in e.errors:
            print(f"  - {err}", file=sys.stderr)
    return None


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)


def cmd_validate(args: argparse.Namespace) -> int:
    bp = _load(args.blueprint)
    if bp is None:
        return 1
    _print_warnings(warnings_for(bp))
    agents = ", ".join(a.name or bp.root_agent_name for a in bp.agents())
    print(f"ok: {bp.name} ({bp.deployment.target}) - agents: {agents}; {len(bp.evals)} eval case(s)")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    bp = _load(args.blueprint)
    if bp is None:
        return 1
    try:
        result = build_project(bp, args.output_dir, overwrite=args.force, lock=not args.no_lock)
    except (BuildError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print_warnings(result.warnings)
    print(f"Built {result.project_dir}")
    for rel in result.files_written:
        print(f"  rendered {rel}")
    if args.zip:
        Path(args.zip).write_bytes(zip_project(result.project_dir))
        print(f"Packaged {args.zip}")
    print("\nNext:")
    for step in result.next_steps():
        print(f"  {step}")
    return 0


def cmd_schema(_: argparse.Namespace) -> int:
    print(json.dumps(Blueprint.model_json_schema(by_alias=True), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-builder", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="validate a blueprint")
    p.add_argument("blueprint")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("build", help="generate an agents-cli project from a blueprint")
    p.add_argument("blueprint")
    p.add_argument("-o", "--output-dir", default=".", help="parent directory for the project (default: .)")
    p.add_argument("--force", action="store_true", help="replace an existing project directory")
    p.add_argument("--zip", metavar="PATH", help="also write the project as a zip")
    p.add_argument("--no-lock", action="store_true", help="skip `uv lock` after adding dependencies")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("schema", help="print the blueprint JSON schema")
    p.set_defaults(func=cmd_schema)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
