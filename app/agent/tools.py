"""工具定义与执行（OpenAI function calling 规范）。

最小工具集（技术选型 §2）：
- search_products：结构化检索 + 逐约束证据 + 零结果诊断；
- get_product_detail：单商品详情，支撑"第二个看看"这类指代式追问。
"""
from __future__ import annotations

from app import config

SEARCH_PRODUCTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_products",
        "description": (
            "按结构化约束检索商品库，返回候选列表（含每个约束的匹配证据）；"
            "无结果时返回诊断与放宽建议。需要找商品时必须调用此工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item_type": {
                    "type": "string", "enum": ["shirt", "mug"],
                    "description": "商品品类（硬约束）",
                },
                "tags": {
                    "type": "array", "items": {"type": "string"},
                    "description": "主题关键词，如 Barn / Ocean / Sunny（硬约束）",
                },
                "manufacturer": {
                    "type": "string",
                    "description": "制造商名称（硬约束，须与用户提到的完全一致）",
                },
                "max_price": {
                    "type": "number",
                    "description": "价格上限，美元（硬约束；用户未明确预算时不要编造）",
                },
                "prefer_manufacturer": {
                    "type": "string",
                    "description": "软偏好：用户说 prefer X if available 时填这里，不作为硬性过滤条件",
                },
            },
            "required": [],
        },
    },
}

GET_PRODUCT_DETAIL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_product_detail",
        "description": "按商品 ID 查询单个商品的完整信息。用户指代某个候选（如'第二个'）时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "description": "商品 ID，如 P0000"},
            },
            "required": ["product_id"],
        },
    },
}

TOOL_SCHEMAS = [SEARCH_PRODUCTS_SCHEMA, GET_PRODUCT_DETAIL_SCHEMA]


class ToolExecutor:
    """工具执行器：名称 → 检索层/商品库的实际调用。"""

    def __init__(self, store):
        self.store = store

    def execute(self, name: str, arguments: dict) -> dict:
        if name == "search_products":
            result = self.store.search(arguments, top_k=config.TOP_K)
            return result.to_llm_dict(top_k=config.TOP_K)
        if name == "get_product_detail":
            pid = str(arguments.get("product_id", "")).strip()
            product = self.store.get_product(pid)
            if product is None:
                return {"found": False, "product_id": pid,
                        "hint": "商品不存在，ID 必须来自 search_products 的结果"}
            # 同样剔除无语义的 description，控制 token
            compact = {k: v for k, v in product.items() if k != "description"}
            return {"found": True, **compact}
        return {"error": f"未知工具: {name}"}

    @staticmethod
    def summarize(name: str, result: dict) -> dict:
        """trace 用的紧凑摘要（完整结果在 messages 里，只给模型看）。"""
        if name == "search_products":
            diag = result.get("no_match_diagnosis") or {}
            return {
                "num_candidates": result.get("num_candidates"),
                "top_ids": [c["product_id"] for c in result.get("candidates", [])],
                "bottleneck": diag.get("bottleneck"),
            }
        if name == "get_product_detail":
            return {"product_id": result.get("product_id"), "found": result.get("found", False)}
        return {}
