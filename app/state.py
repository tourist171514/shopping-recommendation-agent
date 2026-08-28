"""多轮会话状态管理（需求 F7）。

设计原则（对照需求分析 §2 F7）：
- 服务端保存"累积约束状态"：新一轮只更新提到的约束，未提及的保留；
- 历史只保留 role+content 的精简形式，不无限累积（需求 §6 风险对策）；
- 保留上一轮候选与决策，支撑"第二个看看"这类指代式追问。
原型阶段存内存，重启清空（技术选型 §4）。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class SessionState:
    conversation_id: str
    created_at: float = field(default_factory=time.time)
    # 精简对话历史：[{"role": "user"|"assistant", "content": str}]
    history: list[dict] = field(default_factory=list)
    # 累积约束（键同 retrieval.Constraints 字段；新一轮只覆盖提到的键）
    constraints: dict = field(default_factory=dict)
    # 上一轮检索结果快照（支撑指代追问与决策更新）
    last_candidates: list[dict] = field(default_factory=list)
    last_decision: dict | None = None
    turn_count: int = 0

    def add_message(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        if role == "user":
            self.turn_count += 1

    def merge_constraints(self, new: dict) -> dict:
        """增量合并：只覆盖本轮新提到的约束键。返回合并后的完整约束。"""
        for k, v in new.items():
            if v is None or v == [] or v == "":
                # 显式清空语义：用户说"不要厂商限制了" → 值为空即移除
                self.constraints.pop(k, None)
            else:
                self.constraints[k] = v
        return dict(self.constraints)

    def clear_constraint(self, key: str) -> None:
        self.constraints.pop(key, None)

    def public_view(self) -> dict:
        """前端展示用：约束面板 + 候选商品卡片。"""
        return {
            "conversation_id": self.conversation_id,
            "turn_count": self.turn_count,
            "constraints": self.constraints,
            "last_decision": self.last_decision,
            "last_candidates": self.last_candidates,
        }


class SessionStore:
    """内存会话仓库。"""

    def __init__(self):
        self._sessions: dict[str, SessionState] = {}

    def create(self) -> SessionState:
        cid = uuid.uuid4().hex[:12]
        s = SessionState(conversation_id=cid)
        self._sessions[cid] = s
        return s

    def get(self, conversation_id: str) -> SessionState | None:
        return self._sessions.get(conversation_id)

    def get_or_create(self, conversation_id: str | None) -> SessionState:
        if conversation_id:
            s = self.get(conversation_id)
            if s:
                return s
        return self.create()
