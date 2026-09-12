#!/usr/bin/env bash
# Creates a Google-managed Agent Gateway in "Client-to-Agent" (ingress)
# mode - it governs incoming client calls to the deployed agent before they
# reach Agent Runtime.
#
# Unlike the egress gateway, this one:
#   - does NOT reference Agent Registry (the registry isn't used for ingress);
#   - MUST be in the same project AND region as the agent (egress gateways
#     may live in a different project, same region only);
#   - needs no IAM binding - but also enforces nothing until you attach a
#     CONTENT_AUTHZ policy (see setup_ingress_model_armor.sh).
#
# Usage:
#   PROJECT_ID=my-project ./deploy/gateway/setup_ingress_gateway.sh
#
# References:
#   https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/set-up-agent-gateway
#   https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-gateway-runtime-deploy

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

AGW_NAME="${AGW_NAME:-${INGRESS_AGW_NAME}}"

step "Rendering the ingress gateway config"
CONFIG="$(AGW_NAME="${AGW_NAME}" render_template ingress_gateway.yaml.tmpl)"
sed 's/^/  /' "${CONFIG}"

step "Creating ingress Agent Gateway '${AGW_NAME}'"
gcloud network-services agent-gateways import "${AGW_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --source="${CONFIG}"
rm -f "${CONFIG}"

step "Ingress gateway resource name"
info "Pass this to enable_agent_gateway.py as INGRESS_AGENT_GATEWAY_RESOURCE_NAME:"
dim "$(gateway_path "${AGW_NAME}")"
warn "This gateway enforces nothing yet - run setup_ingress_model_armor.sh next."
