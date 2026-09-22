#!/usr/bin/env bash
# Builds and deploys the demo MCP server (mcp_server/) to Cloud Run.
#
# Usage:
#   PROJECT_ID=my-project ./deploy/runtime/deploy_mcp_server.sh
#
# Prints the deployed service URL at the end. Feed that URL (with /mcp
# appended) into mcp_agent/.env as MCP_SERVER_URL before deploying the agent.

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

step "Deploying '${SERVICE_NAME}' to Cloud Run"
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

step "Deployed at ${SERVICE_URL}"
info "Set this in mcp_agent/.env:"
dim "MCP_SERVER_URL=${SERVICE_URL}/mcp"
