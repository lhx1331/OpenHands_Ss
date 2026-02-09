"""SWE-bench E2B Runtime implementation.

This module provides a Runtime implementation for SWE-bench tasks using E2B sandboxes.
It uses HTTP API to execute commands and communicates with action_execution_server
via HTTP REST API, similar to Docker Runtime.
"""

from third_party.runtime.impl.swebench_e2b.swebench_e2b_runtime import (
    SWEBenchE2BRuntime,
)

__all__ = ['SWEBenchE2BRuntime']

