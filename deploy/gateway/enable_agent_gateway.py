#!/usr/bin/env python3
"""Switches an already-deployed Agent Engine (Agent Runtime) resource to use
Agent Identity and route both its inbound (client) and outbound (tool) calls
through Agent Gateway.

Run this AFTER the base `adk deploy agent_engine` deploy (see README).

Sequence to route BOTH directions through Agent Gateway:

  1. runtime/deploy_agent.sh                -> gives you RESOURCE_NAME
  2. gateway/enable_agent_gateway.py        -> sets identity_type only, and
     (with ORGANIZATION_ID set)                prints the Agent Identity
                                               principal used below
  3. gateway/register_mcp_server.sh         -> registers the MCP server with
                                               Agent Registry (egress only)
  4. gateway/setup_egress_gateway.sh        -> creates the EGRESS gateway and
     (AGENT_PRINCIPAL=<from step 2>)           its IAM binding
  5. gateway/setup_ingress_gateway.sh       -> creates the INGRESS gateway
                                               (same project+region as agent)
  6. gateway/enable_agent_gateway.py again  -> wires both, with
     EGRESS_AGENT_GATEWAY_RESOURCE_NAME and INGRESS_AGENT_GATEWAY_RESOURCE_NAME

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

Both gateway env vars take an *Agent Gateway* resource name
(projects/<p>/locations/<l>/agentGateways/<name>) - NOT the agent's own
reasoningEngines/... name.

Implementation note: this issues the PATCH against the Agent Engine REST API
directly instead of going through `client.agent_engines.update()`. The SDK
rejects any update that touches `spec.deployment_spec` (which is where
`agent_gateway_config` lives) unless you also re-supply the agent object or
its source packages - i.e. it would force a full redeploy just to flip a
routing field. The REST call below only sends the fields named in
`updateMask`, so the already-deployed code, env vars, etc. are left alone.
"""

import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from agentclient import API_VERSION, authorized_session, config  # noqa: E402

ENGINE_NAME_RE = re.compile(r"^projects/[^/]+/locations/[^/]+/reasoningEngines/[^/]+$")
GATEWAY_NAME_RE = re.compile(r"^projects/[^/]+/locations/[^/]+/agentGateways/[^/]+$")

PROJECT_ID = config.project_id
LOCATION = config.location
RESOURCE_NAME = config.resource_name
EGRESS_AGENT_GATEWAY_RESOURCE_NAME = os.environ.get("EGRESS_AGENT_GATEWAY_RESOURCE_NAME")
INGRESS_AGENT_GATEWAY_RESOURCE_NAME = os.environ.get("INGRESS_AGENT_GATEWAY_RESOURCE_NAME")
ORGANIZATION_ID = os.environ.get("ORGANIZATION_ID")

RESOURCE_NAME = RESOURCE_NAME.strip().rstrip("/")
if not ENGINE_NAME_RE.match(RESOURCE_NAME):
    sys.exit(
        f"RESOURCE_NAME must look like "
        f"projects/<project>/locations/<location>/reasoningEngines/<id>, got:\n"
        f"  {RESOURCE_NAME}"
    )


def _check_gateway(var_name: str, value: str) -> str:
    value = value.strip().rstrip("/")
    if not GATEWAY_NAME_RE.match(value):
        hint = ""
        if "/reasoningEngines/" in value:
            hint = (
                "\nThat's an Agent Engine (the agent itself), not a gateway. "
                "The gateway name is printed by setup_egress_gateway.sh / "
                "setup_ingress_gateway.sh, or list them with:\n"
                f"  gcloud network-services agent-gateways list --location={LOCATION}"
            )
        sys.exit(
            f"{var_name} must look like "
            f"projects/<project>/locations/<location>/agentGateways/<name>, got:\n"
            f"  {value}{hint}"
        )
    return value


spec = {"identityType": "AGENT_IDENTITY"}
update_masks = ["spec.identity_type"]

agent_gateway_config = {}
if EGRESS_AGENT_GATEWAY_RESOURCE_NAME:
    EGRESS_AGENT_GATEWAY_RESOURCE_NAME = _check_gateway(
        "EGRESS_AGENT_GATEWAY_RESOURCE_NAME", EGRESS_AGENT_GATEWAY_RESOURCE_NAME
    )
    agent_gateway_config["agentToAnywhereConfig"] = {
        "agentGateway": EGRESS_AGENT_GATEWAY_RESOURCE_NAME
    }
if INGRESS_AGENT_GATEWAY_RESOURCE_NAME:
    INGRESS_AGENT_GATEWAY_RESOURCE_NAME = _check_gateway(
        "INGRESS_AGENT_GATEWAY_RESOURCE_NAME", INGRESS_AGENT_GATEWAY_RESOURCE_NAME
    )
    agent_gateway_config["clientToAgentConfig"] = {
        "agentGateway": INGRESS_AGENT_GATEWAY_RESOURCE_NAME
    }
if agent_gateway_config:
    # Only the masked leaf is replaced, so setting just one direction here
    # would drop the other one. Re-state both whenever both are known.
    spec["deploymentSpec"] = {"agentGatewayConfig": agent_gateway_config}
    update_masks.append("spec.deployment_spec.agent_gateway_config")

session = authorized_session()
base_url = config.base_url


def _raise_for_status(response: requests.Response) -> dict:
    if not response.ok:
        sys.exit(f"{response.request.method} {response.url} failed:\n{response.text}")
    return response.json() if response.content else {}


response = _raise_for_status(
    session.patch(
        f"{base_url}/{RESOURCE_NAME}",
        params={"updateMask": ",".join(update_masks)},
        data=json.dumps({"spec": spec}),
    )
)

# The PATCH returns a long-running operation; wait for it so that a failure
# server-side surfaces here rather than silently later.
operation_name = response.get("name", "")
if "/operations/" in operation_name:
    deadline = time.time() + 600
    while not response.get("done"):
        if time.time() > deadline:
            sys.exit(f"Timed out waiting for operation {operation_name}")
        time.sleep(5)
        response = _raise_for_status(session.get(f"{base_url}/{operation_name}"))
if response.get("error"):
    sys.exit(f"Update failed: {json.dumps(response['error'], indent=2)}")

engine_id = RESOURCE_NAME.rsplit("/", 1)[-1]
project_number = RESOURCE_NAME.split("/")[1]

print(f"Updated: {RESOURCE_NAME}")
print("  identity_type = AGENT_IDENTITY")
if EGRESS_AGENT_GATEWAY_RESOURCE_NAME:
    print(f"  egress (Agent-to-Anywhere)  = {EGRESS_AGENT_GATEWAY_RESOURCE_NAME}")
else:
    print(
        "  egress not set yet - re-run with EGRESS_AGENT_GATEWAY_RESOURCE_NAME "
        "once that gateway exists (see setup_egress_gateway.sh)."
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
        "principal (needed for setup_egress_gateway.sh, egress only - ingress "
        "needs no extra IAM binding by default). You can also read it off the "
        "Agent Engine's detail page in the Google Cloud console."
    )
