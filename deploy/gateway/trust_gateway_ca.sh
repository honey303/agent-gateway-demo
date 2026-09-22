#!/usr/bin/env bash
# Copies the egress Agent Gateway's TLS-inspection CA into mcp_agent/.env as
# AGENT_GATEWAY_CA_B64, so the deployed agent can trust it.
#
# Why this is needed: an Agent-to-Anywhere gateway decrypts and re-signs the
# agent's outbound TLS. Source-based deploys are documented to get the
# gateway's CA baked in automatically "during image creation" - but that only
# applies to images built while the gateway is already attached. Attach a
# gateway to an already-deployed engine (which is what enable_agent_gateway.py
# does) and the running image has no idea about the CA, so every outbound
# HTTPS call dies with CERTIFICATE_VERIFY_FAILED - including ADK's own calls
# to the managed session service, which makes even create_session fail.
#
# mcp_agent/agent.py reads AGENT_GATEWAY_CA_B64 at startup and appends the
# certificate to certifi's bundle. It's base64 on a single line because
# `adk deploy agent_engine` ships .env values as container env vars, and a
# multi-line PEM doesn't survive that.
#
# Usage:
#   PROJECT_ID=my-project AGW_NAME=my-egress-gateway \
#     ./deploy/gateway/trust_gateway_ca.sh
#
# Then redeploy so the agent picks it up:
#   PROJECT_ID=... AGENT_ENGINE_ID=... ./deploy/runtime/deploy_agent.sh

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

AGW_NAME="${AGW_NAME:-${EGRESS_AGW_NAME}}"

[[ -f "${ENV_FILE}" ]] || die \
  "Missing ${ENV_FILE}. Run: cp mcp_agent/.env.example mcp_agent/.env, fill it in, then retry."

step "Reading root certificates from gateway '${AGW_NAME}'"
CERTS="$(gcloud network-services agent-gateways describe "${AGW_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --format="value[delimiter=\n](agentGatewayCard.rootCertificates)")"

grep -q "BEGIN CERTIFICATE" <<<"${CERTS}" || die \
  "No root certificates on '${AGW_NAME}'. Is it a Google-managed gateway?
  Check: gcloud network-services agent-gateways describe ${AGW_NAME} --location=${REGION}"

ok "found $(grep -c "BEGIN CERTIFICATE" <<<"${CERTS}") certificate(s)"

# -w0 keeps it on one line; macOS base64 has no -w, hence the fallback.
ENCODED="$(base64 -w0 <<<"${CERTS}" 2>/dev/null || base64 <<<"${CERTS}" | tr -d '\n')"
env_set AGENT_GATEWAY_CA_B64 "${ENCODED}"

step "Wrote AGENT_GATEWAY_CA_B64 to ${ENV_FILE}"
info "Now redeploy so the agent picks it up:"
dim "PROJECT_ID=${PROJECT_ID} AGENT_ENGINE_ID=<id> ./deploy/runtime/deploy_agent.sh"
