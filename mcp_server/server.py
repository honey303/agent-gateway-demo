"""A minimal MCP server exposing a couple of demo tools over Streamable HTTP.

This is deliberately dependency-free (no external APIs, no credentials) so it
can be deployed to Cloud Run and called by the ADK agent in ../mcp_agent.

Why Streamable HTTP (and not stdio)?
  ADK's agent_engines deployment pickles the agent graph to ship it to Agent
  Engine. MCPToolset configurations that hold a live stdio subprocess (i.e.
  StdioConnectionParams) embed unpicklable file handles and fail deployment
  with `TypeError: cannot pickle 'TextIOWrapper' instances`. A remote,
  HTTP-based MCP server avoids that entirely: the agent only stores a URL.
"""

import datetime
import os
import random

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="demo-tools",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8080)),
    # Stateless mode means each request is independent - simplest to run
    # behind Cloud Run's load balancer, which may route requests to any
    # instance/replica.
    stateless_http=True,
)


@mcp.tool()
def roll_dice(sides: int = 6) -> int:
    """Roll a die with the given number of sides and return the result."""
    return random.randint(1, sides)


@mcp.tool()
def get_server_time() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@mcp.tool()
def word_count(text: str) -> int:
    """Count the number of whitespace-separated words in `text`."""
    return len(text.split())


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
