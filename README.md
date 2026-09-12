# agent-gateway-demo

A minimal example of a Google **ADK** agent that calls tools on a remote
**MCP** server, deployed to **Agent Runtime** (Google Cloud's managed agent
runtime, part of the Gemini Enterprise Agent Platform - formerly Vertex AI
Agent Builder), with *both* directions of its traffic governed by **Agent
Gateway**: outbound MCP tool calls (egress) and inbound client calls to the
agent itself (ingress).

> **A note on naming:** "Agent Runtime" is the current product name for what
> the `reasoningEngines` API and the `adk` CLI still call **Agent Engine** -
> they're the same resource. This README uses "Agent Runtime" for the
> product and "Agent Engine" when referring to the literal API/CLI surface.

```
              Agent Gateway                                  Agent Gateway
client ──▶ (Client-to-Agent,   ──▶  mcp_agent/         ──▶  (Agent-to-Anywhere,   ──▶ mcp_server/
  call     ingress)                 ADK agent                checks IAM against         MCP server
                                     (Gemini + McpToolset)     Agent Registry)          (roll_dice, etc.)
                                          │ adk deploy agent_engine                            │ gcloud run deploy
                                          ▼                                                    ▼
                                     Agent Runtime                                          Cloud Run
                                   (Agent Identity on)                                (registered in
                                                                                        Agent Registry)
```

- **`mcp_server/`** – a tiny MCP server (built with the official `mcp` Python
  SDK's `FastMCP`) exposing three demo tools: `roll_dice`, `get_server_time`,
  `word_count`. It's deployed to **Cloud Run**.
- **`mcp_agent/`** – an ADK agent whose only tool is an `McpToolset` pointed
  at the MCP server's URL. It's deployed to **Agent Runtime**.
- **`deploy/`** – scripts to deploy both, register the MCP server with
  **Agent Registry**, and route both directions of agent traffic through
  **Agent Gateway** (egress to the MCP server, ingress from clients to the
  agent) instead of calling/being called directly.

Everything here is a self-contained demo (no external API keys required) —
swap the MCP server's tools and the agent's model/instructions for your own.

## Why the MCP server is a separate, HTTP-based service

ADK's `MCPToolset`/`McpToolset` can connect over stdio (spawning a local MCP
server subprocess) or over HTTP (SSE / Streamable HTTP, talking to a remote
server). For **local development** either works fine.

For **deployment to Agent Engine**, only the HTTP-based connection works
today. Agent Engine's deploy step pickles the agent object graph to ship it;
a stdio-based `MCPToolset` holds a live subprocess/pipe (`TextIOWrapper`)
that cannot be pickled, so deployment fails with:

```
TypeError: cannot pickle 'TextIOWrapper' instances
```

(see [google/adk-python#1727](https://github.com/google/adk-python/issues/1727)
and [#1024](https://github.com/google/adk-python/issues/1024)). Running the
MCP server as its own HTTP service and connecting via
`StreamableHTTPConnectionParams(url=...)` sidesteps the bug entirely — the
agent only stores a URL string, which pickles just fine.

## Prerequisites

- Python 3.11+
- A GCP project with the Vertex AI API and Cloud Run API enabled, and
  billing set up
- `gcloud` CLI, authenticated (`gcloud auth login`) with
  `gcloud config set project <PROJECT_ID>`
- Application Default Credentials for local testing:
  `gcloud auth application-default login`

## 1. Run it locally

Install dependencies (a virtualenv is recommended):

```bash
pip install -r mcp_server/requirements.txt
pip install -r mcp_agent/requirements.txt
```

Start the MCP server (defaults to `http://127.0.0.1:8080/mcp`):

```bash
python mcp_server/server.py
```

In another terminal, configure the agent to use it:

```bash
cp mcp_agent/.env.example mcp_agent/.env
# mcp_agent/.env already defaults MCP_SERVER_URL to the local server above.
# Fill in GOOGLE_CLOUD_PROJECT with your project id (needed for Vertex AI).
```

Then run the agent with ADK's dev UI or CLI, from the repo root:

```bash
adk web .          # browser chat UI, pick "mcp_agent" from the dropdown
# or
adk run mcp_agent  # terminal chat
```

Try asking it: *"Roll a 20-sided die"* or *"What's the server time?"* — it
should call the corresponding MCP tool and use the result in its reply.

## 2. Deploy the MCP server to Cloud Run

Already have it deployed? Skip straight to step 3 with its URL.

```bash
PROJECT_ID=<your-project-id> REGION=us-central1 ./deploy/deploy_mcp_server.sh
```

This builds `mcp_server/` into a container (via Cloud Build) and deploys it
to Cloud Run, printing the service URL. Note the URL — you'll need
`<url>/mcp` as `MCP_SERVER_URL`.

> The script uses `--allow-unauthenticated` for demo simplicity. This is
> **not** recommended beyond a demo — see **Securing the MCP server** below,
> which is now the default behavior of `mcp_agent/agent.py` (it attaches a
> Google ID token automatically for any `https://` `MCP_SERVER_URL`).

## 3. Deploy the agent to Vertex AI Agent Engine

Install the deploy-time tooling first (separate from
`mcp_agent/requirements.txt`, which only covers what the deployed agent
needs at *runtime* — `adk deploy agent_engine` itself additionally needs
`vertexai`, which isn't pulled in by that file):

```bash
pip install -r deploy/requirements.txt
```

Update `mcp_agent/.env` with the real values:

```dotenv
GOOGLE_GENAI_USE_ENTERPRISE=1
GOOGLE_CLOUD_PROJECT=<your-project-id>
GOOGLE_CLOUD_LOCATION=us-central1
MCP_SERVER_URL=https://<your-cloud-run-url>/mcp
# MCP_SERVER_AUTH=auto  # attaches an ID token automatically; see below
```

Then deploy:

```bash
PROJECT_ID=<your-project-id> REGION=us-central1 ./deploy/deploy_agent.sh
```

This is a thin wrapper around the ADK CLI (`adk deploy agent_engine`). It
packages `mcp_agent/` (including its `requirements.txt` and `.env`), builds
a container via Cloud Build, and creates/updates a **Reasoning Engine**
(Agent Engine) resource in your project. On success it prints the deployed
resource name, e.g.:

```
projects/123456789/locations/us-central1/reasoningEngines/987654321
```

Note it down - you'll need it below and in step 4. To redeploy after
changes instead of creating a new resource, pass its numeric id:

```bash
PROJECT_ID=<your-project-id> AGENT_ENGINE_ID=987654321 ./deploy/deploy_agent.sh
```

Now grant the agent's runtime identity permission to actually call the MCP
server (`mcp_agent/agent.py` attaches an ID token by default, but that only
authorizes anything once Cloud Run knows to trust it):

```bash
PROJECT_ID=<your-project-id> REGION=us-central1 ./deploy/grant_run_invoker.sh
```

This grants `roles/run.invoker` on the Cloud Run MCP service to the
default Agent Runtime service agent
(`service-<PROJECT_NUMBER>@gcp-sa-aiplatform-re.iam.gserviceaccount.com`).
If you deployed with a custom `service_account`, or with Agent Identity
(step 4), pass `AGENT_SERVICE_ACCOUNT=...` instead - see the script's
header comment.

## 4. Route both ingress and egress through Agent Gateway

By default the agent calls `MCP_SERVER_URL` directly (egress), and clients
call the agent's `reasoningEngines.query` endpoint directly (ingress) - the
same as running locally. **Agent Gateway** puts a governed, zero-trust
proxy in front of *both* instead - this is Google Cloud's recommended
pattern for agent traffic on Agent Runtime as of the 2026 Gemini Enterprise
Agent Platform rebrand:

- **Egress** (Agent-to-Anywhere): the agent's outbound tool calls are
  checked against `roles/iap.egressor` on the target, as registered in
  **Agent Registry**, before being let through to the MCP server.
- **Ingress** (Client-to-Agent): incoming client calls to the agent are
  intercepted and can be inspected/policed (e.g. with Model Armor for
  prompt-injection defense) before they reach Agent Runtime. This one
  requires the gateway to be in the *same project and region* as the agent
  (egress gateways may live in a different project, same region only), and
  doesn't use Agent Registry at all.

```
        Agent Gateway                          Agent Gateway
     (Client-to-Agent /             mcp_agent  (Agent-to-Anywhere /
        ingress)                  (Agent Runtime,      egress)
           │                       Agent Identity)         │
client ────┤  checked at                 │      checked: roles/iap.egressor
  call     │  the network edge          │      on the target in Agent
           ▼                            ▼      Registry?
      reasoningEngines.query    outbound call intercepted ──▶ mcp_server
      (no code change needed)                                (Cloud Run)
```

> **Heads-up:** Agent Gateway, Agent Registry, and Agent Identity are newly
> announced (Cloud Next 2026) enterprise governance features. The scripts
> below follow the documented resource shapes and `gcloud` commands as of
> this writing, but some surfaces are still on the `alpha` track and flag
> names may move - if a command fails, check it against the linked docs
> before assuming the script is wrong. This is also meaningfully more
> networking/IAM setup than a demo strictly needs; skip this section if you
> just want the agent talking to its MCP server directly (steps 1-3 above
> are already a complete, working deployment).
>
> One specific rough edge: both YAML templates set `protocols: [MCP]`, even
> the ingress one, where the traffic (a client calling
> `reasoningEngines.query`) obviously isn't MCP wire protocol. Per someone
> who reverse-engineered the actual schema, `MCP` and `PROTOCOL_UNSPECIFIED`
> are currently the *only* two values that field accepts for the whole
> Agent Gateway resource type - there's no distinct value yet for
> agent-query/A2A-style traffic. Once you have real `gcloud` access, you
> can confirm this yourself for certain: submit an intentionally-invalid
> `protocols` value and the rejected import prints the live JSON schema
> back at you - the single most reliable way to check anything here.

### Prerequisites

- Your MCP server is already deployed (step 2 above) and its URL is known.
- Your agent is already deployed (step 3 above) and you have its resource
  name, e.g. `projects/123456789/locations/us-central1/reasoningEngines/987654321`.
- Your Google Cloud organization ID (`gcloud organizations list`) - needed
  to construct the agent's Agent Identity principal (egress only).
- `pip install -r deploy/requirements.txt`

### Steps

1. **Turn on Agent Identity for the deployed agent**, then read off its
   identity principal:

   ```bash
   PROJECT_ID=<your-project-id> LOCATION=us-central1 \
   RESOURCE_NAME=projects/<PROJECT_ID>/locations/us-central1/reasoningEngines/<ID> \
   ORGANIZATION_ID=<your-org-id> \
     python deploy/enable_agent_gateway.py
   ```

   Note the printed `principal://agents.global.org-.../reasoningEngines/...`
   value - you'll need it in step 3 (egress only; ingress needs no extra
   IAM binding by default).

2. **Register the MCP server with Agent Registry** (egress only), so Agent
   Gateway knows about it:

   ```bash
   PROJECT_ID=<your-project-id> REGION=us-central1 \
   MCP_URL=https://<your-cloud-run-url>/mcp \
     ./deploy/register_mcp_server.sh
   ```

3. **Create the egress gateway and authorize the agent to use it**, using
   the principal from step 1:

   ```bash
   PROJECT_ID=<your-project-id> REGION=us-central1 \
   AGENT_PRINCIPAL='principal://agents.global.org-.../reasoningEngines/...' \
     ./deploy/setup_agent_gateway.sh
   ```

   Prints the egress gateway's resource name, e.g.
   `projects/<PROJECT_ID>/locations/us-central1/agentGateways/mcp-agent-gateway`.

4. **Create the ingress gateway** (must be same project + region as the
   agent):

   ```bash
   PROJECT_ID=<your-project-id> REGION=us-central1 \
     ./deploy/setup_ingress_gateway.sh
   ```

   Prints the ingress gateway's resource name, e.g.
   `projects/<PROJECT_ID>/locations/us-central1/agentGateways/mcp-agent-gateway-ingress`.

5. **Point the agent at both gateways** in one call:

   ```bash
   PROJECT_ID=<your-project-id> LOCATION=us-central1 \
   RESOURCE_NAME=projects/<PROJECT_ID>/locations/us-central1/reasoningEngines/<ID> \
   EGRESS_AGENT_GATEWAY_RESOURCE_NAME=projects/<PROJECT_ID>/locations/us-central1/agentGateways/mcp-agent-gateway \
   INGRESS_AGENT_GATEWAY_RESOURCE_NAME=projects/<PROJECT_ID>/locations/us-central1/agentGateways/mcp-agent-gateway-ingress \
     python deploy/enable_agent_gateway.py
   ```

   (Set just one of the two env vars if you only want one direction routed
   through Agent Gateway for now - re-run later with the other once it's
   ready; each call only changes what you pass.)

From here: the agent's calls to `MCP_SERVER_URL` are intercepted and
authorized by the egress gateway rather than going straight to Cloud Run,
and client calls to the agent (step 5 in "Query the deployed agent" below -
no code change needed there) are intercepted by the ingress gateway before
reaching Agent Runtime. You can now also lock the Cloud Run service back
down (remove `--allow-unauthenticated`) - see **Securing the MCP server**
below.

Want Model Armor (prompt-injection / harmful-content inspection) on the
ingress path too? That's a further step on top of this - see
[Configure Model Armor on a gateway](https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/configure-model-armor)
and the ["Agent Gateway ingress to Agent Runtime with Model Armor" codelab](https://codelabs.developers.google.com/agw-cuj-arun-ingress-modar) -
not covered by the scripts here.

## 5. Query the deployed agent

```python
import vertexai

client = vertexai.Client(project="<PROJECT_ID>", location="us-central1")
agent_engine = client.agent_engines.get(
    name="projects/<PROJECT_ID>/locations/us-central1/reasoningEngines/<ID>"
)
session = agent_engine.create_session(user_id="demo-user")
for event in agent_engine.stream_query(
    user_id="demo-user",
    session_id=session["id"],
    message="Roll a 6-sided die twice and tell me both results.",
):
    print(event)
```

## Securing the MCP server

Should the call to Cloud Run be unauthenticated? **No** — not beyond a
quick local test. `mcp_agent/agent.py` handles this for you by default
(`MCP_SERVER_AUTH=auto`): it attaches a Google-signed ID token, fetched via
Application Default Credentials, as a bearer header on any `https://`
`MCP_SERVER_URL`, and skips it for `localhost`/`127.0.0.1`. To make that
token actually mean something, lock the Cloud Run service down:

1. Grant the agent's runtime identity `roles/run.invoker` on the Cloud Run
   service (see step 3 above):
   ```bash
   PROJECT_ID=<your-project-id> REGION=us-central1 ./deploy/grant_run_invoker.sh
   ```
2. Stop accepting unauthenticated calls - redeploy `mcp_server/` **without**
   `--allow-unauthenticated`, or remove the existing public binding:
   ```bash
   gcloud run services remove-iam-policy-binding <SERVICE_NAME> \
     --project=<PROJECT_ID> --region=<REGION> \
     --member=allUsers --role=roles/run.invoker
   ```

If you've set up **Agent Gateway** (step 4 above) instead, it becomes the
enforcement point: authorization is centrally checked via
`roles/iap.egressor` against Agent Registry. In that setup you can set
`MCP_SERVER_AUTH=off` in `mcp_agent/.env` (the gateway handles auth, so the
agent doesn't need to attach its own token) — but leaving it on `auto` is
harmless too, since Cloud Run simply ignores extra bearer tokens once IAM
auth is delegated to the gateway.

## Cleaning up

```bash
# Agent Runtime (Agent Engine) resource - replace with your resource name:
python -c "
import vertexai
client = vertexai.Client(project='<PROJECT_ID>', location='us-central1')
client.agent_engines.delete(name='projects/<PROJECT_ID>/locations/us-central1/reasoningEngines/<ID>')
"

gcloud run services delete mcp-demo-server --region=us-central1

# If you set up Agent Gateway (step 4):
gcloud network-services agent-gateways delete mcp-agent-gateway \
  --project=<PROJECT_ID> --location=us-central1
gcloud network-services agent-gateways delete mcp-agent-gateway-ingress \
  --project=<PROJECT_ID> --location=us-central1
gcloud alpha agent-registry services delete mcp-demo-server \
  --project=<PROJECT_ID> --location=us-central1
```
