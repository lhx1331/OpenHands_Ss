# SWE-bench E2B Runtime

This Runtime implementation enables OpenHands to run SWE-bench tasks using E2B sandboxes.

## Overview

Unlike the standard `E2BRuntime` which uses E2B SDK methods directly, `SWEBenchE2BRuntime`:
- Uses HTTP API to execute commands (for starting `action_execution_server`)
- Communicates with `action_execution_server` via HTTP REST API (similar to Docker Runtime)
- All file operations go through `action_execution_server` endpoints

## Architecture

```
OpenHands Client
    ↓
SWEBenchE2BRuntime
    ↓
E2B Sandbox (pre-built SWE-bench + OpenHands image)
    ↓
action_execution_server (started via HTTP API)
    ↓
HTTP REST API (/execute_action, /upload_file, etc.)
```

## Prerequisites

1. **E2B SDK**: Install the E2B Python SDK
   ```bash
   pip install e2b
   ```

2. **Environment Variables**:
   - `E2B_API_KEY`: Your E2B API key (required)
   - `E2B_WORKSPACE_ID`: Your E2B workspace ID (required)
   - `E2B_TEMPLATE`: E2B template name (optional, will be derived from base image if not set)
   - `E2B_API_URL`: E2B API base URL (optional, defaults to `https://api.e2b.dev`)

3. **Pre-built E2B Template**:
   - You need to create an E2B template from your SWE-bench + OpenHands Docker image
   - The template should contain the OpenHands runtime with `action_execution_server` pre-installed

## Usage

### 1. Create E2B Template from Docker Image

First, push your SWE-bench OpenHands image to a registry:
```bash
docker tag <your-image> registry.sensetime.com/sensecore-higgs/sandbox/openhands.sweb.eval.x86_64.sqlfluff_1776_sqlfluff-2419:latest
docker push registry.sensetime.com/sensecore-higgs/sandbox/openhands.sweb.eval.x86_64.sqlfluff_1776_sqlfluff-2419:latest
```

Then create an E2B template from this image (using E2B CLI or API):
```bash
e2b template build --dockerfile Dockerfile --name openhands-swebench-sqlfluff-2419
```

### 2. Configure OpenHands

Set environment variables:
```bash
export E2B_API_KEY="your-api-key"
export E2B_WORKSPACE_ID="your-workspace-id"
export E2B_TEMPLATE="openhands-swebench-sqlfluff-2419"  # Optional
```

In your config file or code:
```python
config = OpenHandsConfig(
    runtime="swebench_e2b",  # Use the new runtime
    sandbox=SandboxConfig(
        base_container_image="registry.sensetime.com/sensecore-higgs/sandbox/openhands.sweb.eval.x86_64.sqlfluff_1776_sqlfluff-2419:latest",
    ),
)
```

### 3. Run SWE-bench Tasks

The runtime will:
1. Create an E2B sandbox from the specified template
2. Start `action_execution_server` using HTTP API (`/workspaces/{workspace_id}/sandboxes/{sandbox_id}:execute`)
3. Wait for the server to be ready
4. Execute all actions via HTTP REST API (same as Docker Runtime)

## How It Works

### Sandbox Creation

```python
sandbox = SWEBenchSandbox(
    config=config.sandbox,
    template="openhands-swebench-sqlfluff-2419",
    workspace_id=os.getenv("E2B_WORKSPACE_ID"),
)
sandbox.create()  # Uses E2B SDK: Sandbox.create()
```

### Starting action_execution_server

The runtime generates the startup command using `get_action_execution_server_startup_command()` and executes it via HTTP API:

```python
# Command: ["python", "-u", "-m", "openhands.runtime.action_execution_server", "3000", ...]
# Executed via: POST /workspaces/{workspace_id}/sandboxes/{sandbox_id}:execute
# Payload: {"code": "nohup python -u -m openhands.runtime.action_execution_server 3000 ... &", "language": "bash"}
```

### Action Execution

All actions (run, read, write, etc.) are sent to `action_execution_server` via HTTP:
- URL: `https://3000-{sandbox_id}.sandbox.cn-sh-01.sensecoreapi.dev`
- Endpoint: `/execute_action`
- Format: Same as Docker Runtime

## Differences from Standard E2BRuntime

| Feature | E2BRuntime | SWEBenchE2BRuntime |
|---------|------------|-------------------|
| Command Execution | `sandbox.commands.run()` | HTTP API `/workspaces/...:execute` |
| File Operations | `sandbox.files.read/write()` | `action_execution_server` HTTP API |
| Code Execution | `sandbox.run_code()` | `action_execution_server` HTTP API |
| Server | No server needed | `action_execution_server` required |
| Use Case | General purpose | SWE-bench tasks |

## Troubleshooting

### Sandbox Creation Fails

- Check `E2B_API_KEY` is set correctly
- Verify template name exists in your E2B workspace
- Check network connectivity to E2B API

### action_execution_server Not Starting

- Check logs in sandbox: `/tmp/action_server.log`
- Verify Python and OpenHands dependencies are installed in the template
- Ensure the startup command is correct (check `get_action_execution_server_startup_command` output)

### Connection Timeout

- Verify the public URL format: `https://3000-{sandbox_id}.sandbox.cn-sh-01.sensecoreapi.dev`
- Check firewall/network rules allow access to the sandbox domain
- Ensure `action_execution_server` is actually running (check `/alive` endpoint)

## See Also

- [E2B Documentation](https://e2b.dev/docs)
- [OpenHands Docker Runtime](../docker/docker_runtime.py) - Similar architecture
- [OpenHands Action Execution Server](../../../../openhands/runtime/action_execution_server.py)

