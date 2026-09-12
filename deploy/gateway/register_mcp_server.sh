#!/usr/bin/env bash
# Registers the deployed MCP server (mcp_server/, on Cloud Run) with Agent
# Registry so Agent Gateway can route to it. Run after deploy_mcp_server.sh.
#
# Usage:
#   PROJECT_ID=my-project MCP_URL=https://mcp-demo-server-xxx.run.app/mcp \
#     ./deploy/gateway/register_mcp_server.sh
#
# Reference: https://docs.cloud.google.com/agent-registry/register-mcp-servers
# NOTE: `agent-registry` is a new (2026) gcloud surface, currently under the
# `alpha` track; check `gcloud alpha agent-registry services create --help`
# against the docs above if this fails - flags may have changed.

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

require_var MCP_URL "the deployed MCP server's /mcp endpoint"

# Generated, not committed: it's derived from whatever the live server exposes.
TOOLSPEC_PATH="$(mktemp "${TMPDIR:-/tmp}/toolspec-XXXXXX.json")"
trap 'rm -f "${TOOLSPEC_PATH}"' EXIT

step "Introspecting ${MCP_URL} to build the tool spec"
python3 "${DEPLOY_DIR}/gateway/generate_toolspec.py" "${MCP_URL}" >"${TOOLSPEC_PATH}"
sed 's/^/  /' "${TOOLSPEC_PATH}"

step "Registering '${SERVICE_NAME}' with Agent Registry"
# --mcp-server-spec-content takes a PATH, not curl's @path idiom.
gcloud alpha agent-registry services create "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --display-name="MCP Demo Server" \
  --mcp-server-spec-type=tool-spec \
  --mcp-server-spec-content="${TOOLSPEC_PATH}" \
  --interfaces="url=${MCP_URL},protocolBinding=JSONRPC"

step "Registered"
dim "//agentregistry.googleapis.com/projects/${PROJECT_ID}/locations/${REGION}/services/${SERVICE_NAME}"
