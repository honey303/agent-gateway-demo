#!/usr/bin/env bash
# Grants the ADK agent's Agent Runtime identity permission to invoke the
# Cloud Run MCP server (roles/run.invoker), so mcp_agent/agent.py's
# ID-token auth (MCP_SERVER_AUTH=auto/on) actually authorizes.
#
# Usage (default runtime identity - no custom service_account, no
# identity_type=AGENT_IDENTITY set at deploy time):
#   PROJECT_ID=my-project ./deploy/runtime/grant_run_invoker.sh
#
# If you deployed with a custom service account, or with Agent Identity
# (the Agent Gateway path), pass it directly instead of letting this script
# guess the default one:
#   AGENT_SERVICE_ACCOUNT=my-sa@my-project.iam.gserviceaccount.com \
#     PROJECT_ID=my-project ./deploy/runtime/grant_run_invoker.sh
#
# Reference: https://cloud.google.com/run/docs/authenticating/service-to-service

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

if [[ -n "${AGENT_SERVICE_ACCOUNT:-}" ]]; then
  MEMBER="serviceAccount:${AGENT_SERVICE_ACCOUNT}"
else
  # Default Agent Runtime identity: the Vertex AI Reasoning Engine Service
  # Agent, one per project, used unless you set a custom `service_account`
  # or `identity_type=AGENT_IDENTITY` at deploy time.
  DEFAULT_SA="service-$(project_number)@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
  step "No AGENT_SERVICE_ACCOUNT set - using the default Reasoning Engine Service Agent"
  info "${DEFAULT_SA}"
  dim "If you deployed with Agent Identity or a custom service account,"
  dim "Ctrl-C and re-run with AGENT_SERVICE_ACCOUNT set instead."
  MEMBER="serviceAccount:${DEFAULT_SA}"
fi

step "Granting roles/run.invoker on '${SERVICE_NAME}'"
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --member="${MEMBER}" \
  --role=roles/run.invoker

ok "granted to ${MEMBER#serviceAccount:}"

step "Note: this alone doesn't make the service private"
info "It's still reachable by allUsers if deployed with --allow-unauthenticated."
info "To require this permission, also run:"
dim "gcloud run services remove-iam-policy-binding ${SERVICE_NAME} \\"
dim "  --project=${PROJECT_ID} --region=${REGION} \\"
dim "  --member=allUsers --role=roles/run.invoker"
