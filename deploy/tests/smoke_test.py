#!/usr/bin/env python3
"""End-to-end smoke test for the Agent Gateway demo.

Checks that the deployed agent is wired the way the README describes, then
actually queries it with a prompt that forces an MCP tool call - so a pass
means the full path works:

    you -> ingress gateway -> Agent Runtime -> egress gateway -> MCP server

Usage:
  PROJECT_ID=my-project python deploy/tests/smoke_test.py

RESOURCE_NAME falls back to REASONING_ENGINE_ID in mcp_agent/.env, so in
practice you usually just need PROJECT_ID.

Exit code is 0 only if the agent answered AND did so by calling an MCP tool.
An answer with no tool call is a failure: the model can invent a dice roll on
its own, which would pass a naive "did it reply?" check while the gateway
path is actually broken.

On failure the script checks the gateway's DENIED log first, then the
runtime's ERROR logs, because the API surfaces every in-agent problem as the
same opaque "Internal Server Error".
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from agentclient import (  # noqa: E402
    AgentClient, Console, config, gateway_denials, runtime_errors,
)

# Phrased to make a tool call the only way to answer correctly.
PROMPT = "Roll a 20-sided die and tell me the result. Use your tools."
EXPECTED_TOOL = "roll_dice"

# Each entry: (regex over the logs, what it means, how to fix it).
KNOWN_FAILURES = [
    (
        r"CERTIFICATE_VERIFY_FAILED|self-signed certificate",
        "The egress gateway is TLS-inspecting the agent's outbound HTTPS and "
        "the agent's container doesn't trust the gateway's CA, so every "
        "outbound call fails - including the ADK session service's own calls "
        "to aiplatform.mtls.googleapis.com.",
        "Run deploy/gateway/trust_gateway_ca.sh, then redeploy the agent.",
    ),
    (
        r"PERMISSION_DENIED|403|iap\.egressor",
        "The agent's identity was refused by the gateway when calling the MCP "
        "server.",
        "Grant roles/iap.egressor on the Agent Registry MCP server entry - "
        "see deploy/gateway/setup_egress_gateway.sh.",
    ),
    (
        r"run\.invoker|Cloud Run.*(401|403)",
        "Cloud Run rejected the MCP call.",
        "Grant the agent's runtime identity roles/run.invoker - see "
        "deploy/runtime/grant_run_invoker.sh.",
    ),
    (
        r"ModuleNotFoundError|ImportError",
        "The deployed agent failed to import.",
        "Check mcp_agent/requirements.txt, then redeploy with "
        "deploy/runtime/deploy_agent.sh.",
    ),
]


def diagnose():
    """Explains the failure from gateway denials and/or runtime error logs."""
    # Gateway entries take ~15-30s to land in Cloud Logging. Without the retry
    # this check silently reports nothing on exactly the failure it exists to
    # catch.
    denials = ""
    for attempt in range(3):
        if attempt:
            time.sleep(15)
        Console.dim("Checking the gateway for blocked requests...")
        denials = gateway_denials()
        if denials.strip():
            break

    if denials.strip():
        print(f"\n  {Console.RED}The egress gateway DENIED these requests:{Console.RESET}")
        for line in denials.strip().splitlines():
            hostname, _, status = line.partition(",")
            print(f"    {hostname} (HTTP {status})")
        print(
            f"  {Console.YELLOW}Fix:{Console.RESET} grant the agent "
            "roles/iap.egressor on the matching Agent Registry entry - "
            "gateway/setup_egress_gateway.sh for the MCP server, "
            "gateway/allowlist_essential_apis.sh for Google APIs."
        )
        return

    Console.dim("Pulling recent runtime errors to diagnose...")
    logs = runtime_errors()
    if not logs.strip():
        print("  No ERROR logs since this run started - check the agent manually.")
        return
    for pattern, meaning, fix in KNOWN_FAILURES:
        if re.search(pattern, logs):
            print(f"\n  {Console.RED}Likely cause:{Console.RESET} {meaning}")
            print(f"  {Console.YELLOW}Fix:{Console.RESET} {fix}")
            return
    print("  Unrecognized error. Last log lines:\n")
    print("\n".join(f"    {line}" for line in logs.strip().splitlines()[-15:]))


def main():
    print(f"Agent:   {config.resource_name}")
    print(f"Project: {config.project_id} ({config.location})")

    client = AgentClient(user_id_prefix="smoke-test")
    if not client.preflight():
        print("\nThe agent isn't configured as expected; continuing anyway.")

    Console.head("Creating a session")
    session_id, error = client.create_session()
    if error:
        Console.fail(f"create_session: {error}")
        diagnose()
        return 1
    Console.ok(f"session {session_id}")

    Console.head(f"Querying: {PROMPT!r}")
    tool_calls, _, answer, error = client.stream_query(session_id, PROMPT)
    if error:
        Console.fail(f"stream_query: {error}")
        diagnose()
        return 1

    Console.head("Result")
    passed = True

    if EXPECTED_TOOL in (tool_calls or []):
        Console.ok(f"MCP tool call succeeded: {EXPECTED_TOOL}")
    elif tool_calls:
        Console.fail(f"expected {EXPECTED_TOOL}, got {tool_calls}")
        passed = False
    else:
        Console.fail(
            "no MCP tool was called - the model answered from its own head, "
            "so the gateway path is broken"
        )
        passed = False

    if answer:
        Console.ok(f"agent answered: {answer[:120]!r}")
    else:
        Console.fail("agent returned no text")
        passed = False

    if passed:
        print(
            "\nEnd-to-end path works: client -> ingress gateway -> Agent "
            "Runtime -> egress gateway -> MCP server"
        )
        return 0

    diagnose()
    return 1


if __name__ == "__main__":
    sys.exit(main())
