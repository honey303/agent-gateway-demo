#!/usr/bin/env python3
"""Switches an already-deployed Agent Engine (Agent Runtime) resource to use
Agent Identity and route both its inbound (client) and outbound (tool) calls
through Agent Gateway.

Run this AFTER the base `adk deploy agent_engine` deploy (see README).

Sequence to route BOTH directions through Agent Gateway:
  1. adk deploy agent_engine ...                         -> get RESOURCE_NAME
  2. python deploy/enable_agent_gateway.py                -> sets identity_type
     (RESOURCE_NAME=... ORGANIZATION_ID=... this script)     only; prints the
     Agent Identity principal to use below.
  3. deploy/register_mcp_server.sh                        -> registers the MCP
     server with Agent Registry (egress only - ingress doesn't use it).
  4. AGENT_PRINCIPAL=<from step 2> deploy/setup_agent_gateway.sh
                                                            -> creates the
     EGRESS gateway + IAM binding; prints its resource name.
  5. deploy/setup_ingress_gateway.sh                       -> creates the
     INGRESS gateway (same project+region as the agent); prints its
     resource name.
  6. EGRESS_AGENT_GATEWAY_RESOURCE_NAME=<from step 4> \\
     INGRESS_AGENT_GATEWAY_RESOURCE_NAME=<from step 5> \\
       python deploy/enable_agent_gateway.py                -> wires both.

Egress only, or ingress only, both work too - just set the one env var you
have and leave the other unset; re-run again later once you have both.

Required env vars:
  PROJECT_ID, LOCATION, RESOURCE_NAME (the reasoningEngines/<id> resource
  returned by `adk deploy agent_engine`)

Optional env vars:
  ORGANIZATION_ID                      - used only to print the Agent
                                          Identity principal
  EGRESS_AGENT_GATEWAY_RESOURCE_NAME    - if set, routes the agent's
                                          outbound tool calls through this
                                          gateway (Agent-to-Anywhere)
  INGRESS_AGENT_GATEWAY_RESOURCE_NAME   - if set, routes inbound client
                                          calls to the agent through this
                                          gateway (Client-to-Agent)
"""

import os
import sys

import vertexai

PROJECT_ID = os.environ.get("PROJECT_ID") or sys.exit("Set PROJECT_ID")
LOCATION = os.environ.get("LOCATION", "us-central1")
RESOURCE_NAME = os.environ.get("RESOURCE_NAME") or sys.exit(
    "Set RESOURCE_NAME to the deployed agent's resource name, e.g. "
    "projects/123/locations/us-central1/reasoningEngines/456"
)
EGRESS_AGENT_GATEWAY_RESOURCE_NAME = os.environ.get("EGRESS_AGENT_GATEWAY_RESOURCE_NAME")
INGRESS_AGENT_GATEWAY_RESOURCE_NAME = os.environ.get("INGRESS_AGENT_GATEWAY_RESOURCE_NAME")
ORGANIZATION_ID = os.environ.get("ORGANIZATION_ID")

config = {"identity_type": "AGENT_IDENTITY"}
agent_gateway_config = {}
if EGRESS_AGENT_GATEWAY_RESOURCE_NAME:
    agent_gateway_config["agent_to_anywhere_config"] = {
        "agent_gateway": EGRESS_AGENT_GATEWAY_RESOURCE_NAME
    }
if INGRESS_AGENT_GATEWAY_RESOURCE_NAME:
    agent_gateway_config["client_to_agent_config"] = {
        "agent_gateway": INGRESS_AGENT_GATEWAY_RESOURCE_NAME
    }
if agent_gateway_config:
    config["agent_gateway_config"] = agent_gateway_config

client = vertexai.Client(project=PROJECT_ID, location=LOCATION)
agent_engine = client.agent_engines.update(name=RESOURCE_NAME, config=config)

engine_id = RESOURCE_NAME.rstrip("/").rsplit("/", 1)[-1]
project_number = RESOURCE_NAME.split("/")[1]

print(f"Updated: {agent_engine.api_resource.name}")
print("  identity_type = AGENT_IDENTITY")
if EGRESS_AGENT_GATEWAY_RESOURCE_NAME:
    print(f"  egress (Agent-to-Anywhere)  = {EGRESS_AGENT_GATEWAY_RESOURCE_NAME}")
else:
    print(
        "  egress not set yet - re-run with EGRESS_AGENT_GATEWAY_RESOURCE_NAME "
        "once that gateway exists (see setup_agent_gateway.sh)."
    )
if INGRESS_AGENT_GATEWAY_RESOURCE_NAME:
    print(f"  ingress (Client-to-Agent)   = {INGRESS_AGENT_GATEWAY_RESOURCE_NAME}")
else:
    print(
        "  ingress not set yet - re-run with INGRESS_AGENT_GATEWAY_RESOURCE_NAME "
        "once that gateway exists (see setup_ingress_gateway.sh)."
    )

if ORGANIZATION_ID:
    principal = (
        f"principal://agents.global.org-{ORGANIZATION_ID}.system.id.goog/"
        f"resources/aiplatform/projects/{project_number}/locations/{LOCATION}/"
        f"reasoningEngines/{engine_id}"
    )
    print(f"\nAgent Identity principal (grant this roles/iap.egressor):\n  {principal}")
else:
    print(
        "\nSet ORGANIZATION_ID and re-run to print this agent's Agent Identity "
        "principal (needed for setup_agent_gateway.sh, egress only - ingress "
        "needs no extra IAM binding by default). You can also read it off the "
        "Agent Engine's detail page in the Google Cloud console."
    )
