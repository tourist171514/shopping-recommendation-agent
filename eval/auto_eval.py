"""自动评估：题目无官方指标，此处自建"硬约束满足率"（需求验收标准 2）。

任务文本为 3 种固定句式（实测 17/17/16 分布），用规则解析出期望约束：
  T1: Find a X about TAG from MFR with price under $P  → 硬: 类型+标签+厂商+价格
  T2: I need a TAG themed X that costs less than $P    → 硬: 类型+标签+价格
  T3: Buy an affordable X related to TAG; prefer MFR.. → 硬: 类型+标签；软: 厂商偏好

评估逻辑（关键：先判可行性，再判行为正确性）：
- 期望约束在商品库中可行（存在满足全部硬约束的商品）→ Agent 应购买且买对；
- 期望约束不可行（没有任何商品满足）→ Agent 正确地不购买（追问/说明）也算对。
这样避免把"诚实拒绝不可能任务"误判为失败。

运行：PYTHONIOENCODING=utf-8 python -m eval.auto_eval
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import config
from app.retrieval import load_store

P1 = re.compile(r"^Find an? (shirt|mug) about (.+?) from (.+?) with price under \$(\d+(?:\.\d+)?)\.$")
P2 = re.compile(r"^I need an? (.+?) themed (shirt|mug) that costs less than \$(\d+(?:\.\d+)?)\.$")
P3 = re.compile(r"^Buy an affordable (shirt|mug) related to (.+?); prefer (.+?) if available\.$")


def parse_expectation(instruction: str) -> dict | None:
    m = P1.match(instruction)
    if m:
        return {"pattern": "T1", "item_type": m.group(1), "tag": m.group(2),
                "manufacturer": m.group(3), "max_price": float(m.group(4)),
                "soft_manufacturer": None}
    m = P2.match(instruction)
    if m:
        return {"pattern": "T2", "item_type": m.group(2), "tag": m.group(1),
                "manufacturer": None, "max_price": float(m.group(3)),
                "soft_manufacturer": None}
    m = P3.match(instruction)
    if m:
        return {"pattern": "T3", "item_type": m.group(1), "tag": m.group(2),
                "manufacturer": None, "max_price": None,
                "soft_manufacturer": m.group(3)}
    return None


def tag_hit(product: dict, tag: str) -> bool:
    k = tag.strip().lower()
    return (any(k in t.lower() for t in product.get("tags", []))
            or k in product.get("name", "").lower())


def satisfies_hard(product: dict, exp: dict) -> bool:
    if product.get("item_type", "").lower() != exp["item_type"].lower():
        return False
    if not tag_hit(product, exp["tag"]):
        return False
    if exp["manufacturer"] and \
            product.get("manufacturer", "").lower() != exp["manufacturer"].strip().lower():
        return False
    if exp["max_price"] is not None and product.get("price", 0) > exp["max_price"]:
        return False
    return True


def main() -> None:
    store = load_store(config.DATA_DIR)
    results_path = config.OUTPUT_DIR / "batch_results.jsonl"
    rows = [json.loads(l) for l in results_path.open(encoding="utf-8") if l.strip()]

    n_feasible_correct = n_infeasible_correct = 0
    n_feasible = n_infeasible = n_unparsed = 0
    soft_total = soft_hit = 0
    failures: list[str] = []

    for row in rows:
        instruction = row["instruction"]
        exp = parse_expectation(instruction)
        if exp is None:
            n_unparsed += 1
            continue
        feasible_products = [p for p in store.products if satisfies_hard(p, exp)]
        purchased = store.get_product(row.get("purchased_product_id") or "")

        if feasible_products:
            n_feasible += 1
            if purchased and satisfies_hard(purchased, exp):
                n_feasible_correct += 1
                if exp["soft_manufacturer"]:
                    soft_total += 1
                    if purchased["manufacturer"].lower() == exp["soft_manufacturer"].lower():
                        soft_hit += 1
            else:
                got = purchased["product_id"] if purchased else "None(未购买)"
                failures.append(f"{row['task_id']} [{exp['pattern']}] 可行但决策错误: {got} | {instruction}")
        else:
            n_infeasible += 1
            if not purchased:
                n_infeasible_correct += 1
            else:
                failures.append(f"{row['task_id']} [{exp['pattern']}] 不可行却购买了: "
                                f"{purchased['product_id']} | {instruction}")

    total = n_feasible + n_infeasible
    overall = n_feasible_correct + n_infeasible_correct
    print("===== 硬约束满足率评估 =====")
    print(f"可解析任务: {total}/{len(rows)} (未解析 {n_unparsed})")
    print(f"可行任务: {n_feasible} | 买对: {n_feasible_correct} "
          f"→ 可行满足率 {n_feasible_correct / max(n_feasible, 1):.1%}")
    print(f"不可行任务: {n_infeasible} | 正确拒绝: {n_infeasible_correct}")
    print(f"总体正确率: {overall}/{total} = {overall / max(total, 1):.1%}")
    if soft_total:
        print(f"软偏好命中(参考): {soft_hit}/{soft_total}")
    if failures:
        print("\n----- 错误明细 -----")
        for f in failures:
            print(" ", f)
    else:
        print("\n无决策错误。")


if __name__ == "__main__":
    main()
