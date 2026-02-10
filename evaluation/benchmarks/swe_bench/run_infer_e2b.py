"""Run SWE-bench evaluation for a single instance using E2B runtime.

This script is a specialized version of run_infer.py for E2B sandboxes.
It runs a single SWE-bench instance using the swebench_e2b runtime with
a pre-built E2B template.
"""

import asyncio
import json
import os
import sys

from datasets import load_dataset

from evaluation.benchmarks.swe_bench.resource.mapping import (
    get_instance_resource_factor,
)
# Import necessary functions from run_infer.py
from evaluation.benchmarks.swe_bench.run_infer import (
    AGENT_CLS_TO_FAKE_USER_RESPONSE_FN,
    complete_runtime,
    get_instruction,
    initialize_runtime,
)
from openhands.events.action.commands import CmdRunAction
from evaluation.utils.shared import assert_and_raise
from evaluation.utils.shared import (
    EvalMetadata,
    EvalOutput,
    assert_and_raise,
    get_default_sandbox_config_for_eval,
    get_metrics,
    is_fatal_evaluation_error,
    make_metadata,
    prepare_dataset,
    reset_logger_for_multiprocessing,
    run_evaluation,
    update_llm_config_for_completions_logging,
)
from openhands.core.config import (
    AgentConfig,
    OpenHandsConfig,
    get_evaluation_parser,
    get_llm_config_arg,
)
from openhands.core.config.condenser_config import NoOpCondenserConfig
from openhands.core.config.utils import get_condenser_config_arg
from openhands.core.logger import openhands_logger as logger
from openhands.core.main import create_runtime, run_controller
from openhands.events.serialization.event import event_to_dict
from openhands.utils.async_utils import call_async_from_sync

# ===================== E2B & SWE-bench 固定配置（可在此直接填写） =====================
# 注意：这里的值会作为默认值，如果外部已经通过环境变量设置了同名键，不会被覆盖。
# 建议在本地开发环境使用，避免将包含密钥的文件提交到公共仓库。

# E2B 访问配置
os.environ.setdefault("E2B_API_KEY", "")  # 请替换为实际 API Key
os.environ.setdefault("E2B_API_URL", "https://sandbox.cn-sh-01.sensecoreapi.tech")
os.environ.setdefault("E2B_WORKSPACE_ID", "")

# action_execution_server 对外访问 URL 模板
os.environ.setdefault(
    "SWE_E2B_ACTION_SERVER_URL_TEMPLATE",
    "https://3000-{sandbox_id}.sandbox.cn-sh-01.sensecoreapi.tech",
)

# Import from run_infer.py
from evaluation.benchmarks.swe_bench.run_infer import (
    DATASET_TYPE,
    ENABLE_LLM_EDITOR,
    RUN_WITH_BROWSING,
    USE_HINT_TEXT,
    get_instance_docker_image,
    set_dataset_type,
)


def get_e2b_config(
    instance,
    metadata: EvalMetadata,
    instance_id: str,
) -> OpenHandsConfig:
    """Get OpenHandsConfig for E2B runtime.

    This is similar to get_config() in run_infer.py but:
    - Forces runtime="swebench_e2b"
    - Uses the instance_id to determine base_container_image (for reference only)
    """
    # Get the docker image name (for reference/logging, not actually used by E2B)
    use_swebench_official_image = DATASET_TYPE != 'SWE-Gym'
    base_container_image = get_instance_docker_image(
        instance_id,
        swebench_official_image=use_swebench_official_image,
    )

    logger.info(
        f'Using instance container image (reference): {base_container_image}. '
        f'Actual E2B template will be determined by E2B_TEMPLATE env var.'
    )

    sandbox_config = get_default_sandbox_config_for_eval()
    sandbox_config.base_container_image = base_container_image
    sandbox_config.enable_auto_lint = True
    sandbox_config.use_host_network = False
    sandbox_config.platform = 'linux/amd64'
    sandbox_config.remote_runtime_resource_factor = get_instance_resource_factor(
        dataset_name=metadata.dataset,
        instance_id=instance_id,
    )
    # Increase timeout for E2B runtime to handle long-running tests (e.g., Django tests)
    # E2B gateway and network overhead may require longer timeout than Docker runtime
    sandbox_config.timeout = 600  # 10 minutes for action execution timeout

    # Set sandbox creation timeout (separate from action execution timeout)
    # This controls how long the E2B sandbox itself will stay alive
    # Directly set to 6000 seconds (100 minutes) for E2B sandbox creation
    os.environ["E2B_SANDBOX_TIMEOUT"] = "6000"

    # Force runtime to swebench_e2b
    config = OpenHandsConfig(
        default_agent=metadata.agent_class,
        run_as_openhands=False,
        max_iterations=metadata.max_iterations,
        enable_browser=RUN_WITH_BROWSING,
        runtime='swebench_e2b',  # Force E2B runtime
        sandbox=sandbox_config,
        workspace_base=None,
        workspace_mount_path=None,
    )

    config.set_llm_config(
        update_llm_config_for_completions_logging(
            metadata.llm_config, metadata.eval_output_dir, instance_id
        )
    )
    config.set_llm_config(get_llm_config_arg('draft_editor'), 'draft_editor')

    agent_config = AgentConfig(
        enable_jupyter=False,
        enable_browsing=RUN_WITH_BROWSING,
        enable_llm_editor=ENABLE_LLM_EDITOR,
        enable_mcp=False,
        condenser=metadata.condenser_config,
        enable_prompt_extensions=False,
    )
    config.set_agent_config(agent_config)
    return config


def initialize_runtime_e2b(runtime, instance, metadata: EvalMetadata) -> None:
    """Initialize runtime for E2B environment.

    This is similar to initialize_runtime() but adapted for E2B:
    - Skips testbed Python interpreter check (E2B uses poetry virtualenv)
    - Otherwise follows the same initialization steps
    """
    logger.info('-' * 30)
    logger.info('BEGIN Runtime Initialization Fn (E2B)')
    logger.info('-' * 30)

    # Use the original initialize_runtime but catch the testbed check error
    try:
        initialize_runtime(runtime, instance, metadata)
        # If we get here, initialization completed successfully (including testbed check)
        logger.info('-' * 30)
        logger.info('END Runtime Initialization Fn (E2B)')
        logger.info('-' * 30)
    except Exception as e:
        error_msg = str(e)
        # If it's the testbed check error, we can skip it for E2B
        if 'testbed' in error_msg.lower() and 'python interpreter' in error_msg.lower():
            logger.warning(
                f'Skipping testbed Python check for E2B runtime: {error_msg}. '
                f'E2B uses poetry virtualenv instead of testbed.'
            )
            # Verify Python is available
            # Try to activate testbed if it exists (like Docker does), otherwise use poetry virtualenv
            action = CmdRunAction(
                command='if [ -d /opt/miniconda3 ]; then . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed 2>/dev/null && which python || which python; else which python; fi'
            )
            action.set_hard_timeout(600)
            logger.info(action, extra={'msg_type': 'ACTION'})
            obs = runtime.run_action(action)
            logger.info(obs, extra={'msg_type': 'OBSERVATION'})
            assert_and_raise(
                obs.exit_code == 0,
                f'Python interpreter not found in E2B sandbox: {str(obs)}',
            )
            python_path = obs.content.strip()
            if 'testbed' in python_path:
                logger.info(f'Python interpreter found in testbed: {python_path}')
            else:
                logger.info(f'Python interpreter found (poetry virtualenv): {python_path}')
            logger.info('-' * 30)
            logger.info('END Runtime Initialization Fn (E2B)')
            logger.info('-' * 30)
        else:
            # Re-raise other errors
            raise


def process_single_instance(
    instance,
    metadata: EvalMetadata,
    instance_id: str,
) -> EvalOutput:
    """Process a single SWE-bench instance using E2B runtime.

    This is a simplified version of process_instance() from run_infer.py,
    specifically for E2B runtime.
    """
    logger.info(f'Starting evaluation for instance {instance_id} using E2B runtime.')

    config = get_e2b_config(instance, metadata, instance_id)

    # Create and connect runtime
    runtime = create_runtime(config)
    call_async_from_sync(runtime.connect)

    try:
        # Initialize runtime (sets up workspace, git config, etc.)
        # Use E2B-specific initialization that skips testbed check
        initialize_runtime_e2b(runtime, instance, metadata)

        # Get instruction for the agent
        message_action = get_instruction(instance, metadata)

        # Run the agent
        logger.info(f'Running agent for instance {instance_id}...')
        state = asyncio.run(
            run_controller(
                config=config,
                initial_user_action=message_action,
                runtime=runtime,
                fake_user_response_fn=AGENT_CLS_TO_FAKE_USER_RESPONSE_FN.get(
                    metadata.agent_class
                ),
                headless_mode=True,  # Explicitly set to True to prevent automatic iteration limit increases
            )
        )

        # Check for fatal errors
        if is_fatal_evaluation_error(state.last_error):
            raise Exception('Fatal error detected: ' + state.last_error)

        # Complete runtime and get git patch
        logger.info(f'Completing runtime for instance {instance_id}...')
        return_val = complete_runtime(runtime, instance)
        git_patch = return_val['git_patch']
        logger.info(
            f'Got git diff for instance {instance_id}:\n--------\n{git_patch}\n--------'
        )

    finally:
        runtime.close()

    # Prepare test result
    test_result = {'git_patch': git_patch}

    if state is None:
        raise ValueError('State should not be None.')

    # Convert history to dict format
    histories = [event_to_dict(event) for event in state.history]
    metrics = get_metrics(state)

    # Prepare instruction with image URLs if present
    instruction = message_action.content
    if message_action.image_urls:
        instruction += (
            '\n\n<image_urls>' + '\n'.join(message_action.image_urls) + '</image_urls>'
        )

    # Create output
    output = EvalOutput(
        instance_id=instance_id,
        instruction=instruction,
        instance=instance.to_dict() if hasattr(instance, 'to_dict') else dict(instance),
        test_result=test_result,
        metadata=metadata,
        history=histories,
        metrics=metrics,
        error=state.last_error if state and state.last_error else None,
    )
    return output


def get_completed_instances(output_file: str) -> set[str]:
    """Get set of completed instance IDs from output.jsonl file."""
    completed = set()
    if os.path.exists(output_file):
        try:
            with open(output_file, 'r') as f:
                for line in f:
                    if line.strip():
                        data = json.loads(line)
                        completed.add(data.get('instance_id', ''))
        except Exception as e:
            logger.warning(f'Error reading output file {output_file}: {e}')
    return completed


def process_instance_e2b(
    instance,
    metadata: EvalMetadata,
    use_mp: bool = True,
) -> EvalOutput:
    """Adapter function for process_single_instance to work with run_evaluation.

    This function wraps process_single_instance() to match the signature expected
    by run_evaluation(): (instance, metadata, use_mp) -> EvalOutput

    Args:
        instance: The instance row from the dataset (must have 'instance_id' column)
        metadata: Evaluation metadata
        use_mp: Whether multiprocessing is being used (used to determine if logger should be reset)

    Returns:
        EvalOutput from process_single_instance
    """
    instance_id = instance['instance_id']

    # Set E2B template and instance ID for this specific instance
    # Rule: id_docker_compatible = instance_id.replace("__", "_1776_")
    # Template: openhands-swe-{id_docker_compatible}
    id_docker_compatible = instance_id.replace("__", "_1776_")
    e2b_template = f"openhands-swe-{id_docker_compatible}"
    os.environ['E2B_TEMPLATE'] = e2b_template
    os.environ['SWE_INSTANCE_ID'] = instance_id

    # Configure per-instance logs (always set up logging, not just for multiprocessing)
    # This ensures every instance has its own log file for easier debugging
    log_dir = os.path.join(metadata.eval_output_dir, 'logs')
    reset_logger_for_multiprocessing(logger, instance_id, log_dir)

    logger.info(f'Using E2B template: {e2b_template} for instance: {instance_id}')

    # Call the actual processing function
    return process_single_instance(instance, metadata, instance_id)


def main():
    """Main entry point for E2B-based SWE-bench evaluation."""
    # Check required environment variables
    required_env_vars = ['E2B_API_KEY', 'E2B_WORKSPACE_ID']
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]
    if missing_vars:
        logger.error(
            f'Missing required environment variables: {", ".join(missing_vars)}'
        )
        logger.error(
            'Please set: export E2B_API_KEY="..." and export E2B_WORKSPACE_ID="..."'
        )
        sys.exit(1)

    # Parse arguments
    parser = get_evaluation_parser()
    parser.add_argument(
        '--instance_id',
        type=str,
        default=None,
        help='SWE-bench instance ID to evaluate. If not provided, --instance_list must be provided.',
    )
    parser.add_argument(
        '--instance_list',
        type=str,
        default=None,
        help='Path to a text file containing instance IDs (one per line), or comma-separated list of instance IDs. '
             'If provided, will process all instances in the list.',
    )
    parser.add_argument(
        '--split',
        type=str,
        default='dev',
        help='Dataset split to use (default: dev)',
    )
    parser.add_argument(
        '--dataset',
        type=str,
        default='princeton-nlp/SWE-bench',
        help='Dataset to use (default: princeton-nlp/SWE-bench)',
    )

    args = parser.parse_args()

    # Set dataset type
    set_dataset_type(args.dataset)

    # Load dataset
    logger.info(f'Loading dataset {args.dataset} with split {args.split}...')
    dataset = load_dataset(args.dataset, split=args.split)
    df = dataset.to_pandas()

    # Determine instance list
    instance_ids = []
    if args.instance_list:
        # Check if it's a file path
        if os.path.isfile(args.instance_list):
            with open(args.instance_list, 'r') as f:
                instance_ids = [line.strip() for line in f if line.strip()]
        else:
            # Treat as comma-separated list
            instance_ids = [id.strip() for id in args.instance_list.split(',') if id.strip()]
    elif args.instance_id:
        instance_ids = [args.instance_id]
    else:
        logger.error('Either --instance_id or --instance_list must be provided')
        sys.exit(1)

    logger.info(f'Processing {len(instance_ids)} instance(s): {instance_ids}')

    # Filter dataset to only include requested instances
    instances_df = df[df['instance_id'].isin(instance_ids)].copy()

    if len(instances_df) == 0:
        logger.error('No matching instances found in dataset')
        sys.exit(1)

    if len(instances_df) < len(instance_ids):
        found_ids = set(instances_df['instance_id'].tolist())
        missing_ids = set(instance_ids) - found_ids
        logger.warning(f'Some instances not found in dataset: {missing_ids}')

    # Get LLM config
    if not args.llm_config:
        logger.error('--llm_config is required')
        sys.exit(1)

    llm_config = get_llm_config_arg(args.llm_config)
    if llm_config is None:
        logger.error(f'Could not find LLM config: {args.llm_config}')
        sys.exit(1)

    llm_config.log_completions = True
    llm_config.modify_params = False

    # Get condenser config
    condenser_name = os.environ.get('EVAL_CONDENSER')
    if condenser_name:
        condenser_config = get_condenser_config_arg(condenser_name)
        if condenser_config is None:
            logger.error(
                f'Could not find Condenser config: EVAL_CONDENSER={condenser_name}'
            )
            sys.exit(1)
    else:
        condenser_config = NoOpCondenserConfig()
        logger.debug(
            'No Condenser config provided via EVAL_CONDENSER, using NoOpCondenser.'
        )

    # Create metadata
    details = {'mode': 'swe'}  # Default mode
    dataset_description = (
        args.dataset.replace('/', '__') + '-' + args.split.replace('/', '__')
    )
    metadata = make_metadata(
        llm_config,
        dataset_description,
        args.agent_cls,
        args.max_iterations,
        args.eval_note,
        args.eval_output_dir,
        details=details,
        condenser_config=condenser_config,
    )

    # Prepare output file
    output_file = os.path.join(metadata.eval_output_dir, 'output.jsonl')
    os.makedirs(metadata.eval_output_dir, exist_ok=True)

    # Prepare dataset (filter out already completed instances)
    instances_df = prepare_dataset(
        instances_df,
        output_file,
        args.eval_n_limit,
        eval_ids=instance_ids if instance_ids else None,
    )

    if len(instances_df) == 0:
        logger.info('All instances have already been completed.')
        return

    # Print list of instances to be processed for better visibility
    instance_list = instances_df['instance_id'].tolist()
    logger.info(f'=' * 80)
    logger.info(f'Running evaluation for {len(instances_df)} instance(s) with {args.eval_num_workers} worker(s)')
    logger.info(f'Instances to process: {instance_list}')
    logger.info(f'Output file: {output_file}')
    logger.info(f'Log directory: {os.path.join(metadata.eval_output_dir, "logs")}')
    logger.info(f'=' * 80)

    # Show completed instances before starting
    completed_before = get_completed_instances(output_file)
    if completed_before:
        logger.info(f'Already completed instances ({len(completed_before)}): {sorted(completed_before)}')

    # Run evaluation with parallel processing support
    run_evaluation(
        instances_df,
        metadata,
        output_file,
        args.eval_num_workers,  # Use the --eval-num-workers parameter
        process_instance_e2b,    # Use the adapter function
        max_retries=5,
        timeout_seconds=8 * 60 * 60,  # 8 hours per instance
    )

    # Show completed instances after finishing
    completed_after = get_completed_instances(output_file)
    newly_completed = completed_after - completed_before
    logger.info(f'=' * 80)
    logger.info(f'Evaluation completed')
    logger.info(f'Total completed instances: {len(completed_after)}')
    if newly_completed:
        logger.info(f'Newly completed instances ({len(newly_completed)}): {sorted(newly_completed)}')
    if instance_list:
        remaining = set(instance_list) - completed_after
        if remaining:
            logger.warning(f'Remaining instances ({len(remaining)}): {sorted(remaining)}')
    logger.info(f'Output saved to: {output_file}')
    logger.info(f'=' * 80)


if __name__ == '__main__':
    main()

