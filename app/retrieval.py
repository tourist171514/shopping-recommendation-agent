"""商品库与结构化检索层（纯代码，0 token 消耗）。

设计依据（docs/02_技术选型.md §3）：
- 商品规模小（子集 96 条，全集数千条），检索键全部是结构化字段；
- description 为占位文本无语义 → 不做向量/语义检索；
- 本层对外只暴露 ProductStore.search()，将来换 SQLite 只需替换本模块。

核心能力：
1. 硬约束过滤：品类 / 价格上限 / 厂商 / 主题标签（多标签按 OR 命中）；
2. 软偏好加分：如 "prefer Bayer-and-Sons if available"，只加分不淘汰；
3. 逐约束证据：每个候选返回"哪个约束满足/未满足"，支撑 F4 约束检查；
4. 零结果诊断：逐步收敛链 + 逐约束放宽（leave-one-out）+ 最近价格提示，
   支撑 F8 主动追问时的"放宽建议"。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# 结构化约束
# ---------------------------------------------------------------------------

@dataclass
class Constraints:
    """从自然语言需求解析出的结构化购物约束。

    硬约束（不满足即淘汰）：item_type / tags / manufacturer / max_price
    软偏好（只加分不淘汰）：prefer_manufacturer
    """
    item_type: str | None = None
    tags: list[str] = field(default_factory=list)
    manufacturer: str | None = None
    max_price: float | None = None
    prefer_manufacturer: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "Constraints":
        """容错解析：缺省字段、字符串/列表混用、大小写与空白都不敏感。"""
        tags = raw.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        price = raw.get("max_price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None

        def clean(v):
            return v.strip() if isinstance(v, str) and v.strip() else None

        return cls(
            item_type=clean(raw.get("item_type")),
            tags=[t.strip() for t in tags if isinstance(t, str) and t.strip()],
            manufacturer=clean(raw.get("manufacturer")),
            max_price=price,
            prefer_manufacturer=clean(raw.get("prefer_manufacturer")),
        )

    def describe(self) -> str:
        """人类可读的约束摘要（用于诊断与展示）。"""
        parts = []
        if self.item_type:
            parts.append(f"品类={self.item_type}")
        if self.tags:
            parts.append("主题包含 " + "/".join(self.tags))
        if self.manufacturer:
            parts.append(f"厂商={self.manufacturer}")
        if self.max_price is not None:
            parts.append(f"价格≤${self.max_price:g}")
        if self.prefer_manufacturer:
            parts.append(f"(软偏好)优先厂商={self.prefer_manufacturer}")
        return "；".join(parts) if parts else "无约束"


# ---------------------------------------------------------------------------
# 检索结果
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """单个候选商品：原始数据 + 得分 + 逐约束匹配证据。"""
    product: dict
    score: float
    matched: dict            # {"item_type": bool, "tags_matched": [...], ...}
    reasons: list[str]       # 人类可读的匹配/加分理由

    def to_llm_dict(self) -> dict:
        """喂给 LLM 的精简表示：刻意不含无语义的 description（token 优化）。"""
        p = self.product
        return {
            "product_id": p["product_id"],
            "name": p["name"],
            "item_type": p["item_type"],
            "manufacturer": p["manufacturer"],
            "price": p["price"],
            "tags": p["tags"],
            "match": {
                "score": self.score,
                "tags_matched": self.matched.get("tags_matched", []),
                "tags_missed": self.matched.get("tags_missed", []),
                "manufacturer_ok": self.matched.get("manufacturer"),
                "prefer_manufacturer_ok": self.matched.get("prefer_manufacturer"),
                "reasons": self.reasons,
            },
        }


@dataclass
class SearchResult:
    constraints: Constraints
    candidates: list[Candidate]
    total_products: int
    diagnosis: dict | None = None   # 零结果时非空

    @property
    def is_empty(self) -> bool:
        return not self.candidates

    def to_llm_dict(self, top_k: int = 5) -> dict:
        """工具输出序列化：候选列表 + 诊断信息。"""
        out = {
            "constraints_applied": self.constraints.describe(),
            "total_products_in_store": self.total_products,
            "num_candidates": len(self.candidates),
            "candidates": [c.to_llm_dict() for c in self.candidates[:top_k]],
        }
        if self.diagnosis:
            out["no_match_diagnosis"] = self.diagnosis
        return out


# ---------------------------------------------------------------------------
# 商品库
# ---------------------------------------------------------------------------

class ProductStore:
    """内存商品库 + 结构化检索。"""

    def __init__(self, products: list[dict]):
        self.products = products
        self.by_id: dict[str, dict] = {p["product_id"]: p for p in products}

    # ---- 基础查询 ----

    def get_product(self, product_id: str) -> dict | None:
        return self.by_id.get(product_id)

    # ---- 匹配原语 ----

    @staticmethod
    def _tag_hits(product: dict, keywords: list[str]) -> tuple[list[str], list[str]]:
        """关键词对标签与名称做大小写不敏感的包含匹配。返回 (命中, 未命中)。"""
        hay_tags = [t.lower() for t in product.get("tags", [])]
        name = product.get("name", "").lower()
        hit, missed = [], []
        for kw in keywords:
            k = kw.strip().lower()
            if any(k in t for t in hay_tags) or (k and k in name):
                hit.append(kw)
            else:
                missed.append(kw)
        return hit, missed

    @staticmethod
    def _manufacturer_ok(product: dict, manufacturer: str) -> bool:
        return product.get("manufacturer", "").strip().lower() == manufacturer.strip().lower()

    def _satisfies(self, product: dict, c: Constraints, skip: set[str] | None = None) -> bool:
        """硬约束校验。skip 用于诊断时"假设放宽某一个约束"。"""
        skip = skip or set()
        if "item_type" not in skip and c.item_type \
                and product.get("item_type", "").lower() != c.item_type.strip().lower():
            return False
        if "max_price" not in skip and c.max_price is not None \
                and product.get("price", float("inf")) > c.max_price:
            return False
        if "manufacturer" not in skip and c.manufacturer \
                and not self._manufacturer_ok(product, c.manufacturer):
            return False
        if "tags" not in skip and c.tags:
            hit, _ = self._tag_hits(product, c.tags)
            if not hit:
                return False
        return True

    # ---- 主检索 ----

    def search(self, constraints: Constraints | dict, top_k: int = 5) -> SearchResult:
        """结构化过滤 → 打分排序 → Top-K。零结果时附带诊断。"""
        c = constraints if isinstance(constraints, Constraints) else Constraints.from_dict(constraints)

        survivors = [p for p in self.products if self._satisfies(p, c)]

        if not survivors:
            return SearchResult(
                constraints=c, candidates=[], total_products=len(self.products),
                diagnosis=self._diagnose(c),
            )

        candidates = [self._score(p, c) for p in survivors]
        # 确定性排序：得分降序 → 价格升序 → ID 升序
        candidates.sort(key=lambda cd: (-cd.score, cd.product["price"], cd.product["product_id"]))
        return SearchResult(constraints=c, candidates=candidates, total_products=len(self.products))

    def _score(self, product: dict, c: Constraints) -> Candidate:
        """匹配打分：标签命中 +2/个，硬厂商匹配 +3，软偏好命中 +2。"""
        score = 0.0
        matched: dict = {}
        reasons: list[str] = []

        if c.tags:
            hit, missed = self._tag_hits(product, c.tags)
            matched["tags_matched"], matched["tags_missed"] = hit, missed
            score += 2.0 * len(hit)
            if hit:
                reasons.append("主题命中: " + ", ".join(hit))
        if c.item_type:
            matched["item_type"] = True
        if c.max_price is not None:
            matched["max_price"] = True
            reasons.append(f"价格 ${product['price']:g} ≤ ${c.max_price:g}")
        if c.manufacturer:
            ok = self._manufacturer_ok(product, c.manufacturer)
            matched["manufacturer"] = ok
            if ok:
                score += 3.0
                reasons.append(f"厂商精确匹配: {product['manufacturer']}")
        if c.prefer_manufacturer:
            ok = self._manufacturer_ok(product, c.prefer_manufacturer)
            matched["prefer_manufacturer"] = ok
            if ok:
                score += 2.0
                reasons.append(f"软偏好命中: 优先厂商 {product['manufacturer']}")

        return Candidate(product=product, score=score, matched=matched, reasons=reasons)

    # ---- 零结果诊断（支撑 F8 主动追问的放宽建议）----

    def _diagnose(self, c: Constraints) -> dict:
        """回答"为什么一个候选都没有"以及"放宽哪个约束能救回来"。

        1. 逐步收敛链：按顺序施加硬约束，记录每步剩余数量 → 定位瓶颈；
        2. 逐约束放宽（leave-one-out）：单独去掉每个约束后的候选数；
        3. 若瓶颈是价格：给出满足其余约束的最低价，建议提高预算。
        """
        # 只收集实际生效的硬约束（按需构造描述，避免对 None 做格式化）
        active: list[tuple[str, str]] = []
        if c.item_type:
            active.append(("item_type", f"品类={c.item_type}"))
        if c.tags:
            active.append(("tags", "主题=" + "/".join(c.tags)))
        if c.manufacturer:
            active.append(("manufacturer", f"厂商={c.manufacturer}"))
        if c.max_price is not None:
            active.append(("max_price", f"价格≤${c.max_price:g}"))

        chain, bottleneck = [], None
        applied: set[str] = set()
        for key, desc in active:
            applied.add(key)
            # 过滤"已施加约束集合"之外的约束全部放宽
            skip_keys = {k for k, _ in active} - applied
            pool = [p for p in self.products if self._satisfies(p, c, skip=skip_keys)]
            chain.append({"applied": desc, "remaining": len(pool)})
            if not pool:
                bottleneck = key
                break

        leave_one_out = {}
        for key, desc in active:
            pool = [p for p in self.products if self._satisfies(p, c, skip={key})]
            leave_one_out[desc] = len(pool)

        suggestions = [f"若放宽「{desc}」，有 {n} 个候选" for desc, n in leave_one_out.items() if n > 0]
        nearest_price = None
        if bottleneck == "max_price" or (c.max_price is not None and leave_one_out):
            others = [p for p in self.products if self._satisfies(p, c, skip={"max_price"})]
            if others:
                nearest_price = min(p["price"] for p in others)
                suggestions.insert(0, f"满足其余条件的最低价为 ${nearest_price:g}，可考虑提高预算")

        return {
            "total_products": len(self.products),
            "constraint_chain": chain,
            "bottleneck": bottleneck,
            "leave_one_out": leave_one_out,
            "nearest_price_if_relax_budget": nearest_price,
            "suggestions": suggestions,
        }


# ---------------------------------------------------------------------------
# 加载入口
# ---------------------------------------------------------------------------

def load_store(data_dir: str | Path) -> ProductStore:
    path = Path(data_dir) / "products.jsonl"
    with path.open("r", encoding="utf-8") as f:
        products = [json.loads(line) for line in f if line.strip()]
    return ProductStore(products)
