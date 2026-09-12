#!/usr/bin/env bash
# Creates a Google-managed Agent Gateway in "Client-to-Agent" (ingress)
# mode - it governs incoming client calls to the deployed agent (e.g.
# reasoningEngines.query) before they reach Agent Runtime.
#
# Unlike the egress gateway (setup_agent_gateway.sh), this one:
#   - does NOT reference Agent Registry (the registry isn't used for
#     ingress traffic);
#   - MUST be created in the same project AND region as the agent (egress
#     gateways may live in a different project, same region only).
#   - needs no separate IAM binding for the basic case: once wired onto the
#     agent (see enable_agent_gateway.py), Google binds the ingress
#     gateway's policy to incoming client requests at the network edge
#     automatically. (Optional add-ons like Model Armor need their own
#     bindings - see the README.)
#
# Usage:
#   PROJECT_ID=my-project REGION=us-central1 ./deploy/setup_ingress_gateway.sh
#
# References:
#   https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/set-up-agent-gateway
#   https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-gateway-runtime-deploy
# NOTE: this is a newly announced (2026) surface; command/flag names may have
# moved since. Cross-check against the docs above if a command below fails.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to your GCP project id}"
REGION="${REGION:-us-central1}"
AGW_NAME="${AGW_NAME:-mcp-agent-gateway-ingress}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Rendering ingress Agent Gateway config..."
AGW_NAME="${AGW_NAME}" \
  envsubst < "${REPO_ROOT}/deploy/agent_gateway_ingress.yaml.tmpl" > "${REPO_ROOT}/deploy/agent_gateway_ingress.yaml"
cat "${REPO_ROOT}/deploy/agent_gateway_ingress.yaml"

echo
echo "Creating ingress Agent Gateway '${AGW_NAME}'..."
gcloud network-services agent-gateways import "${AGW_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --source="${REPO_ROOT}/deploy/agent_gateway_ingress.yaml"

echo
echo "Ingress Agent Gateway resource name (pass this to enable_agent_gateway.py"
echo "as INGRESS_AGENT_GATEWAY_RESOURCE_NAME):"
echo "  projects/${PROJECT_ID}/locations/${REGION}/agentGateways/${AGW_NAME}"
