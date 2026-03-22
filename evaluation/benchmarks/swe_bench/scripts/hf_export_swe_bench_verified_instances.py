#!/usr/bin/env python3
"""从 Hugging Face 导出 princeton-nlp/SWE-bench_Verified 的 instance_id 列表到文本文件。

每行一个 instance_id，与 OpenHands 中 `instance_list.txt` 的常见格式一致。

依赖: `pip install datasets`，或在仓库内执行 `poetry install --with evaluation`（evaluation 组已包含 datasets）。

用法:
  python evaluation/benchmarks/swe_bench/scripts/hf_export_swe_bench_verified_instances.py
  python .../hf_export_swe_bench_verified_instances.py -o my_instances.txt --split test

可选: 设置环境变量 HF_TOKEN 可提高 Hugging Face 下载速率。
"""

from __future__ import annotations

import argparse
import sys

from datasets import load_dataset


def main() -> int:
    parser = argparse.ArgumentParser(
        description='从 Hugging Face 下载 SWE-bench_Verified 并写出 instance_id 列表'
    )
    parser.add_argument(
        '--dataset',
        default='princeton-nlp/SWE-bench_Verified',
        help='Hugging Face 数据集名称',
    )
    parser.add_argument(
        '--split',
        default='test',
        help='要导出的 split（Verified 通常为 test）',
    )
    parser.add_argument(
        '-o',
        '--output',
        default='swe_bench_verified_instance_list.txt',
        help='输出文件路径（每行一个 instance_id）',
    )
    args = parser.parse_args()

    print(f'Loading {args.dataset} split={args.split!r} ...', file=sys.stderr)
    ds = load_dataset(args.dataset, split=args.split)

    if 'instance_id' not in ds.column_names:
        print(
            f'error: column instance_id missing; columns={ds.column_names}',
            file=sys.stderr,
        )
        return 1

    ids = [str(x) for x in ds['instance_id']]
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write('\n'.join(ids))
        if ids:
            f.write('\n')

    print(f'wrote {len(ids)} instance_id lines to {args.output!r}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
