"""SWE-bench E2B Runtime implementation.

This Runtime uses E2B sandboxes for SWE-bench tasks, communicating with
action_execution_server via HTTP REST API, similar to Docker Runtime.
"""

import os
from typing import Callable

import httpx
from tenacity import retry, retry_if_exception, stop_after_delay, wait_fixed

from openhands.core.config import OpenHandsConfig
from openhands.core.exceptions import AgentRuntimeDisconnectedError
from openhands.events import EventStream
from openhands.events.action import Action, CmdRunAction
from openhands.events.observation import CmdOutputObservation, Observation
from openhands.integrations.provider import PROVIDER_TOKEN_TYPE
from openhands.llm.llm_registry import LLMRegistry
from openhands.runtime.impl.action_execution.action_execution_client import (
    ActionExecutionClient,
)
from openhands.runtime.utils.request import RequestHTTPError
from openhands.runtime.plugins import PluginRequirement
from openhands.runtime.runtime_status import RuntimeStatus
from openhands.runtime.utils.command import (
    get_action_execution_server_startup_command,
)
from openhands.utils.async_utils import call_sync_from_async

from third_party.runtime.impl.swebench_e2b.swebench_sandbox import SWEBenchSandbox


def _is_retryable_error(exception: Exception) -> bool:
    """Check if an exception is retryable."""
    return isinstance(
        exception,
        (
            httpx.ConnectTimeout,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.HTTPStatusError,
            httpx.ReadTimeout,
            ConnectionError,
        ),
    )


class SWEBenchE2BRuntime(ActionExecutionClient):
    """Runtime for SWE-bench tasks using E2B sandboxes.

    This runtime:
    1. Creates an E2B sandbox from a pre-built template (SWE-bench + OpenHands image)
    2. Starts action_execution_server using HTTP API
    3. Communicates with action_execution_server via HTTP REST API
    4. All file operations go through action_execution_server (like Docker Runtime)
    """

    def __init__(
        self,
        config: OpenHandsConfig,
        event_stream: EventStream,
        llm_registry: LLMRegistry,
        sid: str = "default",
        plugins: list[PluginRequirement] | None = None,
        env_vars: dict[str, str] | None = None,
        status_callback: Callable | None = None,
        attach_to_existing: bool = False,
        headless_mode: bool = True,
        user_id: str | None = None,
        git_provider_tokens: PROVIDER_TOKEN_TYPE | None = None,
    ):
        """Initialize SWE-bench E2B Runtime.

        Args:
            config: OpenHands configuration
            event_stream: Event stream for runtime events
            llm_registry: LLM registry
            sid: Session ID
            plugins: List of plugin requirements
            env_vars: Environment variables
            status_callback: Status callback function
            attach_to_existing: Whether to attach to existing sandbox
            headless_mode: Whether to run in headless mode
            user_id: User ID
            git_provider_tokens: Git provider tokens
        """
        super().__init__(
            config,
            event_stream,
            llm_registry,
            sid,
            plugins,
            env_vars,
            status_callback,
            attach_to_existing,
            headless_mode,
            user_id,
            git_provider_tokens,
        )

        self.sandbox: SWEBenchSandbox | None = None
        self._server_port = 3000
        self._server_url: str | None = None

        # Configure session headers for action_execution_server calls via the public E2B gateway.
        # In SenseCore's E2B gateway, auth can be required for ALL non-/alive endpoints.
        # We set both headers because some gateways accept one or the other depending on routing.
        self._ensure_gateway_auth_headers()

    def _ensure_gateway_auth_headers(self) -> None:
        """Ensure the HttpSession has auth headers required by the E2B public gateway."""
        e2b_api_key = os.getenv("E2B_API_KEY")
        if not e2b_api_key:
            return

        # Bearer token (works in our manual curl/httpx tests to https://3000-<sandbox>/...)
        self.session.headers.setdefault("Authorization", f"Bearer {e2b_api_key}")
        # Some gateways use X-API-Key for the same purpose
        self.session.headers.setdefault("X-API-Key", e2b_api_key)

    @property
    def action_execution_server_url(self) -> str:
        """Get the action execution server URL."""
        if self._server_url is None:
            if self.sandbox is None:
                raise RuntimeError("Sandbox not initialized")
            self._server_url = self.sandbox.get_action_server_url(self._server_port)
        # Defensive: auth headers must exist before any request is sent.
        self._ensure_gateway_auth_headers()
        return self._server_url

    def _get_e2b_template_name(self) -> str:
        """Get E2B template name from config or instance_id.

        Template naming rule:
        - instance_id: sphinx-doc__sphinx-9698
        - template: openhands-swe-{instance_id.replace("__", "_1776_")}
        - Example: openhands-swe-sphinx-doc_1776_sphinx-9698

        Returns:
            E2B template name
        """
        # Try to get template from config (if explicitly set)
        template = os.getenv("E2B_TEMPLATE")
        if template:
            return template

        # Otherwise, derive from instance_id using the conversion rule
        # Get instance_id from environment variable (set by run_infer_e2b.py)
        instance_id = os.getenv("SWE_INSTANCE_ID")

        if instance_id:
            # Apply conversion rule: replace "__" with "_1776_"
            id_docker_compatible = instance_id.replace("__", "_1776_")
            template = f"openhands-swe-{id_docker_compatible}"
            self.log("debug", f"Generated template name from instance_id {instance_id}: {template}")
            return template

        # Fallback: try to derive from base_container_image
        base_image = self.config.sandbox.base_container_image
        if base_image:
            # Extract template name from image name
            # e.g., "registry.sensetime.com/sensecore-higgs/sandbox/openhands.sweb.eval.x86_64.sqlfluff_1776_sqlfluff-2419:latest"
            # -> "openhands-sweb-eval-sqlfluff-2419" or similar
            if "openhands" in base_image.lower() and "sweb" in base_image.lower():
                # Extract a meaningful template name
                parts = base_image.split("/")
                if parts:
                    image_name = parts[-1].split(":")[0]
                    # Convert to template-friendly name
                    template = image_name.replace(".", "-").replace("_", "-")
                    return template

        # Default fallback
        self.log("warning", "Could not determine template name, using default")
        return "openhands-swebench"

    async def connect(self) -> None:
        """Connect to the E2B sandbox and start action_execution_server."""
        self.set_runtime_status(RuntimeStatus.STARTING_RUNTIME)

        try:
            # 1. Create sandbox
            await call_sync_from_async(self._create_sandbox)

            # 2. Start action_execution_server
            await call_sync_from_async(self._start_action_execution_server)

            # 3. Wait for server to be ready
            await call_sync_from_async(self.wait_until_alive)

            # 4. Setup initial environment
            if not self.attach_to_existing:
                await call_sync_from_async(self.setup_initial_env)
                # Keep E2B command execution aligned with Docker runtime:
                # force conda testbed python for subsequent shell commands.
                await call_sync_from_async(self._ensure_testbed_python)

            self.set_runtime_status(RuntimeStatus.READY)
            self._runtime_initialized = True
            self.log("info", f"SWE-bench E2B Runtime ready at {self.action_execution_server_url}")

        except Exception as e:
            self.log("error", f"Failed to connect: {e}")
            self.close()
            raise

    def _ensure_testbed_python(self) -> None:
        """Force and verify testbed Python in the runtime shell session.

        Docker SWE-bench runtime expects command execution to use testbed Python.
        Enforce the same requirement for E2B runtime to avoid silently falling back
        to poetry/openhands Python.
        """
        activate_cmd = (
            "set -e; "
            # Prefer explicit testbed python path (Docker-compatible behavior).
            "if [ -x /opt/conda/envs/testbed/bin/python ]; then "
            "export OH_TESTBED_BIN=/opt/conda/envs/testbed/bin; "
            "elif [ -x /opt/miniconda3/envs/testbed/bin/python ]; then "
            "export OH_TESTBED_BIN=/opt/miniconda3/envs/testbed/bin; "
            "else "
            "echo 'testbed python binary not found under /opt/conda or /opt/miniconda3' >&2; "
            "exit 1; "
            "fi; "
            # Keep conda activate as a best effort for env vars, but do not rely on it.
            "if [ -f /opt/conda/etc/profile.d/conda.sh ]; then "
            ". /opt/conda/etc/profile.d/conda.sh; "
            "elif [ -f /opt/miniconda3/etc/profile.d/conda.sh ]; then "
            ". /opt/miniconda3/etc/profile.d/conda.sh; "
            "fi; "
            "(conda activate testbed "
            "|| conda activate /opt/conda/envs/testbed "
            "|| conda activate /opt/miniconda3/envs/testbed "
            "|| true); "
            # Force command resolution to testbed python regardless of shell activation quirks.
            "export PATH=$OH_TESTBED_BIN:$PATH; "
            "hash -r; "
            "echo __OH_TESTBED_PY__$(python -c 'import sys; print(sys.executable)'); "
            "which python; "
            "python -V"
        )
        action = CmdRunAction(command=activate_cmd)
        action.set_hard_timeout(120)
        obs = self.run_action(action)
        if not isinstance(obs, CmdOutputObservation) or obs.exit_code != 0:
            raise RuntimeError(
                f"Failed to activate conda testbed python in E2B runtime: {obs}"
            )

        output = obs.content or ""
        if "envs/testbed/bin/python" not in output:
            raise RuntimeError(
                "Expected testbed python interpreter after conda activation, "
                f"but got output: {output}"
            )
        self.log("info", f"Using testbed python for runtime commands:\n{output}")

    def _create_sandbox(self) -> None:
        """Create the E2B sandbox."""
        template = self._get_e2b_template_name()
        workspace_id = os.getenv("E2B_WORKSPACE_ID")

        # Get sandbox timeout from environment variable (set in run_infer_e2b.py)
        # Default to 12000 seconds if not set
        sandbox_timeout_str = os.getenv("E2B_SANDBOX_TIMEOUT")
        if sandbox_timeout_str:
            sandbox_timeout = int(sandbox_timeout_str)
            self.log("info", f"Using sandbox timeout from E2B_SANDBOX_TIMEOUT: {sandbox_timeout} seconds")
        else:
            sandbox_timeout = None  # Will use default 12000 in SWEBenchSandbox.create()
            self.log("info", "Using default sandbox timeout: 12000 seconds")

        self.log("info", f"Creating E2B sandbox with template: {template}")

        self.sandbox = SWEBenchSandbox(
            config=self.config.sandbox,
            template=template,
            # workspace_id is only required when using the HTTP execute API to start the server.
            workspace_id=workspace_id,
            sandbox_timeout=sandbox_timeout,
        )
        self.sandbox.create()

        self.log("info", f"Created E2B sandbox: {self.sandbox.sandbox_id}")

    def _start_action_execution_server(self) -> None:
        """Start action_execution_server in the sandbox using HTTP API.

        Key learnings from testing:
        1. Must use `poetry run python` to access dependencies in poetry virtualenv
        2. Must set PYTHONPATH=/openhands/code to find openhands module
        3. Port is a positional argument, not --port
        4. SWE-bench images have UID 1000 taken by 'nonroot' user, so use that

        Optimization: Instead of fixed sleep, we poll the /alive endpoint.
        """
        if self.sandbox is None:
            raise RuntimeError("Sandbox not created")

        # Build the startup command
        # Note: We manually construct the command instead of using get_action_execution_server_startup_command
        # because the SWE-bench sandbox environment is different:
        # - Packages are in poetry virtualenv, so we need `poetry run`
        # - Need to set PYTHONPATH for openhands module
        # - UID 1000 is taken by 'nonroot' user in SWE-bench images

        plugins_arg = ""
        if self.plugins:
            plugin_names = [p.name for p in self.plugins]
            plugins_arg = f"--plugins {' '.join(plugin_names)} "

        # Get browser settings from config
        enable_browser = getattr(self.config, 'enable_browser', False)
        browser_arg = "--enable-browser" if enable_browser else "--no-enable-browser"

        # Build the full command - use same format as Docker runtime for consistency
        # Use -m module import (like Docker) instead of file path
        startup_cmd = (
            f"cd /openhands/code && "
            f"export PYTHONPATH=/openhands/code:$PYTHONPATH && "
            f"nohup /openhands/micromamba/bin/micromamba run -n openhands poetry run python -u "
            f"-m openhands.runtime.action_execution_server "
            f"{self._server_port} "
            f"--working-dir /workspace "
            f"--username nonroot "
            f"--user-id 1000 "
            f"{plugins_arg}"
            f"{browser_arg} "
            f"> /tmp/action_server.log 2>&1 &"
        )

        self.log("info", f"Starting action_execution_server on port {self._server_port}...")

        try:
            # Execute command via HTTP API - use short timeout since it's a background command
            # The nohup command should return quickly, but E2B API may wait for all output
            self.sandbox.execute_command(
                command=startup_cmd,
                language="bash",
                timeout=10,  # Short timeout - command runs in background
            )
        except (httpx.ReadTimeout, httpx.TimeoutException):
            # Timeout is expected - the server starts in background
            self.log("debug", "Execute API timed out (expected for background process)")
        except Exception as e:
            # Log but don't fail - server may still have started
            self.log("debug", f"Execute command exception: {e}")

        # Server startup is handled by wait_until_alive() with retry logic
        # No need for fixed sleep here

    @retry(
        stop=stop_after_delay(180),  # Allow up to 3 minutes for server startup (includes poetry warmup)
        retry=retry_if_exception(_is_retryable_error),
        reraise=True,
        wait=wait_fixed(3),  # Check every 3 seconds
    )
    def wait_until_alive(self) -> None:
        """Wait for action_execution_server to be ready.

        Uses retry logic to poll the /alive endpoint until the server responds.
        This is more efficient than fixed sleep as it returns as soon as server is ready.
        """
        if self.sandbox is None:
            raise AgentRuntimeDisconnectedError("Sandbox not initialized")

        # Check if sandbox is still alive
        if self.sandbox._closed:
            raise AgentRuntimeDisconnectedError("Sandbox has been closed")

        try:
            # Check if action_execution_server is alive
            self.check_if_alive()
            self.log("debug", "action_execution_server is alive")
        except Exception as e:
            # On failure, try to get server logs for debugging
            self._log_server_status_on_failure(log_level="warning")
            raise

    def _log_server_status_on_failure(self, log_level: str = "warning") -> None:
        """Log server status and logs when health check fails or 500 error occurs.

        Args:
            log_level: Log level to use ("error", "warning", or "debug")
        """
        if self.sandbox is None:
            return

        try:
            # Check if server process is running
            ps_result = self.sandbox.execute_command(
                command="ps aux | grep -E 'action_execution|uvicorn' | grep -v grep | head -3",
                language="bash",
                timeout=5,
            )
            ps_output = ps_result.get("result", {}).get("stdout", "")

            if ps_output.strip():
                self.log(log_level, f"Server process found: {ps_output[:200]}")
            else:
                self.log(log_level, "Server process not found - may have crashed")

            # Check workspace directory status
            workspace_check_cmd = (
                "ls -la /workspace/ 2>&1 && "
                "echo '---' && "
                "if [ -d /workspace/pvlib__pvlib-python__0.7 ]; then "
                "  echo 'Directory exists' && ls -la /workspace/pvlib__pvlib-python__0.7/ | head -5; "
                "else "
                "  echo 'Directory NOT found'; "
                "fi"
            )
            workspace_result = self.sandbox.execute_command(
                command=workspace_check_cmd,
                language="bash",
                timeout=10,
            )
            workspace_output = workspace_result.get("result", {}).get("stdout", "")
            if workspace_output:
                self.log(log_level, f"Workspace directory status:\n{workspace_output}")

            # Get last few lines of server log
            log_result = self.sandbox.execute_command(
                command="tail -50 /tmp/action_server.log 2>/dev/null || echo 'No log file'",
                language="bash",
                timeout=5,
            )
            log_output = log_result.get("result", {}).get("stdout", "")
            if log_output and log_output != "No log file":
                # Always log errors at error level, others at specified level
                if "Error" in log_output or "error" in log_output.lower() or "Traceback" in log_output:
                    self.log("error", f"Server log contains errors:\n{log_output[-1000:]}")
                else:
                    self.log(log_level, f"Server log tail:\n{log_output[-500:]}")
        except Exception as e:
            self.log(log_level, f"Could not get server status: {e}")

    def close(self) -> None:
        """Close the runtime and clean up resources."""
        super().close()

        if self.sandbox is not None:
            self.sandbox.close()
            self.sandbox = None

        self._server_url = None
        self.log("info", "SWE-bench E2B Runtime closed")

    def send_action_for_execution(self, action: Action) -> Observation:
        """Override to add better error handling for 500 errors.

        When action_execution_server returns 500, we fetch server logs
        to help diagnose the issue.
        """
        try:
            # Defensive: ensure we always send gateway auth headers for every action request.
            self._ensure_gateway_auth_headers()
            # Log every action sent to /execute_action so we can see successful ones as well.
            try:
                self.log("info", f"EXECUTE_ACTION request: {action}", extra={"msg_type": "ACTION"})
            except Exception:
                # Logging should never break execution
                pass

            obs = super().send_action_for_execution(action)

            # Log the corresponding observation (truncate to avoid huge logs)
            try:
                obs_str = str(obs)
                if len(obs_str) > 2000:
                    obs_str = obs_str[:2000] + "... [truncated]"
                self.log("info", f"EXECUTE_ACTION response: {obs_str}", extra={"msg_type": "OBSERVATION"})
            except Exception:
                pass

            return obs
        except RequestHTTPError as e:
            # If it's a 500 error, get server logs for debugging
            if e.response is not None and e.response.status_code == 500:
                # Log the action that caused the error
                action_str = str(action)
                if hasattr(action, 'command'):
                    action_str = f"{action_str}\nCommand: {action.command[:500]}"  # Limit length
                self.log("error", f"Server returned 500 error for action: {action_str}")
                self.log("error", f"Server error: {e}")
                if e.detail:
                    self.log("error", f"Server error detail: {e.detail}")
                # Log whether gateway auth headers are present (helps debug intermittent auth failures)
                try:
                    auth_present = bool(self.session.headers.get("Authorization"))
                    x_api_key_present = bool(self.session.headers.get("X-API-Key"))
                    self.log(
                        "error",
                        f"Gateway auth headers present? Authorization={auth_present}, X-API-Key={x_api_key_present}",
                    )
                except Exception:
                    pass
                # Log raw response text (usually contains traceback from action_execution_server)
                try:
                    resp_text = e.response.text
                    if resp_text:
                        self.log("error", f"Server raw response (truncated):\n{resp_text[:4000]}")
                except Exception as _:
                    # Avoid masking original error if response body can't be read
                    pass
                # Get server logs and status with error level
                self._log_server_status_on_failure(log_level="error")
            raise

