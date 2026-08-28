"""Agent 核心：工具循环编排（技术选型 §2，题目"自主规划、调用工具、观察结果"的实现）。

循环结构：
    用户输入 → LLM(决定调哪个工具) → 执行工具 → LLM(观察结果) → … → 最终答复
    （正常路径 2 次 LLM 调用；上限 MAX_TOOL_ROUNDS 防失控，超限强制作答）

两个入口共享同一循环：
- run(instruction)：单轮，兼容 starter/agent_interface.py，供批量评测；
- chat(session, message)：多轮，生成器，逐步 yield trace/answer/done 事件，供 Web SSE。

可靠性设计：
- 决策解析失败 → 自动修复重试 1 次；
- 护栏校验：最终选购商品若违反硬约束 → 用最近一次检索中最优的合法候选兜底，
  并在 trace 中记录 override 原因（可审计）。
"""
from __future__ import annotations

import json
import re
import time

from app import config
from app.agent.prompts import build_system_prompt
from app.agent.tools import TOOL_SCHEMAS, ToolExecutor
from app.llm import DeepSeekClient, TokenUsage
from app.retrieval import Constraints, load_store
from app.state import SessionState

_DECISION_RE = re.compile(r"<decision>\s*(\{.*?\})\s*</decision>", re.S)


def parse_decision(text: str) -> tuple[str, dict | None]:
    """从最终答复中分离展示文本与 <decision> JSON。"""
    m = _DECISION_RE.search(text or "")
    if not m:
        return (text or "").strip(), None
    try:
        decision = json.loads(m.group(1))
    except json.JSONDecodeError:
        return (text or "").strip(), None
    display = (text[:m.start()] + text[m.end():]).strip()
    return display, decision


class Agent:
    """购物 Agent。保持 starter 约定的公开调用方式：

        agent = Agent(data_dir)
        result = agent.run(instruction)
    """

    def __init__(self, data_dir, thinking: bool | None = None):
        """thinking：会话级思考开关（None=取 config 默认）。

        注意：DeepSeek 禁止同一会话中途切换思考模式，故为会话级而非步骤级。
        """
        self.store = load_store(data_dir)
        self.tools = ToolExecutor(self.store)
        self.llm = DeepSeekClient()
        self.thinking = config.THINKING_ENABLED if thinking is None else thinking
        self.system_prompt = build_system_prompt(config.MAX_TOOL_ROUNDS)

    # ------------------------------------------------------------------
    # 入口 1：单轮（兼容 starter，批量评测用）
    # ------------------------------------------------------------------

    def run(self, instruction: str) -> dict:
        session = SessionState(conversation_id=f"batch-{int(time.time() * 1000)}")
        done = None
        for event in self.chat(session, instruction):
            if event["type"] == "done":
                done = event
        decision = (done or {}).get("decision") or {}
        return {
            "instruction": instruction,
            "purchased_product_id": decision.get("purchased_product_id"),
            "trace": (done or {}).get("trace", []),
            "summary": decision.get("answer_text", ""),
            "usage": (done or {}).get("usage_total", {}),
            "rounds_used": (done or {}).get("rounds_used", 0),
        }

    # ------------------------------------------------------------------
    # 入口 2：多轮（Web 用，生成器事件流）
    # ------------------------------------------------------------------

    def chat(self, session: SessionState, message: str):
        """事件流：
        {"type":"trace", "kind":..., ...}   —— 过程事件（时间线展示）
        {"type":"answer", "content":...}    —— 最终自然语言答复
        {"type":"done", "decision":..., "trace":..., "usage_total":...}
        """
        trace: list[dict] = []
        usage_total = TokenUsage()
        step_counter = {"n": 0}

        def trace_event(kind: str, **payload) -> dict:
            step_counter["n"] += 1
            ev = {"step": step_counter["n"], "type": "trace", "kind": kind, **payload}
            trace.append(ev)
            return ev

        session.add_message("user", message)
        yield trace_event("user_input", content=message)

        messages = self._build_messages(session)
        final_text = ""

        # ---------------- 工具循环 ----------------
        rounds_used = 0
        for round_idx in range(config.MAX_TOOL_ROUNDS):
            try:
                resp = self.llm.chat(messages, tools=TOOL_SCHEMAS, thinking=self.thinking)
            except Exception as e:  # noqa: BLE001
                yield trace_event("error", message=f"模型调用失败: {e}")
                final_text = "抱歉，模型服务暂时不可用，请稍后重试。"
                break
            usage_total.add(resp.usage)
            rounds_used = round_idx + 1
            msg = resp.message
            yield trace_event("llm_call", round=round_idx,
                              finish_reason=resp.finish_reason,
                              usage=resp.usage.to_dict())
            messages.append(self._assistant_msg(msg))

            tool_calls = getattr(msg, "tool_calls", None) or []
            if not tool_calls:
                final_text = msg.content or ""
                break

            # 执行工具并把结果回填给模型观察
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self.tools.execute(name, args)
                yield trace_event("tool_call", name=name, arguments=args)
                yield trace_event("tool_result", name=name,
                                  summary=self.tools.summarize(name, result))
                if name == "search_products":
                    session.last_candidates = result.get("candidates", [])
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
        else:
            # 循环上限仍未收敛：移除工具，强制直接作答
            yield trace_event("loop_cap", max_rounds=config.MAX_TOOL_ROUNDS)
            resp = self.llm.chat(
                messages + [{"role": "user",
                             "content": "工具调用次数已达上限，请基于已有信息直接按格式给出最终答复。"}],
                tools=None, thinking=self.thinking)
            usage_total.add(resp.usage)
            final_text = resp.message.content or ""

        # ---------------- 决策解析（含一次自动修复）----------------
        display, decision = parse_decision(final_text)
        if decision is None:
            yield trace_event("decision_repair", reason="缺少或无法解析 <decision> 块")
            try:
                repair = self.llm.chat(
                    messages + [{"role": "assistant", "content": final_text},
                                {"role": "user",
                                 "content": "你的回复缺少决策块。请只补发 <decision>{...}</decision> "
                                            "JSON，不要输出任何其他内容。"}],
                    tools=None, thinking=self.thinking, max_tokens=600)
                usage_total.add(repair.usage)
                _, decision = parse_decision(repair.message.content or "")
            except Exception:  # noqa: BLE001
                decision = None
        decision = decision or {}

        # ---------------- 多轮状态更新 ----------------
        merged = session.merge_constraints(decision.get("constraints_update") or {})

        # ---------------- 护栏：选购商品违反硬约束时兜底 ----------------
        pid = decision.get("purchased_product_id")
        product = self.store.get_product(pid) if pid else None
        if product is not None:
            violations = self._validate(product, merged)
            if violations:
                replacement = self._best_valid_candidate(session, merged)
                yield trace_event("guardrail", product_id=pid, violations=violations,
                                  action=("override->" + replacement["product_id"])
                                  if replacement else "kept(无合法候选)")
                if replacement:
                    decision["purchased_product_id"] = replacement["product_id"]

        answer_text = display or final_text
        decision_out = {
            "action": decision.get("action", "recommend" if decision.get("purchased_product_id") else "ask_user"),
            "purchased_product_id": decision.get("purchased_product_id"),
            "constraints_update": decision.get("constraints_update") or {},
            "follow_up_options": decision.get("follow_up_options") or [],
            "answer_text": answer_text,
        }
        session.last_decision = decision_out
        session.add_message("assistant", answer_text)
        yield trace_event("decision", **{k: v for k, v in decision_out.items() if k != "answer_text"})
        yield {"type": "answer", "content": answer_text}
        yield {
            "type": "done",
            "decision": decision_out,
            "trace": trace,
            "rounds_used": rounds_used,
            "usage_total": usage_total.to_dict(),
            "session_view": session.public_view(),
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _build_messages(self, session: SessionState) -> list[dict]:
        """system + 会话状态行 + 精简历史（利用前缀缓存，多轮增量成本极低）。"""
        messages: list[dict] = [{"role": "system", "content": self.system_prompt}]
        state_desc = Constraints.from_dict(session.constraints).describe()
        state_line = f"[当前会话状态] 累积约束：{state_desc}"
        if session.last_candidates:
            cands = ", ".join(f"{c['product_id']}({c['name']})"
                              for c in session.last_candidates[:config.TOP_K])
            state_line += f"；上轮候选：{cands}"
        messages.append({"role": "system", "content": state_line})
        # 历史滑动窗口：只取最近 N 条，防止长会话 token 无限膨胀（需求 §6 对策）。
        # 截断是安全的：品类/预算等关键状态已由结构化约束（上方状态行）承载。
        messages.extend(session.history[-config.HISTORY_LIMIT:])
        return messages

    @staticmethod
    def _assistant_msg(msg) -> dict:
        """把 SDK 消息对象转成可回传的 dict。

        注意：DeepSeek 思考模式要求把上一轮的 reasoning_content 原样传回，
        否则报 400（实测踩坑，已修复）。
        """
        out = {"role": "assistant", "content": msg.content}
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            out["reasoning_content"] = reasoning
        if getattr(msg, "tool_calls", None):
            out["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        return out

    def _validate(self, product: dict, constraints: dict) -> list[str]:
        """护栏校验：返回该商品违反的硬约束列表。"""
        c = Constraints.from_dict(constraints)
        violations = []
        if c.item_type and product.get("item_type", "").lower() != c.item_type.lower():
            violations.append(f"品类应为 {c.item_type}")
        if c.max_price is not None and product.get("price", 0) > c.max_price:
            violations.append(f"价格 ${product.get('price'):g} 超出上限 ${c.max_price:g}")
        if c.manufacturer and not self.store._manufacturer_ok(product, c.manufacturer):
            violations.append(f"厂商应为 {c.manufacturer}")
        if c.tags:
            hit, _ = self.store._tag_hits(product, c.tags)
            if not hit:
                violations.append("缺少主题 " + "/".join(c.tags))
        return violations

    def _best_valid_candidate(self, session: SessionState, constraints: dict) -> dict | None:
        """从上一轮检索候选中找第一个满足全部硬约束的商品（候选已按得分排序）。"""
        for cand in session.last_candidates:
            product = self.store.get_product(cand.get("product_id", ""))
            if product and not self._validate(product, constraints):
                return product
        return None
