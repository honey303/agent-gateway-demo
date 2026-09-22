#!/usr/bin/env bash
# Negative test: proves the egress gateway actually enforces authorization.
#
# Temporarily revokes the agent's roles/iap.egressor on the Agent Registry MCP
# server entry, runs the smoke test (which should now FAIL at the tool call),
# shows the DENIED entry in the gateway logs, and restores the binding.
#
# This is the demo that proves governance is real: a passing smoke test only
# shows traffic flows, not that anything is being enforced. Here the agent
# keeps working - sessions, model calls - and loses exactly one capability:
# the MCP tool it is no longer authorized to reach.
#
# The binding is restored by an EXIT trap, so it comes back even if the smoke
# test fails unexpectedly or you Ctrl-C.
#
# Usage:
#   PROJECT_ID=my-project AGENT_PRINCIPAL='principal://...' \
#     ./deploy/tests/test_egress_denied.sh

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

require_agent_principal
# The API applies the revoke immediately, but the gateway data plane caches
# the old policy for a bit (~30s observed), so poll instead of guessing.
POLL_SECONDS="${POLL_SECONDS:-30}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-6}"

step "Looking up the Agent Registry MCP server entry for '${SERVICE_NAME}'"
MCP_SERVER_ID="$(require_mcp_server_id)"
ok "${MCP_SERVER_ID}"

restore_binding() {
  step "Restoring roles/iap.egressor"
  iap_binding add "--mcp-server=${MCP_SERVER_ID}"
  ok "restored (the gateway needs ~${POLL_SECONDS}s to pick it back up)"
}
trap restore_binding EXIT

step "Revoking roles/iap.egressor from the agent"
iap_binding remove "--mcp-server=${MCP_SERVER_ID}"
ok "revoked"

step "Waiting for the revoke to reach the gateway, then testing"
info "The API applies it instantly, but the data plane lags by roughly half a"
info "minute - so poll rather than guess a delay."

START_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DENIED=0

for attempt in $(seq 1 "${MAX_ATTEMPTS}"); do
  sleep "${POLL_SECONDS}"
  step "attempt ${attempt}/${MAX_ATTEMPTS} ($(date -u +%H:%M:%S))"
  set +e
  OUTPUT="$(PROJECT_ID="${PROJECT_ID}" REGION="${REGION}" \
    python3 "${DEPLOY_DIR}/tests/smoke_test.py" 2>&1)"
  SMOKE_EXIT=$?
  set -e

  if [[ ${SMOKE_EXIT} -ne 0 ]]; then
    DENIED=1
    echo "${OUTPUT}" | sed -n '/Querying:/,$p'
    break
  fi
  info "still allowed - the old policy is probably still cached"
done

step "Gateway log entries for '${SERVICE_NAME}' since the revoke"
sleep 15 # log ingestion lag
gcloud logging read \
  "logName=\"projects/${PROJECT_ID}/logs/networkservices.googleapis.com%2Fgateway_requests\" AND
   jsonPayload.enforcedGatewaySecurityPolicy.hostname:\"${SERVICE_NAME}\" AND
   timestamp>=\"${START_TIME}\"" \
  --project="${PROJECT_ID}" \
  --limit=10 \
  --format="csv[no-heading](timestamp,jsonPayload.enforcedGatewaySecurityPolicy.hostname,httpRequest.status,jsonPayload.authzPolicyInfo.result)" \
  || true

if [[ ${DENIED} -eq 1 ]]; then
  step "PASS"
  info "The tool call failed while the grant was revoked - the gateway enforces"
  info "authorization rather than just forwarding traffic."
  info "Note the agent still created a session and answered: it lost exactly"
  info "the one capability it was deauthorized for."
else
  step "UNEXPECTED"
  warn "Still passing after ${MAX_ATTEMPTS} attempts. Either propagation is"
  warn "slower than expected, or the agent is reaching the MCP server without"
  warn "traversing the gateway."
fi
