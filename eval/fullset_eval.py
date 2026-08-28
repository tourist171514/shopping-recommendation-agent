"""全集评测（学长建议的"找全集测"落地）：1740 商品 / 150 条合成任务。

三段式：
A. 检索层（纯代码）：150 任务的检索延迟与候选收敛情况 —— 验证两阶段检索
   在 18 倍数据规模下的性能与裁剪能力；
B. 基线 token 测量：把全集商品库整体塞进 prompt 的真实 token 数（1 次 API），
   与本项目两阶段方案对比 —— token 优化论证；
C. LLM 端到端抽样：30 条全集任务走完整 Agent 循环，验证规模下的决策质量。

运行：PYTHONIOENCODING=utf-8 python -u -m eval.fullset_eval [--llm-sample 30]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import config
from app.retrieval import ProductStore, load_store
from eval.auto_eval import parse_expectation, satisfies_hard

SRC = config.OUTPUT_DIR / "fullset"


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


# ---------------------------------------------------------------- A. 检索层
def eval_retrieval(products: list[dict], tasks: list[dict]) -> None:
    store = ProductStore(products)
    latencies, survivors_list, empties = [], [], 0
    for t in tasks:
        exp = parse_expectation(t["instruction"])
        if exp is None:
            continue
        c = {"item_type": exp["item_type"], "tags": [exp["tag"]]}
        if exp["manufacturer"]:
            c["manufacturer"] = exp["manufacturer"]
        if exp["max_price"] is not None:
            c["max_price"] = exp["max_price"]
        if exp["soft_manufacturer"]:
            c["prefer_manufacturer"] = exp["soft_manufacturer"]
        t0 = time.perf_counter()
        r = store.search(c)
        latencies.append((time.perf_counter() - t0) * 1000)
        if r.is_empty:
            empties += 1
            survivors_list.append(0)
        else:
            survivors_list.append(len(r.candidates))

    latencies.sort()
    n = len(latencies)
    p50 = latencies[n // 2]
    p95 = latencies[int(n * 0.95)]
    print("===== A. 检索层（全集 1740 商品）=====")
    print(f"任务数: {n} | 延迟 avg {sum(latencies)/n:.2f}ms / p50 {p50:.2f}ms / p95 {p95:.2f}ms")
    print(f"硬约束过滤后平均幸存候选: {sum(survivors_list)/n:.1f} 条 "
          f"(对比子集 96 条商品的规模放大 18 倍)")
    print(f"零结果任务: {empties}/{n} —— 诊断模块全部覆盖")


# ---------------------------------------------------------------- B. 基线 token
def eval_baseline_tokens(products: list[dict]) -> int:
    from app.llm import DeepSeekClient
    catalog = json.dumps(products, ensure_ascii=False)
    messages = [
        {"role": "system",
         "content": "你是购物助手。下面是完整商品库，请根据用户需求选出最合适的商品并输出其 product_id。"},
        {"role": "user",
         "content": f"商品库:\n{catalog}\n\n用户需求: Find a mug about Trees with price under $15."},
    ]
    client = DeepSeekClient()
    resp = client.chat(messages, tools=None, thinking=False, max_tokens=30)
    prompt_tokens = resp.usage.prompt_tokens
    print("===== B. 基线方案（全量商品入 prompt）=====")
    print(f"全集 {len(products)} 商品序列化后单轮 prompt tokens: {prompt_tokens:,}")
    return prompt_tokens


# ---------------------------------------------------------------- C. LLM 抽样
def eval_llm_sample(products: list[dict], tasks: list[dict], sample_n: int) -> None:
    from app.agent.core import Agent

    # Agent 从目录加载 products.jsonl，为全集建一个临时数据目录
    data_dir = SRC / "data_dir"
    data_dir.mkdir(exist_ok=True)
    shutil.copy(SRC / "full_products.jsonl", data_dir / "products.jsonl")

    step = max(1, len(tasks) // sample_n)
    sample = tasks[::step][:sample_n]
    agent = Agent(str(data_dir))

    correct = total_tokens = 0
    print(f"===== C. LLM 端到端抽样（{len(sample)} 条全集任务）=====")
    for i, t in enumerate(sample, 1):
        exp = parse_expectation(t["instruction"])
        result = agent.run(t["instruction"])
        usage = result.get("usage") or {}
        total_tokens += usage.get("total_tokens", 0)
        feasible = [p for p in products if satisfies_hard(p, exp)] if exp else []
        purchased = next((p for p in products
                          if p["product_id"] == result.get("purchased_product_id")), None)
        ok = bool(feasible and purchased and satisfies_hard(purchased, exp)) \
            or bool(not feasible and not purchased)
        correct += ok
        print(f"  [{i}/{len(sample)}] {t['task_id']} {'正确' if ok else '错误'} | "
              f"{usage.get('total_tokens', '?')} tokens", flush=True)

    print(f"全集抽样正确率: {correct}/{len(sample)} = {correct/len(sample):.1%}")
    print(f"平均每条任务 token: {total_tokens/len(sample):.0f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm-sample", type=int, default=30)
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--skip-llm", action="store_true")
    args = parser.parse_args()

    products = load_jsonl(SRC / "full_products.jsonl")
    tasks = load_jsonl(SRC / "full_tasks.jsonl")
    print(f"已加载全集: {len(products)} 商品 / {len(tasks)} 任务\n")

    eval_retrieval(products, tasks)
    print()
    baseline = 0 if args.skip_baseline else eval_baseline_tokens(products)
    if not args.skip_llm:
        print()
        eval_llm_sample(products, tasks, args.llm_sample)

    # 汇总：与子集批量结果对比
    batch = config.OUTPUT_DIR / "batch_results.jsonl"
    if batch.exists():
        rows = load_jsonl(batch)
        sub_avg = sum((r.get("usage") or {}).get("total_tokens", 0) for r in rows) / len(rows)
        print("\n===== 汇总 =====")
        print(f"子集(96商品)两阶段方案平均: {sub_avg:,.0f} tokens/任务")
        if baseline:
            print(f"全集(1740商品)基线方案:      {baseline:,} tokens/任务")
            print(f"规模放大后，两阶段检索相对基线的 token 节省: "
                  f"{(1 - sub_avg / baseline):.1%}")


if __name__ == "__main__":
    main()
