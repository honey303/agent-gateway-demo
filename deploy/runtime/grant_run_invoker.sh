#!/usr/bin/env bash
# Grants the ADK agent's Agent Runtime identity permission to invoke the
# Cloud Run MCP server (roles/run.invoker), so mcp_agent/agent.py's
# ID-token auth (MCP_SERVER_AUTH=auto/on) actually authorizes.
#
# Usage (default runtime identity - no custom service_account, no
# identity_type=AGENT_IDENTITY set at deploy time):
#   PROJECT_ID=my-project REGION=us-central1 ./deploy/grant_run_invoker.sh
#
# If you deployed with a custom service account, or with Agent Identity
# (the Agent Gateway path - see enable_agent_gateway.py), pass it directly
# instead of letting this script guess the default one:
#   AGENT_SERVICE_ACCOUNT=my-sa@my-project.iam.gserviceaccount.com \
#     PROJECT_ID=my-project ./deploy/grant_run_invoker.sh
#
# Reference: https://cloud.google.com/run/docs/authenticating/service-to-service

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to your GCP project id}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-mcp-demo-server}"

if [[ -n "${AGENT_SERVICE_ACCOUNT:-}" ]]; then
  MEMBER="serviceAccount:${AGENT_SERVICE_ACCOUNT}"
else
  # Default Agent Runtime identity: the Vertex AI Reasoning Engine Service
  # Agent, one per project, used unless you set a custom `service_account`
  # or `identity_type=AGENT_IDENTITY` at deploy time.
  PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
  DEFAULT_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
  echo "No AGENT_SERVICE_ACCOUNT set - using the default Reasoning Engine"
  echo "Service Agent: ${DEFAULT_SA}"
  echo "(If you deployed with Agent Identity or a custom service account,"
  echo " Ctrl-C and re-run with AGENT_SERVICE_ACCOUNT set instead.)"
  MEMBER="serviceAccount:${DEFAULT_SA}"
fi

gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --member="${MEMBER}" \
  --role=roles/run.invoker

echo
echo "Granted roles/run.invoker on '${SERVICE_NAME}' to ${MEMBER#serviceAccount:}."
echo
echo "This alone doesn't make the service private - it's still reachable by"
echo "allUsers if it was deployed with --allow-unauthenticated. To require"
echo "this permission (and stop accepting unauthenticated calls), also run:"
echo "  gcloud run services remove-iam-policy-binding ${SERVICE_NAME} \\"
echo "    --project=${PROJECT_ID} --region=${REGION} \\"
echo "    --member=allUsers --role=roles/run.invoker"
