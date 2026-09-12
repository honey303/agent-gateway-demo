#!/usr/bin/env bash
# Deploys the ADK agent (mcp_agent/) to Agent Runtime (Vertex AI Agent
# Engine), via the adk CLI.
#
# Before running this:
#   1. pip install -r deploy/requirements.txt
#      `adk deploy agent_engine` needs `vertexai` (from google-cloud-aiplatform)
#      to actually perform the deploy - it's NOT pulled in by
#      mcp_agent/requirements.txt (google-adk[mcp]), which only covers what
#      the deployed agent itself needs at runtime. Skipping this fails with
#      "ModuleNotFoundError: No module named 'vertexai'".
#   2. cp mcp_agent/.env.example mcp_agent/.env, then fill in
#      GOOGLE_CLOUD_PROJECT and MCP_SERVER_URL (see deploy_mcp_server.sh).
#      `adk deploy agent_engine` packages this .env and ships its values as
#      the deployed agent's environment variables.
#
# Usage:
#   PROJECT_ID=my-project REGION=us-central1 ./deploy/deploy_agent.sh
#
# To redeploy/update an existing agent instead of creating a new one, pass
# its numeric id:
#   PROJECT_ID=my-project AGENT_ENGINE_ID=987654321 ./deploy/deploy_agent.sh
#
# Prints the deployed resource name at the end, e.g.:
#   projects/123456789/locations/us-central1/reasoningEngines/987654321
# Note it down - grant_run_invoker.sh and enable_agent_gateway.py both need it.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to your GCP project id}"
REGION="${REGION:-us-central1}"
DISPLAY_NAME="${DISPLAY_NAME:-MCP Gateway Demo Agent}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/mcp_agent/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}." >&2
  echo "Run: cp mcp_agent/.env.example mcp_agent/.env, fill it in, then retry." >&2
  exit 1
fi

if ! python3 -c "import vertexai" >/dev/null 2>&1; then
  echo "Missing the 'vertexai' module (needed by 'adk deploy agent_engine'" >&2
  echo "itself, not just the deployed agent)." >&2
  echo "Run: pip install -r deploy/requirements.txt, then retry." >&2
  exit 1
fi

deploy_args=(
  --project="${PROJECT_ID}"
  --region="${REGION}"
  --display_name="${DISPLAY_NAME}"
)

if [[ -n "${AGENT_ENGINE_ID:-}" ]]; then
  deploy_args+=(--agent_engine_id="${AGENT_ENGINE_ID}")
fi

adk deploy agent_engine "${deploy_args[@]}" "${REPO_ROOT}/mcp_agent"

echo
echo "Deployed. Copy the 'projects/.../reasoningEngines/...' resource name"
echo "printed above - you'll need it for:"
echo "  RESOURCE_NAME=... ./deploy/grant_run_invoker.sh"
echo "  RESOURCE_NAME=... python deploy/enable_agent_gateway.py   (if using Agent Gateway)"
