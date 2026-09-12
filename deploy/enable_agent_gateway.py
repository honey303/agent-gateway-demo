#!/usr/bin/env python3
"""Switches an already-deployed Agent Engine (Agent Runtime) resource to use
Agent Identity and route its outbound tool calls through Agent Gateway.

Run this AFTER the base `adk deploy agent_engine` deploy (see README) and
AFTER register_mcp_server.sh, then feed the printed Agent Identity principal
into setup_agent_gateway.sh, and finally re-run this script with
AGENT_GATEWAY_RESOURCE_NAME set to wire the two together.

Sequence:
  1. adk deploy agent_engine ...                       -> get RESOURCE_NAME
  2. python deploy/enable_agent_gateway.py              -> sets identity_type
     (RESOURCE_NAME=... ORGANIZATION_ID=... this script) only; prints the
     Agent Identity principal to use below.
  3. deploy/register_mcp_server.sh                      -> registers the MCP
     server with Agent Registry.
  4. AGENT_PRINCIPAL=<from step 2> deploy/setup_agent_gateway.sh
                                                          -> creates the
     gateway + IAM binding; prints AGENT_GATEWAY_RESOURCE_NAME.
  5. AGENT_GATEWAY_RESOURCE_NAME=<from step 4> python deploy/enable_agent_gateway.py
                                                          -> points the agent
     at the gateway for egress.

Required env vars:
  PROJECT_ID, LOCATION, RESOURCE_NAME (the reasoningEngines/<id> resource
  returned by `adk deploy agent_engine`)

Optional env vars:
  ORGANIZATION_ID          - used only to print the Agent Identity principal
  AGENT_GATEWAY_RESOURCE_NAME - if set, wires egress through this gateway
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
AGENT_GATEWAY_RESOURCE_NAME = os.environ.get("AGENT_GATEWAY_RESOURCE_NAME")
ORGANIZATION_ID = os.environ.get("ORGANIZATION_ID")

config = {"identity_type": "AGENT_IDENTITY"}
if AGENT_GATEWAY_RESOURCE_NAME:
    config["agent_gateway_config"] = {
        "agent_to_anywhere_config": {"agent_gateway": AGENT_GATEWAY_RESOURCE_NAME}
    }

client = vertexai.Client(project=PROJECT_ID, location=LOCATION)
agent_engine = client.agent_engines.update(name=RESOURCE_NAME, config=config)

engine_id = RESOURCE_NAME.rstrip("/").rsplit("/", 1)[-1]
project_number = RESOURCE_NAME.split("/")[1]

print(f"Updated: {agent_engine.api_resource.name}")
print(f"  identity_type = AGENT_IDENTITY")
if AGENT_GATEWAY_RESOURCE_NAME:
    print(f"  agent_gateway = {AGENT_GATEWAY_RESOURCE_NAME}")
else:
    print(
        "  agent_gateway_config not set yet - re-run this script with "
        "AGENT_GATEWAY_RESOURCE_NAME once the gateway exists (see "
        "setup_agent_gateway.sh)."
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
        "principal (needed for setup_agent_gateway.sh). You can also read it "
        "off the Agent Engine's detail page in the Google Cloud console."
    )
