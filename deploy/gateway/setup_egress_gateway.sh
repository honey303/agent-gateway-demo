#!/usr/bin/env bash
# Creates a Google-managed Agent Gateway (egress / "Agent-to-Anywhere" mode)
# bound to the Agent Registry, then grants the agent's Agent Identity
# permission to route traffic through it to the registered MCP server.
#
# Run AFTER register_mcp_server.sh and after you know the agent's Agent
# Identity principal (printed by enable_agent_gateway.py).
#
# Usage:
#   PROJECT_ID=my-project AGENT_PRINCIPAL='principal://...' \
#     ./deploy/gateway/setup_egress_gateway.sh
#
# References:
#   https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/set-up-agent-gateway
#   https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/policies/configure-iam-policies

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

require_agent_principal
AGW_NAME="${AGW_NAME:-${EGRESS_AGW_NAME}}"

step "Rendering the egress gateway config"
CONFIG="$(AGW_NAME="${AGW_NAME}" render_template egress_gateway.yaml.tmpl)"
sed 's/^/  /' "${CONFIG}"

step "Creating Agent Gateway '${AGW_NAME}'"
gcloud network-services agent-gateways import "${AGW_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --source="${CONFIG}"
rm -f "${CONFIG}"

step "Looking up the Agent Registry MCP server entry for '${SERVICE_NAME}'"
MCP_SERVER_ID="$(require_mcp_server_id)"
ok "${MCP_SERVER_ID}"

step "Granting roles/iap.egressor on the registered MCP service"
iap_binding add "--mcp-server=${MCP_SERVER_ID}"
ok "granted to ${AGENT_PRINCIPAL}"

step "Egress gateway resource name"
info "Pass this to enable_agent_gateway.py as EGRESS_AGENT_GATEWAY_RESOURCE_NAME:"
dim "$(gateway_path "${AGW_NAME}")"
