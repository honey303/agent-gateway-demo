# agent-gateway-demo

A minimal example of a Google **ADK** agent that calls tools on a remote
**MCP** server, deployed to **Vertex AI Agent Engine** (Google Cloud's
managed agent runtime).

```
┌─────────────────────┐   Streamable HTTP    ┌──────────────────────┐
│  mcp_agent/          │  ───────────────────▶│  mcp_server/          │
│  ADK agent           │                       │  MCP server           │
│  (Gemini + McpToolset)│◀───────────────────  │  (roll_dice, etc.)     │
└──────────┬───────────┘                       └───────────┬──────────┘
           │ adk deploy agent_engine                        │ gcloud run deploy
           ▼                                                ▼
  Vertex AI Agent Engine                                 Cloud Run
```

- **`mcp_server/`** – a tiny MCP server (built with the official `mcp` Python
  SDK's `FastMCP`) exposing three demo tools: `roll_dice`, `get_server_time`,
  `word_count`. It's deployed to **Cloud Run**.
- **`mcp_agent/`** – an ADK agent whose only tool is an `McpToolset` pointed
  at the MCP server's URL. It's deployed to **Vertex AI Agent Engine**.

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

```bash
PROJECT_ID=<your-project-id> REGION=us-central1 ./deploy/deploy_mcp_server.sh
```

This builds `mcp_server/` into a container (via Cloud Build) and deploys it
to Cloud Run, printing the service URL. Note the URL — you'll need
`<url>/mcp` as `MCP_SERVER_URL`.

> The script uses `--allow-unauthenticated` for demo simplicity, since MCP
> Streamable HTTP doesn't carry Google credentials on its own. For anything
> beyond a demo, see **Securing the MCP server** below.

## 3. Deploy the agent to Vertex AI Agent Engine

Update `mcp_agent/.env` with the real values:

```dotenv
GOOGLE_GENAI_USE_ENTERPRISE=1
GOOGLE_CLOUD_PROJECT=<your-project-id>
GOOGLE_CLOUD_LOCATION=us-central1
MCP_SERVER_URL=https://<your-cloud-run-url>/mcp
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

## 4. Query the deployed agent

```python
from vertexai import agent_engines

agent_engine = agent_engines.get(
    "projects/<PROJECT_ID>/locations/us-central1/reasoningEngines/<ID>"
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

This demo leaves the Cloud Run MCP server publicly reachable
(`--allow-unauthenticated`) to keep the walkthrough simple. For real use,
lock it down with Cloud Run's built-in IAM auth instead:

1. Redeploy without `--allow-unauthenticated` (or run
   `gcloud run services remove-iam-policy-binding ... --member=allUsers`).
2. Grant the Agent Engine's runtime service account the `roles/run.invoker`
   role on the Cloud Run service.
3. Have the agent attach a Google-signed ID token as a bearer header, e.g.:

   ```python
   import google.auth.transport.requests
   import google.oauth2.id_token

   token = google.oauth2.id_token.fetch_id_token(
       google.auth.transport.requests.Request(), audience=MCP_SERVER_URL
   )
   mcp_toolset = McpToolset(
       connection_params=StreamableHTTPConnectionParams(
           url=MCP_SERVER_URL,
           headers={"Authorization": f"Bearer {token}"},
       ),
   )
   ```

## Cleaning up

```bash
gcloud run services delete mcp-demo-server --region=us-central1
# Delete the Agent Engine resource (replace with your resource name):
python -c "from vertexai import agent_engines; agent_engines.delete('projects/<PROJECT_ID>/locations/us-central1/reasoningEngines/<ID>')"
```
