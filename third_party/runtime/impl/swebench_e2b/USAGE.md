# SWE-bench E2B Runtime 使用指南

## 快速开始

### 1. 环境变量配置

```bash
export E2B_API_KEY="your-e2b-api-key"
export E2B_WORKSPACE_ID="your-workspace-id"
export E2B_TEMPLATE="openhands-swebench-sqlfluff-2419"  # 可选，会自动从 base_image 推导
```

### 2. 在 SWE-bench 评测中使用

```bash
# 设置 runtime 环境变量
export RUNTIME=swebench_e2b

# 运行评测
python evaluation/benchmarks/swe_bench/run_infer.py \
    <model_config> <commit_hash> <agent> <eval_limit> <max_iter> <num_workers> <dataset> <split> <n_runs> <mode>
```

### 3. 在代码中使用

```python
from openhands.core.config import OpenHandsConfig, SandboxConfig

config = OpenHandsConfig(
    runtime="swebench_e2b",
    sandbox=SandboxConfig(
        base_container_image="registry.sensetime.com/sensecore-higgs/sandbox/openhands.sweb.eval.x86_64.sqlfluff_1776_sqlfluff-2419:latest",
    ),
)

# 创建 runtime
from openhands.core.setup import create_runtime
runtime = create_runtime(config, ...)
await runtime.connect()
```

## 工作流程

1. **创建 Sandbox**: 使用 E2B SDK `Sandbox.create(template=...)` 创建 sandbox
2. **启动 action_execution_server**: 通过 HTTP API `/workspaces/{workspace_id}/sandboxes/{sandbox_id}:execute` 执行启动命令
3. **等待就绪**: 轮询 `/alive` 端点直到服务器就绪
4. **执行任务**: 所有操作通过 `action_execution_server` 的 HTTP REST API

## 模板名称推导

如果未设置 `E2B_TEMPLATE` 环境变量，runtime 会尝试从 `base_container_image` 推导：

- 镜像名: `registry.sensetime.com/sensecore-higgs/sandbox/openhands.sweb.eval.x86_64.sqlfluff_1776_sqlfluff-2419:latest`
- 推导的模板名: `openhands-sweb-eval-x86-64-sqlfluff-1776-sqlfluff-2419`

**建议**: 明确设置 `E2B_TEMPLATE` 环境变量以避免推导错误。

## 故障排查

### Sandbox 创建失败

```bash
# 检查环境变量
echo $E2B_API_KEY
echo $E2B_WORKSPACE_ID

# 检查模板是否存在
# 使用 E2B CLI 或 API 查看可用模板
```

### action_execution_server 启动失败

```bash
# 在 sandbox 中查看日志
# 日志位置: /tmp/action_server.log

# 检查 Python 环境
python --version
python -m openhands.runtime.action_execution_server --help
```

### 连接超时

```bash
# 检查服务器 URL
# 格式: https://3000-{sandbox_id}.sandbox.cn-sh-01.sensecoreapi.dev

# 测试连接
curl https://3000-{sandbox_id}.sandbox.cn-sh-01.sensecoreapi.dev/alive
```

## 与 Docker Runtime 的对比

| 特性 | Docker Runtime | SWEBenchE2BRuntime |
|------|---------------|-------------------|
| 运行位置 | 本地 Docker | E2B 云端 |
| 镜像管理 | 本地构建/拉取 | E2B 模板 |
| 命令执行 | Docker API | HTTP API |
| 文件操作 | Docker volume | action_execution_server API |
| 网络访问 | 本地端口映射 | E2B 公共 URL |
| 适用场景 | 本地开发/评测 | 云端评测/大规模任务 |

## 注意事项

1. **模板必须包含 OpenHands 运行时**: 确保 E2B 模板基于预构建的 SWE-bench + OpenHands 镜像
2. **网络访问**: 确保可以访问 `*.sandbox.cn-sh-01.sensecoreapi.dev` 域名
3. **API 配额**: 注意 E2B API 的调用限制和配额
4. **成本**: E2B sandbox 按使用时间计费，注意控制成本

