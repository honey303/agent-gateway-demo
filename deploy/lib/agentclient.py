"""Shared helpers for the test scripts in ../tests.

Everything here was duplicated between smoke_test.py and
test_ingress_blocked.py: authentication, the `:query` / `:streamQuery` REST
calls, Cloud Logging lookups, and terminal output.

Import it by putting ../lib on sys.path:

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
    from agentclient import AgentClient, Console, config
"""

import datetime
import json
import os
import subprocess
import sys
import uuid

import google.auth
import google.auth.transport.requests
import requests

API_VERSION = "v1beta1"
REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)


# --------------------------------------------------------------- output ----
class Console:
    """Terminal output. Colours switch off when piped."""

    if sys.stdout.isatty():
        GREEN, RED, YELLOW, DIM, RESET = (
            "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
        )
    else:
        GREEN = RED = YELLOW = DIM = RESET = ""

    @classmethod
    def ok(cls, msg):
        print(f"  {cls.GREEN}PASS{cls.RESET} {msg}")

    @classmethod
    def fail(cls, msg):
        print(f"  {cls.RED}FAIL{cls.RESET} {msg}")

    @classmethod
    def warn(cls, msg):
        print(f"  {cls.YELLOW}WARN{cls.RESET} {msg}")

    @classmethod
    def dim(cls, msg):
        print(f"  {cls.DIM}{msg}{cls.RESET}")

    @classmethod
    def head(cls, msg):
        print(f"\n{msg}")


# --------------------------------------------------------------- config ----
def _env_file_value(key, env_file=None):
    """Reads a key out of mcp_agent/.env, if present."""
    env_file = env_file or os.path.join(REPO_ROOT, "mcp_agent", ".env")
    if not os.path.isfile(env_file):
        return None
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return None


class Config:
    """Resolved settings, from the environment with .env as a fallback."""

    def __init__(self):
        self.project_id = os.environ.get("PROJECT_ID")
        if not self.project_id:
            sys.exit("Set PROJECT_ID to your GCP project id")
        self.location = os.environ.get("LOCATION", "us-central1")
        self.resource_name = os.environ.get("RESOURCE_NAME") or _env_file_value(
            "REASONING_ENGINE_ID"
        )
        if not self.resource_name:
            sys.exit(
                "Set RESOURCE_NAME (or REASONING_ENGINE_ID in mcp_agent/.env) to "
                "the deployed agent, e.g. "
                "projects/123/locations/us-central1/reasoningEngines/456"
            )
        self.base_url = (
            f"https://{self.location}-aiplatform.googleapis.com/{API_VERSION}"
        )
        # RFC 3339. Log queries are scoped by this rather than a --freshness
        # window: after a fix + redeploy the previous failure is still minutes
        # old, and reporting it as the current cause is actively misleading.
        self.start_time = (
            datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    @property
    def engine_id(self):
        return self.resource_name.rsplit("/", 1)[-1]


config = Config()


# ------------------------------------------------------------- logging -----
def read_logs(log_filter, limit=10, fmt="value(textPayload)", project=None):
    """Runs `gcloud logging read`, returning stdout (empty string on failure)."""
    try:
        result = subprocess.run(
            [
                "gcloud", "logging", "read", log_filter,
                f"--project={project or config.project_id}",
                f"--limit={limit}",
                f"--format={fmt}",
            ],
            capture_output=True, text=True, timeout=120,
        )
        return result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def runtime_errors(limit=5):
    """ERROR-level logs emitted by this agent since the run began."""
    return read_logs(
        'resource.type="aiplatform.googleapis.com/ReasoningEngine" AND '
        f'resource.labels.reasoning_engine_id="{config.engine_id}" AND '
        f'severity>=ERROR AND timestamp>="{config.start_time}"',
        limit=limit,
    )


def gateway_denials():
    """'hostname,status' lines for egress the gateway blocked.

    Usually the only visible evidence of a blocked tool call: ADK handles MCP
    failures gracefully, so the agent quietly loses its tools and answers
    without them, leaving nothing in the runtime's ERROR logs.
    """
    log_name = (
        f"projects/{config.project_id}/logs/"
        "networkservices.googleapis.com%2Fgateway_requests"
    )
    return read_logs(
        f'logName="{log_name}" AND '
        'jsonPayload.authzPolicyInfo.result="DENIED" AND '
        f'timestamp>="{config.start_time}"',
        limit=10,
        fmt="csv[no-heading](jsonPayload.enforcedGatewaySecurityPolicy.hostname,"
            "httpRequest.status)",
    )


def model_armor_events(limit=10):
    """'operationType,filterMatchState' lines - the ingress evidence trail.

    Ingress gateways emit nothing to gateway_requests even while actively
    returning 403s, so Model Armor's own log is the only record.
    """
    log_name = (
        f"projects/{config.project_id}/logs/"
        "modelarmor.googleapis.com%2Fsanitize_operations"
    )
    return read_logs(
        f'logName="{log_name}" AND timestamp>="{config.start_time}"',
        limit=limit,
        fmt="csv[no-heading](jsonPayload.operationType,"
            "jsonPayload.sanitizationResult.filterMatchState)",
    )


def authz_policies_on_gateways():
    """Authorization policies whose target is an Agent Gateway."""
    try:
        result = subprocess.run(
            [
                "gcloud", "network-security", "authz-policies", "list",
                f"--project={config.project_id}",
                f"--location={config.location}",
                "--format=json",
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return None
        policies = json.loads(result.stdout or "[]")
    except (subprocess.SubprocessError, FileNotFoundError, json.JSONDecodeError):
        return None
    return [
        p for p in policies
        if any("agentGateways/" in t
               for t in p.get("target", {}).get("resources", []))
    ]


# ---------------------------------------------------------------- client ---
def authorized_session():
    """A requests.Session carrying an ADC bearer token for the Vertex APIs."""
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    http = requests.Session()
    http.headers.update(
        {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
            "x-goog-user-project": config.project_id,
        }
    )
    return http


class AgentClient:
    """Thin REST client for a deployed Agent Runtime agent.

    The SDK route doesn't work here: with google-cloud-aiplatform as pinned,
    the object returned by `agent_engines.get()` has no create_session /
    stream_query attached.
    """

    def __init__(self, user_id_prefix="test"):
        self.user_id = f"{user_id_prefix}-{uuid.uuid4().hex[:8]}"
        self.http = authorized_session()

    # -- configuration -----------------------------------------------------
    def get_spec(self):
        response = self.http.get(
            f"{config.base_url}/{config.resource_name}", timeout=60
        )
        if not response.ok:
            return None, f"{response.status_code} {response.text[:300]}"
        return response.json().get("spec", {}), None

    def preflight(self, require_ingress=False, require_egress=False):
        """Asserts the agent is wired as the README describes."""
        Console.head("Preflight: agent configuration")
        spec, error = self.get_spec()
        if error:
            Console.fail(f"cannot read the agent: {error}")
            return False

        gateways = spec.get("deploymentSpec", {}).get("agentGatewayConfig", {})
        passed = True

        if spec.get("identityType") == "AGENT_IDENTITY":
            Console.ok("identity_type = AGENT_IDENTITY")
        else:
            Console.fail(
                f"identity_type = {spec.get('identityType')} (want AGENT_IDENTITY)"
            )
            passed = False

        for label, key, required in (
            ("ingress gateway ", "clientToAgentConfig", require_ingress),
            ("egress gateway  ", "agentToAnywhereConfig", require_egress),
        ):
            gateway = gateways.get(key, {}).get("agentGateway")
            if gateway:
                Console.ok(f"{label} = {gateway.rsplit('/', 1)[-1]}")
            elif required:
                Console.fail(f"{label} not configured")
                passed = False
            else:
                Console.warn(f"{label.strip()} not configured")

        return passed

    # -- invocation --------------------------------------------------------
    def create_session(self):
        """Creates an ADK session, returning (session_id, error)."""
        response = self.http.post(
            f"{config.base_url}/{config.resource_name}:query",
            # `input` keys are passed to the agent as Python kwargs, so
            # they're snake_case even though the enclosing API is camelCase.
            json={"classMethod": "create_session",
                  "input": {"user_id": self.user_id}},
            timeout=180,
        )
        if not response.ok:
            return None, f"HTTP {response.status_code}: {response.text[:400]}"
        return response.json()["output"]["id"], None

    def stream_query_raw(self, session_id, message):
        """Sends a prompt. Returns (status_code, body_text).

        Drains the stream fully: a policy can also block mid-stream, once the
        gateway has already started proxying.
        """
        response = self.http.post(
            f"{config.base_url}/{config.resource_name}:streamQuery?alt=sse",
            json={
                "classMethod": "stream_query",
                "input": {
                    "user_id": self.user_id,
                    "session_id": session_id,
                    "message": message,
                },
            },
            stream=True,
            timeout=300,
        )
        if not response.ok:
            return response.status_code, response.text
        chunks = [line for line in response.iter_lines(decode_unicode=True) if line]
        return response.status_code, "\n".join(chunks)

    def stream_query(self, session_id, message, verbose=True):
        """Sends a prompt and parses the event stream.

        Returns (tool_calls, tool_results, answer_text, error).
        """
        status, body = self.stream_query_raw(session_id, message)
        if status != 200:
            return None, None, None, f"HTTP {status}: {body[:400]}"

        tool_calls, tool_results, answer = [], [], []
        for line in body.splitlines():
            # SSE when ?alt=sse, plain JSON lines otherwise; tolerate both.
            payload = line[len("data:"):].strip() if line.startswith("data:") else line
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for part in event.get("content", {}).get("parts", []):
                # Event payloads use snake_case part keys even though the
                # enclosing REST API is camelCase. Accept both: this mismatch
                # makes a real tool call look like it never happened.
                call = part.get("function_call") or part.get("functionCall")
                result = part.get("function_response") or part.get("functionResponse")
                if call:
                    tool_calls.append(call.get("name"))
                    if verbose:
                        Console.dim(
                            f"-> tool call: {call.get('name')}({call.get('args')})"
                        )
                if result:
                    tool_results.append(result)
                    if verbose:
                        Console.dim(
                            f"<- tool result: {json.dumps(result.get('response'))[:200]}"
                        )
                if part.get("text"):
                    answer.append(part["text"])
        return tool_calls, tool_results, "".join(answer).strip(), None
