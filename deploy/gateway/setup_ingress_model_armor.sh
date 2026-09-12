#!/usr/bin/env bash
# Attaches Model Armor to the Client-to-Agent (ingress) Agent Gateway, so the
# ingress path actually *enforces* something instead of just passing traffic
# through.
#
# An ingress gateway accepts exactly ONE authorization policy, and it must use
# the CONTENT_AUTHZ profile. This script wires up the Model Armor flavour:
#
#   1. Model Armor template  - what to screen for
#   2. IAM                   - service agents that may call Model Armor
#   3. authzExtension        - points Service Extensions at Model Armor
#   4. authzPolicy           - binds that extension to the gateway
#
# Note on service accounts: for ingress the grants go to the *AI Platform
# Reasoning Engine* service agent (gcp-sa-aiplatform-re), NOT the Service
# Extensions service agent (gcp-sa-dep) used for egress. The two docs pages
# disagree on this; we grant both because the extension plumbing itself runs
# as gcp-sa-dep and extra bindings are harmless.
#
# Usage:
#   PROJECT_ID=my-project ./deploy/gateway/setup_ingress_model_armor.sh
#
# Optional:
#   AGW_NAME            ingress gateway (default mcp-agent-gateway-ingress)
#   ARMOR_TEMPLATE_ID   template id     (default mcp-ingress-armor)
#   ENFORCEMENT         enabled|disabled (default enabled)
#
# References:
#   https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/configure-model-armor
#   https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/delegate-authorization

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

AGW_NAME="${AGW_NAME:-${INGRESS_AGW_NAME}}"
ENFORCEMENT="${ENFORCEMENT:-enabled}"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

PROJECT_NUMBER="$(project_number)"
RE_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
DEP_SA="service-${PROJECT_NUMBER}@gcp-sa-dep.iam.gserviceaccount.com"
TEMPLATE_PATH="projects/${PROJECT_ID}/locations/${REGION}/templates/${ARMOR_TEMPLATE_ID}"

info "Project ${PROJECT_ID} (${PROJECT_NUMBER}), region ${REGION}, gateway ${AGW_NAME}"

# ---------------------------------------------------------------- APIs -----
step "1/5 Enabling required APIs"
gcloud services enable \
  modelarmor.googleapis.com \
  networksecurity.googleapis.com \
  networkservices.googleapis.com \
  --project="${PROJECT_ID}" --quiet
ok "enabled"

# ------------------------------------------------------ Model Armor tmpl ----
step "2/5 Model Armor template '${ARMOR_TEMPLATE_ID}'"
# Deliberately NOT enabling the responsible-AI (RAI) filters.
#
# The RAI 'dangerous' category flags this demo's own happy path - "Roll a
# 6-sided die" reads as gambling - so with RAI on at MEDIUM_AND_ABOVE the
# gateway blocks every legitimate request. Check any prompt yourself with the
# template's :sanitizeUserPrompt endpoint before turning a filter on.
#
# pi-and-jailbreak is the filter that matters for ingress anyway: it's what
# the docs recommend against prompt-injection attacks, and it cleanly
# separates a benign prompt from an injection payload.
FILTER_FLAGS=(
  --pi-and-jailbreak-filter-settings-enforcement="${ENFORCEMENT}"
  --pi-and-jailbreak-filter-settings-confidence-level=LOW_AND_ABOVE
  --malicious-uri-filter-settings-enforcement="${ENFORCEMENT}"
  --basic-config-filter-enforcement="${ENFORCEMENT}"
  --template-metadata-log-sanitize-operations
  --template-metadata-log-operations
)

if gcloud model-armor templates describe "${ARMOR_TEMPLATE_ID}" \
     --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud model-armor templates update "${ARMOR_TEMPLATE_ID}" \
    --location="${REGION}" --project="${PROJECT_ID}" \
    --clear-rai-settings-filters "${FILTER_FLAGS[@]}" >/dev/null
  ok "updated (enforcement=${ENFORCEMENT})"
else
  gcloud model-armor templates create "${ARMOR_TEMPLATE_ID}" \
    --location="${REGION}" --project="${PROJECT_ID}" \
    "${FILTER_FLAGS[@]}" >/dev/null
  ok "created (enforcement=${ENFORCEMENT})"
fi
dim "${TEMPLATE_PATH}"

# ----------------------------------------------------------------- IAM -----
step "3/5 Granting Model Armor access to the service agents"
grant() {
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:$1" --role="$2" \
    --condition=None --quiet >/dev/null
  dim "${2} -> ${1%%@*}@..."
}
grant "${RE_SA}" roles/modelarmor.calloutUser      # ingress caller
grant "${RE_SA}" roles/modelarmor.user
grant "${DEP_SA}" roles/modelarmor.calloutUser     # extension plumbing
grant "${DEP_SA}" roles/serviceusage.serviceUsageConsumer
grant "${DEP_SA}" roles/modelarmor.user

# ------------------------------------------------------ authz extension ----
step "4/5 Importing authorization extension '${ARMOR_EXTENSION_ID}'"
cat >"${WORK_DIR}/ext.yaml" <<EOF
name: ${ARMOR_EXTENSION_ID}
service: modelarmor.${REGION}.rep.googleapis.com
failOpen: false
timeout: 1s
metadata:
  model_armor_settings: '[
    {
      "request_template_id": "${TEMPLATE_PATH}",
      "response_template_id": "${TEMPLATE_PATH}"
    }
  ]'
EOF
gcloud service-extensions authz-extensions import "${ARMOR_EXTENSION_ID}" \
  --source="${WORK_DIR}/ext.yaml" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" >/dev/null
ok "imported"

# --------------------------------------------------------- authz policy ----
step "5/5 Importing authorization policy '${ARMOR_POLICY_ID}'"
# No httpRules: for Client-to-Agent the extension binds to the whole gateway.
# (The egress sample adds content-type rules to keep internal gRPC chatter
# away from Model Armor; ingress traffic is already just query/streamQuery.)
cat >"${WORK_DIR}/policy.yaml" <<EOF
name: ${ARMOR_POLICY_ID}
target:
  resources:
  - "$(gateway_path "${AGW_NAME}")"
policyProfile: CONTENT_AUTHZ
action: CUSTOM
customProvider:
  authzExtension:
    resources:
    - "projects/${PROJECT_ID}/locations/${REGION}/authzExtensions/${ARMOR_EXTENSION_ID}"
EOF
gcloud network-security authz-policies import "${ARMOR_POLICY_ID}" \
  --source="${WORK_DIR}/policy.yaml" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" >/dev/null
ok "imported"

step "Done - Model Armor is screening ingress traffic to '${AGW_NAME}'"
info "In Client-to-Agent mode the gateway only governs query and streamQuery,"
info "which is exactly what the test scripts use."
info "Verify with:"
dim "PROJECT_ID=${PROJECT_ID} python deploy/tests/test_ingress_blocked.py"
