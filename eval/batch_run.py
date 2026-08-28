"""批量评测：对全部公开任务运行 Agent，落盘结果与统计。

运行：PYTHONIOENCODING=utf-8 python -m eval.batch_run [--limit 50]
输出：outputs/batch_results.jsonl（逐条写入，中断可续看已有部分）

与 starter/simulate_agent.py 的关系：输出保持其字段约定
（task_id/validation_errors/instruction/purchased_product_id/trace/summary），
另加 usage/rounds_used 等扩展字段用于本报告的成本分析。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import config
from app.agent.core import Agent

REQUIRED_KEYS = {"instruction", "purchased_product_id", "trace", "summary"}


def read_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def validate_result(result: dict) -> list[str]:
    """复用 starter 的输出结构校验逻辑。"""
    errors = []
    if not isinstance(result, dict):
        return ["result must be a dict"]
    missing = sorted(REQUIRED_KEYS - set(result))
    if missing:
        errors.append(f"missing keys: {missing}")
    if "trace" in result and not isinstance(result["trace"], list):
        errors.append("trace must be a list")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0,
                        help="从第 N 条任务开始（断点续跑；>0 时结果以追加模式写入）")
    parser.add_argument("--out", default=str(config.OUTPUT_DIR / "batch_results.jsonl"))
    args = parser.parse_args()

    all_tasks = read_jsonl(config.DATA_DIR / "tasks.jsonl")
    tasks = all_tasks[args.offset: args.offset + args.limit]
    agent = Agent(config.DATA_DIR)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total_tokens = total_latency = 0.0
    t_start = time.time()
    mode = "a" if args.offset else "w"
    with out_path.open(mode, encoding="utf-8") as f:
        for i, task in enumerate(tasks, 1):
            t0 = time.time()
            try:
                result = agent.run(task["instruction"])
                errors = validate_result(result)
            except Exception as e:  # noqa: BLE001
                result = {"instruction": task["instruction"], "error": str(e)}
                errors = [f"exception: {e}"]
            elapsed = time.time() - t0
            usage = result.get("usage") or {}
            total_tokens += usage.get("total_tokens", 0)
            total_latency += usage.get("latency_seconds", 0.0)
            row = {"task_id": task["task_id"], "validation_errors": errors,
                   "wall_seconds": round(elapsed, 2), **result}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            print(f"[{args.offset + i}/{len(all_tasks)}] {task['task_id']} -> "
                  f"{result.get('purchased_product_id')} | "
                  f"{usage.get('total_tokens', '?')} tokens | {elapsed:.1f}s"
                  f"{'  [校验失败]' if errors else ''}", flush=True)

    n = len(tasks)
    print("\n===== 批量统计 =====")
    print(f"任务数: {n}")
    print(f"总 token: {int(total_tokens)} | 平均每任务: {total_tokens / n:.0f}")
    print(f"模型累计耗时: {total_latency:.1f}s | 平均每任务: {total_latency / n:.2f}s")
    print(f"墙钟总耗时: {time.time() - t_start:.1f}s")
    print(f"结果文件: {out_path}")


if __name__ == "__main__":
    main()
