#!/usr/bin/env bash
# Registers the deployed MCP server (mcp_server/, on Cloud Run) with Agent
# Registry so Agent Gateway can route to it. Run this after
# deploy_mcp_server.sh.
#
# Usage:
#   PROJECT_ID=my-project REGION=us-central1 MCP_URL=https://mcp-demo-server-xxx.run.app/mcp \
#     ./deploy/register_mcp_server.sh
#
# Reference: https://docs.cloud.google.com/agent-registry/register-mcp-servers
# NOTE: `agent-registry` is a new (2026) gcloud surface, currently under the
# `alpha` track; check `gcloud alpha agent-registry services create --help`
# against the docs above if this fails - flags may have changed.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to your GCP project id}"
REGION="${REGION:-us-central1}"
MCP_URL="${MCP_URL:?Set MCP_URL to the deployed MCP server's /mcp endpoint}"
SERVICE_NAME="${SERVICE_NAME:-mcp-demo-server}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLSPEC_PATH="${REPO_ROOT}/deploy/toolspec.json"

echo "Introspecting ${MCP_URL} to build toolspec.json..."
python3 "${REPO_ROOT}/deploy/generate_toolspec.py" "${MCP_URL}" > "${TOOLSPEC_PATH}"
cat "${TOOLSPEC_PATH}"

echo
echo "Registering '${SERVICE_NAME}' with Agent Registry..."
gcloud alpha agent-registry services create "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --display-name="MCP Demo Server" \
  --mcp-server-spec-type=tool-spec \
  --mcp-server-spec-content="@${TOOLSPEC_PATH}" \
  --interfaces="url=${MCP_URL},protocolBinding=JSONRPC"

echo
echo "Registered. Resource name:"
echo "  //agentregistry.googleapis.com/projects/${PROJECT_ID}/locations/${REGION}/services/${SERVICE_NAME}"
