#!/usr/bin/env bash
# Builds and deploys the demo MCP server (mcp_server/) to Cloud Run.
#
# Usage:
#   PROJECT_ID=my-project REGION=us-central1 ./deploy/deploy_mcp_server.sh
#
# Prints the deployed service URL at the end. Feed that URL (with /mcp
# appended) into mcp_agent/.env as MCP_SERVER_URL before deploying the agent.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to your GCP project id}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-mcp-demo-server}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

gcloud run deploy "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --source="${REPO_ROOT}/mcp_server" \
  --allow-unauthenticated \
  --no-cpu-throttling

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format='value(status.url)')"

echo
echo "MCP server deployed at: ${SERVICE_URL}"
echo "Set this in mcp_agent/.env:"
echo "  MCP_SERVER_URL=${SERVICE_URL}/mcp"
