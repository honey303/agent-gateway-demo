#!/usr/bin/env bash
# Grants the agent's Agent Identity roles/iap.egressor on the Google API
# endpoints that Agent Runtime itself needs.
#
# Why this is needed: an Agent-to-Anywhere gateway is default-deny and
# intercepts ALL of the agent's outbound traffic - not just your tool calls,
# but the platform's own calls (Sessions API, telemetry, token minting).
# Bind a gateway without allowlisting these and every invocation fails with
# "Egress request is not authorized", including create_session.
#
# Hostname matching is exact: no wildcards, and each regional and mTLS
# variant is a separate endpoint that must be granted separately.
#
# Usage:
#   PROJECT_ID=my-project AGENT_PRINCIPAL='principal://...' \
#     ./deploy/gateway/allowlist_essential_apis.sh
#
# Reference (see "Allowlist essential APIs for Runtime operations"):
#   https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-gateway-runtime-deploy

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

require_agent_principal

# Hostnames from the "Essential platform endpoints" table, plus oauth2 for
# token minting. Entries absent from this registry are skipped, so listing a
# few extras is harmless.
ESSENTIAL_HOSTNAMES=(
  "${REGION}-aiplatform.googleapis.com"
  "${REGION}-aiplatform.mtls.googleapis.com"
  "aiplatform.googleapis.com"
  "aiplatform.mtls.googleapis.com"
  "${REGION}-agentregistry.googleapis.com"
  "${REGION}-agentregistry.mtls.googleapis.com"
  "agentregistry.googleapis.com"
  "oauth2.googleapis.com"
  "oauth2.mtls.googleapis.com"
  "logging.googleapis.com"
  "logging.mtls.googleapis.com"
  "monitoring.googleapis.com"
  "monitoring.mtls.googleapis.com"
  "telemetry.googleapis.com"
  "telemetry.mtls.googleapis.com"
  "trace.googleapis.com"
  "trace.mtls.googleapis.com"
  "cloudtrace.googleapis.com"
  "cloudtrace.mtls.googleapis.com"
  "cloudresourcemanager.googleapis.com"
  "cloudresourcemanager.mtls.googleapis.com"
  "iamcredentials.googleapis.com"
  "iamcredentials.mtls.googleapis.com"
  "secretmanager.googleapis.com"
  "secretmanager.mtls.googleapis.com"
)

step "Listing Agent Registry endpoints in ${PROJECT_ID}/${REGION}"
# "<endpoint-id> <hostname>" per line; IAM is keyed on the id, not the hostname.
ENDPOINTS="$(gcloud agent-registry endpoints list \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --format="value(name.basename(),displayName)")"

granted=0
skipped=0
for hostname in "${ESSENTIAL_HOSTNAMES[@]}"; do
  endpoint_id="$(awk -v want="${hostname}" '$2 == want {print $1; exit}' <<<"${ENDPOINTS}")"
  if [[ -z "${endpoint_id}" ]]; then
    dim "skip  ${hostname} (not registered)"
    skipped=$((skipped + 1))
    continue
  fi
  iap_binding add "--endpoint=${endpoint_id}"
  info "grant ${hostname}"
  granted=$((granted + 1))
done

step "Granted roles/iap.egressor on ${granted} endpoint(s); skipped ${skipped}"
info "Tool calls need their own grant on the MCP server entry -"
info "see gateway/setup_egress_gateway.sh."
