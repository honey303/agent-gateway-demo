# agent-gateway-demo

A minimal example of a Google **ADK** agent that calls tools on a remote
**MCP** server, deployed to **Agent Runtime** (Google Cloud's managed agent
runtime, part of the Gemini Enterprise Agent Platform - formerly Vertex AI
Agent Builder), with its outbound MCP tool calls governed by **Agent
Gateway**.

> **A note on naming:** "Agent Runtime" is the current product name for what
> the `reasoningEngines` API and the `adk` CLI still call **Agent Engine** -
> they're the same resource. This README uses "Agent Runtime" for the
> product and "Agent Engine" when referring to the literal API/CLI surface.

```
 mcp_agent/                                              mcp_server/
 ADK agent                                                MCP server
 (Gemini + McpToolset)  ──▶  Agent Gateway  ──▶            (roll_dice, etc.)
                             (Agent-to-Anywhere,
                              checks IAM against
                              Agent Registry)
        │ adk deploy agent_engine                                 │ gcloud run deploy
        ▼                                                         ▼
   Agent Runtime                                               Cloud Run
 (Agent Identity on)                                    (registered in
                                                           Agent Registry)
```

- **`mcp_server/`** – a tiny MCP server (built with the official `mcp` Python
  SDK's `FastMCP`) exposing three demo tools: `roll_dice`, `get_server_time`,
  `word_count`. It's deployed to **Cloud Run**.
- **`mcp_agent/`** – an ADK agent whose only tool is an `McpToolset` pointed
  at the MCP server's URL. It's deployed to **Agent Runtime**.
- **`deploy/`** – scripts to deploy both, register the MCP server with
  **Agent Registry**, and route the agent's tool calls through **Agent
  Gateway** instead of calling Cloud Run directly.

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

Update `mcp_agent/.env` with the real values:

```dotenv
GOOGLE_GENAI_USE_ENTERPRISE=1
GOOGLE_CLOUD_PROJECT=<your-project-id>
GOOGLE_CLOUD_LOCATION=us-central1
MCP_SERVER_URL=https://<your-cloud-run-url>/mcp
# MCP_SERVER_AUTH=auto  # attaches an ID token automatically; see below
```

Then deploy with the ADK CLI:

```bash
adk deploy agent_engine \
  --project=<your-project-id> \
  --region=us-central1 \
  --display_name="MCP Gateway Demo Agent" \
  mcp_agent
```

This packages `mcp_agent/` (including its `requirements.txt` and `.env`),
builds a container via Cloud Build, and creates/updates a **Reasoning
Engine** (Agent Engine) resource in your project. On success it prints the
deployed resource name, e.g.:

```
projects/123456789/locations/us-central1/reasoningEngines/987654321
```

To redeploy after changes, add `--agent_engine_id=<the numeric id>` so it
updates the existing resource instead of creating a new one.

## 4. Route the agent's tool calls through Agent Gateway

By default, the deployed agent calls `MCP_SERVER_URL` directly - the same as
running locally. **Agent Gateway** puts a governed, zero-trust proxy in
front of that call instead: the agent's outbound (egress) traffic goes
through the gateway, which checks IAM authorization (`roles/iap.egressor`)
against **Agent Registry** before letting a call through to the MCP server.
This is Google Cloud's recommended pattern for agent-to-tool traffic on
Agent Runtime as of the 2026 Gemini Enterprise Agent Platform rebrand.

```
mcp_agent (Agent Runtime, Agent Identity)
   │  outbound call to the MCP server, transparently intercepted
   ▼
Agent Gateway  (Agent-to-Anywhere / egress mode)
   │  checks: does this agent's identity have roles/iap.egressor
   │  on the target service registered in Agent Registry?
   ▼
mcp_server  (Cloud Run, registered in Agent Registry)
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

### Prerequisites

- Your MCP server is already deployed (step 2 above) and its URL is known.
- Your agent is already deployed (step 3 above) and you have its resource
  name, e.g. `projects/123456789/locations/us-central1/reasoningEngines/987654321`.
- Your Google Cloud organization ID (`gcloud organizations list`) - needed
  to construct the agent's Agent Identity principal.
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
   value - you'll need it in step 3.

2. **Register the MCP server with Agent Registry**, so Agent Gateway knows
   about it:

   ```bash
   PROJECT_ID=<your-project-id> REGION=us-central1 \
   MCP_URL=https://<your-cloud-run-url>/mcp \
     ./deploy/register_mcp_server.sh
   ```

3. **Create the Agent Gateway and authorize the agent to use it**, using the
   principal from step 1:

   ```bash
   PROJECT_ID=<your-project-id> REGION=us-central1 \
   AGENT_PRINCIPAL='principal://agents.global.org-.../reasoningEngines/...' \
     ./deploy/setup_agent_gateway.sh
   ```

   This prints the new gateway's resource name, e.g.
   `projects/<PROJECT_ID>/locations/us-central1/agentGateways/mcp-agent-gateway`.

4. **Point the agent's egress at the gateway**:

   ```bash
   PROJECT_ID=<your-project-id> LOCATION=us-central1 \
   RESOURCE_NAME=projects/<PROJECT_ID>/locations/us-central1/reasoningEngines/<ID> \
   AGENT_GATEWAY_RESOURCE_NAME=projects/<PROJECT_ID>/locations/us-central1/agentGateways/mcp-agent-gateway \
     python deploy/enable_agent_gateway.py
   ```

From here, the agent's calls to `MCP_SERVER_URL` are intercepted and
authorized by Agent Gateway rather than going straight to Cloud Run. You can
now also lock the Cloud Run service back down (remove
`--allow-unauthenticated`) - see **Securing the MCP server** below.

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

1. Redeploy `mcp_server/` **without** `--allow-unauthenticated`, or run:
   ```bash
   gcloud run services remove-iam-policy-binding <SERVICE_NAME> \
     --region=<REGION> --member=allUsers --role=roles/run.invoker
   ```
2. Grant the agent's runtime identity `roles/run.invoker` on the Cloud Run
   service:
   ```bash
   gcloud run services add-iam-policy-binding <SERVICE_NAME> \
     --region=<REGION> \
     --member="serviceAccount:<AGENT_RUNTIME_SERVICE_ACCOUNT>" \
     --role=roles/run.invoker
   ```
   The default runtime identity is
   `service-<PROJECT_NUMBER>@gcp-sa-aiplatform-re.iam.gserviceaccount.com`
   unless you set a custom `service_account` or `identity_type=AGENT_IDENTITY`
   (see step 4) when deploying.

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
gcloud alpha agent-registry services delete mcp-demo-server \
  --project=<PROJECT_ID> --location=us-central1
```
