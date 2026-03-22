# SWE-bench E2B Evaluation Guide

本文档说明如何使用 E2B 沙箱运行 SWE-bench 评估，以 Kimi K2.5 为例。

## 前置条件

1. **Python 环境**：已通过 `poetry install --with evaluation` 安装依赖。
2. **E2B 沙箱**：已申请 E2B API Key，并准备好 SWE-bench 实例对应的 E2B 模板。
3. **LLM API**：已获取目标模型的 API Key 和 Base URL。

## 1. 配置 `config.toml`

在项目根目录创建 `config.toml`（不要提交到仓库，其中含密钥）。以 Kimi K2.5 为例：

```toml
[llm.kimi_k25]
model = "openai/kimi-k2.5"
api_key = "<your-api-key>"
base_url = "https://api.stage.sensenova.cn/compatible-mode/v2"
max_output_tokens = 16384
temperature = 0.6
top_p = 0.95
timeout = 120
drop_params = false
litellm_kwargs = { extra_body = { thinking = { type = "disabled" } } }

[llm.draft_editor]
model = "openai/kimi-k2.5"
api_key = "<your-api-key>"
base_url = "https://api.stage.sensenova.cn/compatible-mode/v2"
max_output_tokens = 16384
temperature = 0.6
top_p = 0.95
timeout = 120
drop_params = false
litellm_kwargs = { extra_body = { thinking = { type = "disabled" } } }
```

### 关键配置说明

| 参数 | 说明 |
|---|---|
| `model` | 前缀 `openai/` 告诉 LiteLLM 使用 OpenAI-compatible 协议 |
| `temperature` / `top_p` | Kimi K2.5 锁定为 0.6 / 0.95，其他值会被拒绝 |
| `timeout` | LLM 请求超时（秒），建议 ≥120 以应对推理服务波动 |
| `drop_params` | 设为 `false`，确保 `extra_body` 等参数不被丢弃 |
| `litellm_kwargs` | 通过 `extra_body` 传递厂商特有参数，绕过 OpenAI SDK 校验 |
| `thinking.type` | `"disabled"` 关闭思维链模式（SWE-bench 场景推荐关闭） |
| `draft_editor` | CodeActAgent 内部使用的编辑器模型，需与主模型一致 |

## 2. 准备实例列表（可选）

如果只想跑部分实例，先生成实例 ID 列表：

```bash
poetry run python evaluation/benchmarks/swe_bench/scripts/hf_export_swe_bench_verified_instances.py
```

生成的 `swe_bench_verified_instance_list.txt` 每行一个实例 ID。也可以手动编写只含少量 ID 的文件用于调试。

## 3. 设置 E2B 环境变量

在脚本中已有默认值，也可以通过环境变量覆盖：

```bash
export E2B_API_KEY="<your-e2b-api-key>"
export E2B_API_URL="https://sandbox.cn-sh-01.sensecoreapi.cn"
export E2B_WORKSPACE_ID="<your-workspace-id>"
export SWE_E2B_ACTION_SERVER_URL_TEMPLATE="https://3000-{sandbox_id}.sandbox.cn-sh-01.sensecoreapi.cn"
```

## 4. 启动评估

### 完整命令

```bash
EVAL_SKIP_MAXIMUM_RETRIES_EXCEEDED="true" \
LOG_ALL_EVENTS="true" \
poetry run python evaluation/benchmarks/swe_bench/run_infer_e2b_pro.py \
  --llm-config kimi_k25 \
  --dataset princeton-nlp/SWE-bench_Verified \
  --split test \
  --instance_list swe_bench_verified_instance_list.txt \
  --agent-cls CodeActAgent \
  --eval-output-dir evaluation/evaluation_outputs/KIMI-2.5 \
  --max-iterations 100 \
  --eval-num-workers 32
```

### 参数说明

| 参数 | 说明 |
|---|---|
| `EVAL_SKIP_MAXIMUM_RETRIES_EXCEEDED` | 遇到 E2B 沙箱重试耗尽时跳过该实例而非终止整个评估 |
| `LOG_ALL_EVENTS` | 记录所有事件到日志，便于调试 |
| `--llm-config kimi_k25` | 对应 `config.toml` 中的 `[llm.kimi_k25]` 段 |
| `--dataset` | HuggingFace 上的数据集名 |
| `--split test` | 数据集分片 |
| `--instance_list` | 要评估的实例 ID 列表文件（可选，不传则跑全量） |
| `--agent-cls CodeActAgent` | 使用的 Agent 类型 |
| `--eval-output-dir` | 结果输出目录 |
| `--max-iterations 100` | 每个实例最大交互轮数 |
| `--eval-num-workers 32` | 并发 worker 数量 |

### 调试单个实例

只跑一个实例时去掉 `--instance_list`，改用 `--eval-n-limit 1`，或写一个只含目标 ID 的列表文件：

```bash
echo "astropy__astropy-7166" > debug_instance.txt

EVAL_SKIP_MAXIMUM_RETRIES_EXCEEDED="true" \
LOG_ALL_EVENTS="true" \
poetry run python evaluation/benchmarks/swe_bench/run_infer_e2b_pro.py \
  --llm-config kimi_k25 \
  --dataset princeton-nlp/SWE-bench_Verified \
  --split test \
  --instance_list debug_instance.txt \
  --agent-cls CodeActAgent \
  --eval-output-dir evaluation/evaluation_outputs/debug \
  --max-iterations 100
```

## 5. 输出结构

评估完成后在 `--eval-output-dir` 下生成：

```
evaluation_outputs/KIMI-2.5/
└── princeton-nlp__SWE-bench_Verified-test/
    └── CodeActAgent/
        └── kimi-k2.5_maxiter_100/
            ├── output.jsonl          # 每行一个实例的评估结果（含 git_patch）
            └── logs/
                └── instance_*.log    # 每个实例的详细日志
```

## 6. 已知问题与注意事项

| 问题 | 原因 | 应对方式 |
|---|---|---|
| `engine is not available temporarily` | Kimi 推理服务间歇性不可用 | 设置 `EVAL_SKIP_MAXIMUM_RETRIES_EXCEEDED=true` 跳过失败实例，待服务恢复后重跑 |
| `APITimeoutError` | LLM 请求超时 | `config.toml` 中设置 `timeout = 120`（或更大） |
| `fatal: bad object <commit>` | E2B 沙箱使用 squash 后的单次提交历史 | 代码已自动 fallback 到 `git diff HEAD` |
| `invalid content` | Kimi 不接受 list 格式的 message content | 代码已为 Kimi 启用 `force_string_serializer` |
| 空 `git_patch` | 实例因 LLM 错误或沙箱异常未能完成 | 重新跑未完成的实例（脚本会自动跳过已完成的） |

## 7. 重跑失败实例

脚本会自动读取 `output.jsonl` 跳过已完成的实例。直接重新运行相同命令即可继续未完成的部分：

```bash
# 和第一次一样的命令，会自动跳过已完成实例
EVAL_SKIP_MAXIMUM_RETRIES_EXCEEDED="true" \
LOG_ALL_EVENTS="true" \
poetry run python evaluation/benchmarks/swe_bench/run_infer_e2b_pro.py \
  --llm-config kimi_k25 \
  --dataset princeton-nlp/SWE-bench_Verified \
  --split test \
  --instance_list swe_bench_verified_instance_list.txt \
  --agent-cls CodeActAgent \
  --eval-output-dir evaluation/evaluation_outputs/KIMI-2.5 \
  --max-iterations 100 \
  --eval-num-workers 32
```

如果需要重跑特定失败实例（比如 `git_patch` 为空的），可以从 `output.jsonl` 中删除对应行后重新启动。
