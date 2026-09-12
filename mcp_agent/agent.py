"""A simple ADK agent that uses tools exposed by a remote MCP server.

Deployment target: Vertex AI Agent Engine ("GCP Agent Runtime"), via
    adk deploy agent_engine --project=<PROJECT> --region=<REGION> mcp_agent

The agent talks to the MCP server defined in ../mcp_server over Streamable
HTTP (MCP_SERVER_URL). Using HTTP rather than a local stdio subprocess is
required here: Agent Engine's deploy step pickles the agent, and a
stdio-based MCPToolset holds an open subprocess/pipe that cannot be
pickled (see https://github.com/google/adk-python/issues/1727). A remote
HTTP MCP server has no such handle, so it deploys cleanly.
"""

import os

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8080/mcp")

mcp_toolset = McpToolset(
    connection_params=StreamableHTTPConnectionParams(url=MCP_SERVER_URL),
)

root_agent = Agent(
    model="gemini-2.5-flash",
    name="mcp_gateway_agent",
    description="An assistant that can call tools hosted on a remote MCP server.",
    instruction=(
        "You are a helpful assistant. You have access to tools served by an "
        "external MCP server (dice rolling, the current server time, and "
        "word counting). Use them whenever they are relevant to the user's "
        "request, and otherwise answer normally."
    ),
    tools=[mcp_toolset],
)
