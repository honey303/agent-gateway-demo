#!/usr/bin/env bash
# Shared setup for every script in this repo. Source it as the first thing
# after the shebang:
#
#   source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
#
# It provides strict mode, the common configuration variables, logging
# helpers, and the handful of gcloud lookups that more than one script needs.

set -euo pipefail

# --------------------------------------------------------------- paths -----
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${LIB_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DEPLOY_DIR}/.." && pwd)"
TEMPLATE_DIR="${DEPLOY_DIR}/templates"

# ------------------------------------------------------------- config ------
# Every value is overridable from the environment. Defaults are the names the
# README uses, so the happy path needs only PROJECT_ID.
PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to your GCP project id}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-mcp-demo-server}"
EGRESS_AGW_NAME="${EGRESS_AGW_NAME:-mcp-agent-gateway}"
INGRESS_AGW_NAME="${INGRESS_AGW_NAME:-mcp-agent-gateway-ingress}"
ARMOR_TEMPLATE_ID="${ARMOR_TEMPLATE_ID:-mcp-ingress-armor}"
ARMOR_EXTENSION_ID="${ARMOR_EXTENSION_ID:-ma-ingress-authz-ext}"
ARMOR_POLICY_ID="${ARMOR_POLICY_ID:-ma-ingress-authz-policy}"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/mcp_agent/.env}"

# ------------------------------------------------------------ logging ------
if [[ -t 1 ]]; then
  C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_YELLOW=$'\033[33m'
  C_DIM=$'\033[2m'; C_RESET=$'\033[0m'
else
  C_GREEN=""; C_RED=""; C_YELLOW=""; C_DIM=""; C_RESET=""
fi

step() { printf '\n%s==>%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
info() { printf '  %s\n' "$*"; }
dim()  { printf '  %s%s%s\n' "${C_DIM}" "$*" "${C_RESET}"; }
ok()   { printf '  %sok%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn() { printf '  %swarn%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()  { printf '\n%serror%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; exit 1; }

require_var() {
  # require_var AGENT_PRINCIPAL "how to get one"
  local name="$1" hint="${2:-}"
  if [[ -z "${!name:-}" ]]; then
    die "Set ${name}${hint:+ - ${hint}}"
  fi
}

require_agent_principal() {
  require_var AGENT_PRINCIPAL \
    "the agent's Agent Identity principal, printed by gateway/enable_agent_gateway.py"
}

# ------------------------------------------------------- gcloud lookups ----
project_number() {
  gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)'
}

gateway_path() {
  # gateway_path <name> -> fully qualified agentGateways resource name
  echo "projects/${PROJECT_ID}/locations/${REGION}/agentGateways/$1"
}

mcp_server_id() {
  # `agent-registry services create` makes a Service; the registry then exposes
  # it as an mcpServers resource with a generated id. IAP's agent-registry
  # policies are keyed on that id, not on the service name - and the selector
  # has to be --mcp-server (--endpoint and --agent address different kinds of
  # registry resource, and silently 404 for an MCP server).
  gcloud agent-registry mcp-servers list \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --filter="mcpServerId ~ services:${SERVICE_NAME}\$" \
    --format="value(name.basename())" | head -n 1
}

require_mcp_server_id() {
  local id
  id="$(mcp_server_id)"
  [[ -n "${id}" ]] || die \
    "No Agent Registry MCP server found for '${SERVICE_NAME}'. Run gateway/register_mcp_server.sh first."
  echo "${id}"
}

# --------------------------------------------------------------- IAP -------
iap_binding() {
  # iap_binding add|remove --mcp-server=ID|--endpoint=ID
  local verb="$1" selector="$2"
  gcloud iap web "${verb}-iam-policy-binding" \
    --resource-type=agent-registry \
    "${selector}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --member="${AGENT_PRINCIPAL}" \
    --role=roles/iap.egressor \
    --quiet >/dev/null
}

# ----------------------------------------------------------- templates -----
render_template() {
  # render_template <name.yaml.tmpl> -> prints the path of the rendered file
  local tmpl="${TEMPLATE_DIR}/$1"
  [[ -f "${tmpl}" ]] || die "Missing template ${tmpl}"
  local out
  out="$(mktemp "${TMPDIR:-/tmp}/agw-XXXXXX.yaml")"
  AGW_NAME="${AGW_NAME:-}" PROJECT_ID="${PROJECT_ID}" REGION="${REGION}" \
    envsubst <"${tmpl}" >"${out}"
  echo "${out}"
}

# -------------------------------------------------------------- .env -------
env_get() {
  # env_get KEY -> value from mcp_agent/.env, empty if absent
  [[ -f "${ENV_FILE}" ]] || return 0
  sed -n "s/^$1=//p" "${ENV_FILE}" | tail -n 1 | tr -d "\"'"
}

env_set() {
  # env_set KEY VALUE - upsert into mcp_agent/.env
  local key="$1" value="$2"
  touch "${ENV_FILE}"
  if grep -q "^${key}=" "${ENV_FILE}"; then
    # value can contain slashes and +, so use a python rewrite rather than sed
    python3 - "${ENV_FILE}" "${key}" "${value}" <<'PY'
import sys
path, key, value = sys.argv[1:4]
lines = open(path).read().splitlines()
out = [f"{key}={value}" if l.startswith(f"{key}=") else l for l in lines]
open(path, "w").write("\n".join(out) + "\n")
PY
  else
    printf '%s=%s\n' "${key}" "${value}" >>"${ENV_FILE}"
  fi
}
