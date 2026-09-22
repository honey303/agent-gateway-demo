"""A simple ADK agent that uses tools exposed by a remote MCP server.

Deployment target: Vertex AI Agent Engine ("GCP Agent Runtime"), via
    adk deploy agent_engine --project=<PROJECT> --region=<REGION> mcp_agent

The agent talks to the MCP server defined in ../mcp_server over Streamable
HTTP (MCP_SERVER_URL). Using HTTP rather than a local stdio subprocess is
required here: Agent Engine's deploy step pickles the agent, and a
stdio-based MCPToolset holds an open subprocess/pipe that cannot be
pickled (see https://github.com/google/adk-python/issues/1727). A remote
HTTP MCP server has no such handle, so it deploys cleanly.

Authenticating to the MCP server (Cloud Run):
  Cloud Run services should require IAM auth in anything beyond a quick
  demo (i.e. deployed WITHOUT --allow-unauthenticated), with the agent's
  runtime identity granted roles/run.invoker. To call an authenticated
  Cloud Run service, attach a Google-signed OIDC ID token whose audience
  is the service URL - that's what _auth_headers() below does, using
  Application Default Credentials (the agent's own service account when
  deployed; your `gcloud auth application-default login` locally).

  MCP_SERVER_AUTH controls this:
    "auto" (default) - attach a token for any https:// MCP_SERVER_URL;
                        skip it for http://localhost/127.0.0.1 (local dev
                        against an unauthenticated local server).
    "on"              - always attach a token; raise if that fails.
    "off"             - never attach a token (e.g. the Cloud Run service
                        is deliberately --allow-unauthenticated, or an
                        Agent Gateway in front of it handles auth instead).
"""

import base64
import os
import tempfile
from urllib.parse import urlparse


def _trust_agent_gateway_ca() -> None:
    """Adds the Agent Gateway's TLS-inspection CA to this process's trust store.

    An Agent-to-Anywhere (egress) Agent Gateway terminates and re-signs the
    agent's outbound TLS, so *every* outbound HTTPS call fails with
    CERTIFICATE_VERIFY_FAILED ("self-signed certificate in certificate
    chain") unless the gateway's CA is trusted - not just tool calls, but
    ADK's own calls to the managed session service on
    aiplatform.mtls.googleapis.com, which makes even create_session fail.

    Source-based (non-BYOC) deploys are documented to get this CA injected
    automatically during image creation. That only covers images built
    *after* the gateway is attached; attaching a gateway to an
    already-deployed engine leaves the running image without it, so we add
    it here at runtime instead.

    Set AGENT_GATEWAY_CA_B64 to the base64 of the gateway's
    `agentGatewayCard.rootCertificates` PEM - deploy/gateway/trust_gateway_ca.sh
    writes it into mcp_agent/.env for you. Unset, this is a no-op.
    """
    encoded = os.environ.get("AGENT_GATEWAY_CA_B64")
    if not encoded:
        return

    try:
        gateway_ca = base64.b64decode(encoded)
    except Exception:
        print("AGENT_GATEWAY_CA_B64 is not valid base64 - skipping CA setup.")
        return

    # Append to certifi's bundle rather than replacing it: the agent still
    # needs to verify ordinary public certificates.
    import certifi

    with open(certifi.where(), "rb") as bundle_file:
        bundle = bundle_file.read()
    if not bundle.endswith(b"\n"):
        bundle += b"\n"
    bundle += gateway_ca

    handle, path = tempfile.mkstemp(prefix="agw-ca-bundle-", suffix=".pem")
    with os.fdopen(handle, "wb") as out:
        out.write(bundle)

    # One variable per consumer: stdlib ssl/aiohttp, requests, and gRPC.
    os.environ["SSL_CERT_FILE"] = path
    os.environ["REQUESTS_CA_BUNDLE"] = path
    os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = path


# Must run before anything builds an SSL context or an API client.
_trust_agent_gateway_ca()

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8080/mcp")
MCP_SERVER_AUTH = os.environ.get("MCP_SERVER_AUTH", "auto").lower()


def _auth_headers(url: str) -> dict[str, str] | None:
    """Returns an Authorization header carrying a Google ID token for `url`,
    or None if auth is disabled/skipped. See MCP_SERVER_AUTH above."""
    if MCP_SERVER_AUTH == "off":
        return None
    if MCP_SERVER_AUTH == "auto" and urlparse(url).hostname in ("localhost", "127.0.0.1"):
        return None

    import google.auth.transport.requests
    import google.oauth2.id_token

    # Cloud Run's IAM check verifies the token's audience against the
    # service's own URL (scheme + host), not the full request path.
    parsed = urlparse(url)
    audience = f"{parsed.scheme}://{parsed.netloc}"

    try:
        token = google.oauth2.id_token.fetch_id_token(
            google.auth.transport.requests.Request(), audience=audience
        )
    except Exception:
        if MCP_SERVER_AUTH == "on":
            raise
        # "auto" mode: fall back to no auth rather than failing to start -
        # useful if the target is intentionally unauthenticated.
        return None
    return {"Authorization": f"Bearer {token}"}


mcp_toolset = McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url=MCP_SERVER_URL,
        headers=_auth_headers(MCP_SERVER_URL),
    ),
)

root_agent = Agent(
    model="gemini-2.5-flash",
    name="mcp_gateway_agent",
    description="An assistant that can call tools hosted on a remote MCP server.",
    instruction=(
        "You are a helpful assistant. You have access to tools served by an "
        "external MCP server (dice rolling, the current server time, and "
        "word counting). Use them whenever they are relevant to the user's "
        "request, and otherwise answer normally."
    ),
    tools=[mcp_toolset],
)
