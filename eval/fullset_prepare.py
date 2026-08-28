"""全集数据准备：上游 1740 条 → 本项目 schema + 合成全集任务。

上游仓库（stockholmux/ecommerce-sample-set）的 items.json 字段与材料包
products.jsonl 同源（材料包即其种子抽样），故转换为无损投影：
    product_id = F{序号}, name/item_type/manufacturer/price/tags/description 直接映射

任务集：上游不含任务文件（材料包的 50 条为出题方生成），此处按实测的
3 种固定句式模板合成 150 条（每种 50 条），全部锚定真实存在的商品属性，
用于检索层与 token 消耗的全集验证（学长建议："找全集来测，会准一点"）。

运行：PYTHONIOENCODING=utf-8 python -m eval.fullset_prepare
输出：outputs/fullset/full_products.jsonl, outputs/fullset/full_tasks.jsonl
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import config

SRC = config.OUTPUT_DIR / "fullset"
SEED = 20260705  # 与材料包 metadata.json 的种子一致


def convert_products() -> list[dict]:
    items = json.loads((SRC / "items.json").read_text(encoding="utf-8"))
    products = []
    for i, it in enumerate(items):
        products.append({
            "product_id": f"F{i:05d}",
            "name": it["name"],
            "item_type": it["itemType"],
            "manufacturer": it["manufacturer"],
            "price": it["price"],
            "tags": it["tags"],
            "description": it["description"],
        })
    out = SRC / "full_products.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for p in products:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"商品转换完成: {len(products)} 条 -> {out}")
    return products


def make_tasks(products: list[dict], per_template: int = 50) -> list[dict]:
    rng = random.Random(SEED)
    by_tag: dict[str, list[dict]] = {}
    for p in products:
        for t in p["tags"]:
            by_tag.setdefault(t, []).append(p)

    tasks, tid = [], 0

    def add(instruction: str):
        nonlocal tid
        tasks.append({"task_id": f"F{tid:04d}", "instruction": instruction})
        tid += 1

    tag_pool = sorted(by_tag.keys())
    mfrs = sorted({p["manufacturer"] for p in products})

    # T1: Find a X about TAG from MFR with price under $P
    for _ in range(per_template):
        tag = rng.choice(tag_pool)
        p = rng.choice(by_tag[tag])
        budget = p["price"] + rng.choice([1, 2, 3, 5])
        add(f"Find a {p['item_type']} about {tag} from {p['manufacturer']} "
            f"with price under ${budget:g}.")

    # T2: I need a TAG themed X that costs less than $P
    for _ in range(per_template):
        tag = rng.choice(tag_pool)
        p = rng.choice(by_tag[tag])
        budget = p["price"] + rng.choice([1, 2, 3, 5])
        add(f"I need a {tag} themed {p['item_type']} that costs less than ${budget:g}.")

    # T3: Buy an affordable X related to TAG; prefer MFR if available
    for _ in range(per_template):
        tag = rng.choice(tag_pool)
        p = rng.choice(by_tag[tag])
        prefer = p["manufacturer"] if rng.random() < 0.5 else rng.choice(mfrs)
        add(f"Buy an affordable {p['item_type']} related to {tag}; "
            f"prefer {prefer} if available.")

    out = SRC / "full_tasks.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for t in tasks:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print(f"任务合成完成: {len(tasks)} 条 -> {out}")
    return tasks


if __name__ == "__main__":
    prods = convert_products()
    make_tasks(prods)
