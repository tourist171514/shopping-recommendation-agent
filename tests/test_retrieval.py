"""检索层单元测试（纯代码，不依赖 LLM / 网络）。

运行：python -m tests.test_retrieval
"""
from __future__ import annotations

import sys

# Windows 控制台默认 GBK，强制 UTF-8 避免中文/符号输出崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import DATA_DIR
from app.retrieval import load_store

store = load_store(DATA_DIR)
PASSED, FAILED = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}  {detail}")


def test_basic_hard_constraints():
    """对应任务 A000：shirt + Barn + Konopelski-Inc + ≤$17"""
    print("[1] 硬约束组合命中")
    r = store.search({"item_type": "shirt", "tags": ["Barn"],
                      "manufacturer": "Konopelski-Inc", "max_price": 17})
    check("有候选", not r.is_empty, "零结果")
    if r.candidates:
        top = r.candidates[0].product
        check("Top1 满足全部硬约束",
              top["item_type"] == "shirt" and top["price"] <= 17
              and top["manufacturer"] == "Konopelski-Inc"
              and any("barn" in t.lower() for t in top["tags"]),
              str(top))
        check("附带匹配证据", len(r.candidates[0].reasons) > 0)


def test_soft_preference_boost():
    """对应任务 A002：affordable mug related to Sunny; prefer Bayer-and-Sons"""
    print("[2] 软偏好加分（不淘汰、只排序）")
    r = store.search({"item_type": "mug", "tags": ["Sunny"],
                      "prefer_manufacturer": "Bayer-and-Sons"})
    check("有候选", not r.is_empty)
    if r.candidates:
        check("软偏好厂商排在前面",
              r.candidates[0].product["manufacturer"] == "Bayer-and-Sons",
              f"Top1={r.candidates[0].product['manufacturer']}")


def test_zero_result_diagnosis_price():
    """零结果：预算卡死 → 诊断应指出价格瓶颈并给出最低可行价"""
    print("[3] 零结果诊断（价格瓶颈）")
    r = store.search({"item_type": "shirt", "tags": ["Barn"], "max_price": 5.0})
    check("确实零结果", r.is_empty)
    d = r.diagnosis
    check("有诊断信息", d is not None)
    if d:
        check("瓶颈定位到价格", d["bottleneck"] == "max_price", str(d["bottleneck"]))
        check("给出最低可行价", d["nearest_price_if_relax_budget"] is not None,
              str(d))
        check("给出放宽建议", len(d["suggestions"]) > 0)


def test_zero_result_diagnosis_manufacturer():
    """零结果：厂商不存在 → leave-one-out 应指认厂商是瓶颈"""
    print("[4] 零结果诊断（厂商瓶颈）")
    r = store.search({"item_type": "shirt", "manufacturer": "Not-Exist-Corp"})
    check("确实零结果", r.is_empty)
    if r.diagnosis:
        loo = r.diagnosis["leave_one_out"]
        check("放宽厂商后有候选", any(n > 0 for desc, n in loo.items() if "厂商" in desc),
              str(loo))


def test_case_insensitive_and_name_match():
    print("[5] 大小写不敏感 + 名称匹配")
    r1 = store.search({"tags": ["barn"]})          # 小写
    r2 = store.search({"tags": ["BARN"]})          # 大写
    check("大小写结果一致", len(r1.candidates) == len(r2.candidates) and len(r1.candidates) > 0)


def test_get_product():
    print("[6] 按 ID 取商品详情")
    p = store.get_product("P0000")
    check("P0000 存在", p is not None and p["name"] == "Generic Barn Shirt")
    check("未知 ID 返回 None", store.get_product("P9999") is None)


def test_price_boundary_sanity():
    """记录设计假设：数据中无整数价格，'≤ 预算' 与 '< 预算' 等价。"""
    print("[7] 价格边界检查（≤ 与 < 的等价性前提）")
    # 真正的性质：不存在整数价格 → 任务里 "under $17" 用 ≤17 过滤不会产生边界歧义
    integer_prices = [p for p in store.products
                      if abs(p["price"] - round(p["price"])) < 1e-9]
    check("不存在整数价格（预算过滤无边界歧义）", not integer_prices,
          str([p["price"] for p in integer_prices[:5]]))


def test_llm_dict_no_description():
    """token 优化验证：喂给 LLM 的候选不含无语义的 description"""
    print("[8] LLM 输出精简（不含 description）")
    r = store.search({"item_type": "shirt"})
    if r.candidates:
        d = r.candidates[0].to_llm_dict()
        check("无 description 字段", "description" not in d)
        check("含匹配证据", "match" in d)


def main():
    print(f"商品库加载: {len(store.products)} 条商品")
    for fn in [test_basic_hard_constraints, test_soft_preference_boost,
               test_zero_result_diagnosis_price, test_zero_result_diagnosis_manufacturer,
               test_case_insensitive_and_name_match, test_get_product,
               test_price_boundary_sanity, test_llm_dict_no_description]:
        fn()
    print(f"\n结果: {PASSED} 通过 / {FAILED} 失败")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
