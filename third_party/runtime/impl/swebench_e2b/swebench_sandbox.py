"""SWE-bench specific E2B Sandbox wrapper.

This module provides a wrapper around E2B SDK for SWE-bench tasks,
using HTTP API to execute commands instead of SDK methods.
"""

import os
from typing import Any

import httpx

from openhands.core.config import SandboxConfig
from openhands.core.logger import openhands_logger as logger

try:
    # Try e2b_code_interpreter first (newer API with Sandbox.create())
    from e2b_code_interpreter import Sandbox as E2BSandbox
except ImportError:
    try:
        # Fallback to e2b
        from e2b import Sandbox as E2BSandbox
    except ImportError:
        E2BSandbox = None  # type: ignore


class SWEBenchSandbox:
    """Wrapper for E2B Sandbox specifically for SWE-bench tasks.

    This class uses HTTP API to execute commands instead of SDK methods,
    as SWE-bench templates may not have full SDK capabilities.
    """

    def __init__(
        self,
        config: SandboxConfig,
        template: str,
        workspace_id: str | None = None,
        e2b_api_key: str | None = None,
        e2b_api_url: str | None = None,
        action_server_base_path: str | None = None,
        action_server_url_template: str | None = None,
        sandbox_timeout: int | None = None,
    ):
        """Initialize SWE-bench E2B Sandbox.

        Args:
            config: Sandbox configuration
            template: E2B template name to use
            workspace_id: E2B workspace ID (optional, can be set later)
            e2b_api_key: E2B API key (defaults to E2B_API_KEY env var)
            e2b_api_url: E2B API base URL (defaults to E2B_API_URL env var or default)
            sandbox_timeout: Timeout in seconds for sandbox creation (defaults to E2B_SANDBOX_TIMEOUT env var or 12000)
        """
        if E2BSandbox is None:
            raise ImportError(
                "E2B SDK is not installed. Please install it with: pip install e2b"
            )

        self.config = config
        self.template = template
        # workspace_id is NOT required for sandbox creation. It is only required when
        # calling the HTTP execute endpoint:
        #   /workspaces/{workspace_id}/sandboxes/{sandbox_id}:execute
        self.workspace_id = workspace_id or os.getenv("E2B_WORKSPACE_ID")
        self.e2b_api_key = e2b_api_key or os.getenv("E2B_API_KEY")
        self.e2b_api_url = e2b_api_url or os.getenv(
            "E2B_API_URL", "https://api.e2b.dev"
        )
        # SenseTime gateway may expose action server under a prefix like /execute.
        # ActionExecutionClient will append endpoints like /alive, /execute_action.
        self.action_server_base_path = (
            action_server_base_path
            if action_server_base_path is not None
            else os.getenv("SWE_E2B_ACTION_SERVER_BASE_PATH", "")
        )
        # Allow overriding the full public URL format (recommended).
        # Example:
        #   https://3000-{sandbox_id}.sandbox.cn-sh-01.sensecoreapi.dev
        # Placeholders:
        #   {sandbox_id} - E2B sandbox id
        #   {port}       - server port (e.g. 3000)
        self.action_server_url_template = (
            action_server_url_template
            if action_server_url_template is not None
            else os.getenv(
                "SWE_E2B_ACTION_SERVER_URL_TEMPLATE",
                "https://{port}-{sandbox_id}.sandbox.cn-sh-01.sensecoreapi.dev",
            )
        )

        if not self.e2b_api_key:
            raise ValueError(
                "E2B_API_KEY environment variable or e2b_api_key parameter is required"
            )

        # Store sandbox timeout (will be used in create())
        self.sandbox_timeout = sandbox_timeout

        # Create E2B sandbox
        self.sandbox: E2BSandbox | None = None
        self._sandbox_id: str | None = None
        self._closed = False

    @property
    def sandbox_id(self) -> str:
        """Get the sandbox ID."""
        if self._sandbox_id is None:
            raise RuntimeError("Sandbox not created yet. Call create() first.")
        return self._sandbox_id

    def create(self) -> None:
        """Create the E2B sandbox."""
        if self.sandbox is not None:
            logger.warning("Sandbox already created")
            return

        # Set environment variables for E2B SDK (SDK reads from env vars)
        os.environ['E2B_API_KEY'] = self.e2b_api_key
        if self.e2b_api_url:
            os.environ['E2B_API_URL'] = self.e2b_api_url

        logger.debug(f'Creating E2B sandbox with template: "{self.template}"')

        # Configure sandbox timeout (seconds).
        # Priority: 1) parameter passed to __init__, 2) environment variable, 3) default 12000s
        if self.sandbox_timeout is not None:
            sandbox_timeout = self.sandbox_timeout
        else:
            timeout_str = os.getenv('E2B_SANDBOX_TIMEOUT', '12000')
            try:
                sandbox_timeout = int(timeout_str)
            except ValueError:
                logger.warning(
                    f'Invalid E2B_SANDBOX_TIMEOUT={timeout_str!r}, falling back to 12000'
                )
                sandbox_timeout = 12000

        # Use Sandbox.create() class method instead of constructor
        # The SDK reads API key from E2B_API_KEY environment variable
        self.sandbox = E2BSandbox.create(template=self.template, timeout=sandbox_timeout)
        self._sandbox_id = self.sandbox.sandbox_id
        logger.debug(
            f'Created E2B sandbox with ID: "{self._sandbox_id}", timeout={sandbox_timeout}s'
        )

    def execute_command(
        self, command: str, language: str = "bash", timeout: int | None = None
    ) -> dict[str, Any]:
        """Execute a command in the sandbox using HTTP API.

        Args:
            command: Command to execute
            language: Language of the command (default: "bash")
            timeout: Timeout in seconds (default: None)

        Returns:
            Response from the execute API

        Raises:
            RuntimeError: If sandbox is not created
            httpx.HTTPError: If API request fails
        """
        if self.sandbox is None or self._sandbox_id is None:
            raise RuntimeError("Sandbox not created. Call create() first.")

        if not self.workspace_id:
            raise ValueError(
                "E2B_WORKSPACE_ID is required to execute commands via HTTP API "
                "(/workspaces/{workspace_id}/sandboxes/{sandbox_id}:execute)."
            )

        # Use /studio/sandbox/v1/ prefix for the API
        path = f"/studio/sandbox/v1/workspaces/{self.workspace_id}/sandboxes/{self._sandbox_id}:execute"
        url = f"{self.e2b_api_url}{path}"

        # API expects execute_content wrapper
        payload = {
            "execute_content": {
                "code": command,
                "language": language,
            }
        }

        headers = {
            "Authorization": f"Bearer {self.e2b_api_key}",
            "Content-Type": "application/json",
        }

        logger.debug(f"Executing command via HTTP API: {command[:100]}...")

        # Use longer timeout for execute API (may take time for first poetry run)
        effective_timeout = timeout if timeout is not None else 120

        with httpx.Client(timeout=effective_timeout) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

    def get_action_server_url(self, port: int = 3000) -> str:
        """Get the public URL for action_execution_server.

        Args:
            port: Port number (default: 3000)

        Returns:
            Public URL for the action server base (may include a gateway prefix, e.g. /execute)

        Raises:
            RuntimeError: If sandbox is not created
        """
        if self._sandbox_id is None:
            raise RuntimeError("Sandbox not created. Call create() first.")

        # Preferred: explicit template (avoids ambiguity about prefixes like /execute)
        if self.action_server_url_template:
            return self.action_server_url_template.format(
                sandbox_id=self._sandbox_id, port=port
            )

        # Fallback: compose host + base path
        host = f"https://{port}-{self._sandbox_id}.sandbox.cn-sh-01.sensecoreapi.dev"
        base_path = (self.action_server_base_path or "").strip()
        if not base_path:
            return host
        if not base_path.startswith("/"):
            base_path = "/" + base_path
        return host + base_path

    def close(self) -> None:
        """Close the sandbox."""
        if self._closed:
            return

        if self.sandbox is not None:
            try:
                # Try different close methods depending on SDK version
                if hasattr(self.sandbox, 'kill'):
                    self.sandbox.kill()
                elif hasattr(self.sandbox, 'close'):
                    self.sandbox.close()
                else:
                    logger.warning(f"Sandbox object has no close/kill method")
                logger.debug(f'Closed E2B sandbox: "{self._sandbox_id}"')
            except Exception as e:
                logger.warning(f"Error closing sandbox: {e}")

        self._closed = True
        self.sandbox = None
        self._sandbox_id = None

