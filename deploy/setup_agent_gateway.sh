#!/usr/bin/env bash
# Creates a Google-managed Agent Gateway (egress / "Agent-to-Anywhere" mode)
# bound to the Agent Registry, then grants the agent's Agent Identity
# permission to route traffic through it to the registered MCP server.
#
# Run this AFTER register_mcp_server.sh and after you know the agent's
# Agent Identity principal (printed by enable_agent_gateway.py).
#
# Usage:
#   PROJECT_ID=my-project REGION=us-central1 AGENT_PRINCIPAL='principal://...' \
#     ./deploy/setup_agent_gateway.sh
#
# References:
#   https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/set-up-agent-gateway
#   https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/policies/configure-iam-policies
# NOTE: this is a newly announced (2026) surface; command/flag names may have
# moved since. Cross-check against the docs above if a command below fails.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to your GCP project id}"
REGION="${REGION:-us-central1}"
AGW_NAME="${AGW_NAME:-mcp-agent-gateway}"
SERVICE_NAME="${SERVICE_NAME:-mcp-demo-server}"
AGENT_PRINCIPAL="${AGENT_PRINCIPAL:?Set AGENT_PRINCIPAL to the agent Agent Identity principal (see enable_agent_gateway.py output)}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Rendering Agent Gateway config..."
AGW_NAME="${AGW_NAME}" PROJECT_ID="${PROJECT_ID}" REGION="${REGION}" \
  envsubst < "${REPO_ROOT}/deploy/agent_gateway.yaml.tmpl" > "${REPO_ROOT}/deploy/agent_gateway.yaml"
cat "${REPO_ROOT}/deploy/agent_gateway.yaml"

echo
echo "Creating Agent Gateway '${AGW_NAME}'..."
gcloud network-services agent-gateways import "${AGW_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --source="${REPO_ROOT}/deploy/agent_gateway.yaml"

echo
echo "Granting the agent's identity roles/iap.egressor on the registered MCP service..."
gcloud iap web add-iam-policy-binding \
  --resource-type=agent-registry \
  --endpoint="${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --member="${AGENT_PRINCIPAL}" \
  --role=roles/iap.egressor

echo
echo "Egress Agent Gateway resource name (pass this to enable_agent_gateway.py as EGRESS_AGENT_GATEWAY_RESOURCE_NAME):"
echo "  projects/${PROJECT_ID}/locations/${REGION}/agentGateways/${AGW_NAME}"
