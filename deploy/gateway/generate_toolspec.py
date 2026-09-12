#!/usr/bin/env python3
"""Generates a toolspec.json for Agent Registry from a live MCP server.

Agent Registry's `--mcp-server-spec-content` flag expects a JSON object with
a single `tools` field, containing exactly what the MCP server returns from
a standard `tools/list` request. This script connects to the MCP server over
Streamable HTTP and dumps that.

Usage:
    python deploy/gateway/generate_toolspec.py https://<mcp-server>/mcp > toolspec.json
"""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def generate(url: str) -> dict:
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return {
                "tools": [
                    tool.model_dump(mode="json", exclude_none=True)
                    for tool in result.tools
                ]
            }


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <mcp-server-url>", file=sys.stderr)
        sys.exit(1)

    payload = asyncio.run(generate(sys.argv[1]))
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
