/* 前端逻辑：对话流 + SSE 事件消费 + trace/卡片/约束面板渲染。
   无框架依赖（技术选型 §5：零构建链，评审者开箱即用）。 */
"use strict";

const chatEl = document.getElementById("chat");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send");
const welcomeEl = document.getElementById("welcome");
const constraintBar = document.getElementById("constraint-bar");
const constraintChips = document.getElementById("constraint-chips");

let conversationId = null;
let busy = false;
let currentTurn = null;   // 当前这轮对话的渲染容器

/* ---------------- 基础工具 ---------------- */

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* 极简 markdown：**加粗** + 换行；其余按纯文本处理（先转义防注入） */
function renderRich(text) {
  const escaped = text
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return escaped
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\n/g, "<br>");
}

function scrollToBottom() {
  window.scrollTo({ top: document.body.scrollHeight });
}

/* ---------------- 约束面板 ---------------- */

const CONSTRAINT_LABELS = {
  item_type: "品类",
  tags: "主题",
  manufacturer: "厂商",
  max_price: "预算上限",
  prefer_manufacturer: "偏好厂商",
};

function renderConstraints(constraints) {
  constraintChips.innerHTML = "";
  const entries = Object.entries(constraints || {});
  if (!entries.length) {
    constraintBar.classList.add("hidden");
    return;
  }
  constraintBar.classList.remove("hidden");
  for (const [key, value] of entries) {
    const label = CONSTRAINT_LABELS[key] || key;
    let shown = value;
    if (key === "tags") shown = [].concat(value).join("/");
    if (key === "max_price") shown = "$" + value;
    constraintChips.appendChild(el("span", "chip", label + "：" + shown));
  }
}

/* ---------------- 商品卡片 ---------------- */

function renderCards(sessionView, decision) {
  const candidates = sessionView.last_candidates || [];
  if (!candidates.length) return null;
  const wrap = el("div", "cards");
  for (const cand of candidates.slice(0, 5)) {
    const card = el("div", "card");
    const picked = decision && decision.purchased_product_id === cand.product_id;
    if (picked) card.classList.add("picked");

    if (picked) card.appendChild(el("span", "badge", "已选"));
    card.appendChild(el("div", "name", cand.name));
    card.appendChild(el("div", "price", "$" + cand.price));
    card.appendChild(el("div", "meta", cand.manufacturer + " · " + cand.item_type));

    const tags = el("div", "tags");
    for (const t of cand.tags || []) tags.appendChild(el("span", "tag", t));
    card.appendChild(tags);

    const reasons = (cand.match && cand.match.reasons || []).join("；");
    if (reasons) card.appendChild(el("div", "reasons", reasons));
    wrap.appendChild(card);
  }
  return wrap;
}

/* ---------------- 推理时间线 ---------------- */

function traceLine(ev) {
  switch (ev.kind) {
    case "llm_call": {
      const u = ev.usage || {};
      return `第 ${ev.round + 1} 次模型调用（${ev.finish_reason}，${u.total_tokens} tokens，${u.latency_seconds}s）`;
    }
    case "tool_call":
      return "调用工具 " + ev.name + "：" + JSON.stringify(ev.arguments);
    case "tool_result": {
      const s = ev.summary || {};
      if (ev.name === "search_products") {
        return s.num_candidates
          ? "检索到 " + s.num_candidates + " 个候选：" + (s.top_ids || []).join(", ")
          : "无匹配候选（瓶颈：" + (s.bottleneck || "未知") + "）";
      }
      return "查询商品 " + s.product_id + (s.found ? " 成功" : " 不存在");
    }
    case "guardrail":
      return "护栏校验:" + ev.product_id + " 违反约束 " + (ev.violations || []).join("、") + " → " + ev.action;
    case "decision_repair":
      return "决策格式异常，自动修复重试";
    case "loop_cap":
      return "工具调用达到上限，基于已有信息作答";
    case "decision":
      return "决策：" + ev.action + (ev.purchased_product_id ? "（" + ev.purchased_product_id + "）" : "");
    case "error":
      return "错误：" + (ev.message || "");
    default:
      return ev.kind;
  }
}

function ensureTraceBlock() {
  let details = currentTurn.querySelector("details.trace");
  if (!details) {
    details = el("details", "trace");
    details.appendChild(el("summary", null, "推理过程"));
    details.appendChild(el("ul", "trace-list"));
    currentTurn.insertBefore(details, currentTurn.querySelector(".msg-agent, .thinking"));
  }
  return details;
}

function addTrace(ev) {
  const details = ensureTraceBlock();
  const li = el("li", null, traceLine(ev));
  details.querySelector(".trace-list").appendChild(li);
}

/* ---------------- SSE 事件处理 ---------------- */

function handleEvent(ev) {
  if (!currentTurn) return;
  if (ev.type === "trace") {
    if (ev.kind === "user_input") return;   // 用户消息已单独展示
    addTrace(ev);
  } else if (ev.type === "answer") {
    const thinking = currentTurn.querySelector(".thinking");
    if (thinking) thinking.remove();
    const bubble = el("div", "msg-agent");
    bubble.innerHTML = renderRich(ev.content);
    currentTurn.appendChild(bubble);
  } else if (ev.type === "done") {
    const thinking = currentTurn.querySelector(".thinking");
    if (thinking) thinking.remove();

    // 关键：从服务端响应中获取并记住会话 ID，后续消息才能带上它实现多轮对话
    if (ev.session_view && ev.session_view.conversation_id) {
      conversationId = ev.session_view.conversation_id;
    }
    renderConstraints(ev.session_view ? ev.session_view.constraints : {});
    const cards = renderCards(ev.session_view || {}, ev.decision);
    if (cards) currentTurn.appendChild(cards);

    // 追问选项按钮（需求 F8）
    const options = ev.decision && ev.decision.follow_up_options || [];
    if (options.length) {
      const wrap = el("div", "options");
      for (const opt of options) {
        const btn = el("button", "option-btn", opt);
        btn.type = "button";
        btn.addEventListener("click", () => sendMessage(opt));
        wrap.appendChild(btn);
      }
      currentTurn.appendChild(wrap);
    }

    // trace 摘要补充总账
    const details = currentTurn.querySelector("details.trace");
    if (details && ev.usage_total) {
      const u = ev.usage_total;
      details.querySelector("summary").textContent =
        "推理过程（" + (ev.rounds_used || 0) + " 轮，共 " + u.total_tokens +
        " tokens，耗时 " + u.latency_seconds + "s）";
    }
  } else if (ev.type === "error") {
    const bubble = el("div", "msg-agent", "出错了：" + (ev.message || "未知错误"));
    currentTurn.appendChild(bubble);
  }
  scrollToBottom();
}

/* ---------------- 发送与 SSE 读取 ---------------- */

async function sendMessage(text) {
  text = (text || "").trim();
  if (!text || busy) return;
  busy = true;
  sendBtn.disabled = true;
  welcomeEl.classList.add("hidden");
  inputEl.value = "";

  chatEl.appendChild(el("div", "msg-user", text));
  currentTurn = el("div", "turn");
  currentTurn.style.display = "flex";
  currentTurn.style.flexDirection = "column";
  currentTurn.style.gap = "8px";
  currentTurn.appendChild(el("div", "thinking", "正在检索商品库并思考…"));
  chatEl.appendChild(currentTurn);
  scrollToBottom();

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, conversation_id: conversationId }),
    });
    if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of block.split("\n")) {
          if (line.startsWith("data: ")) {
            try { handleEvent(JSON.parse(line.slice(6))); }
            catch (e) { console.warn("SSE 解析失败", line, e); }
          }
        }
      }
    }
  } catch (e) {
    if (currentTurn) {
      const thinking = currentTurn.querySelector(".thinking");
      if (thinking) thinking.remove();
      currentTurn.appendChild(el("div", "msg-agent", "网络或服务异常：" + e.message));
    }
  } finally {
    busy = false;
    sendBtn.disabled = false;
    currentTurn = null;
    scrollToBottom();
  }
}

/* ---------------- 交互绑定 ---------------- */

sendBtn.addEventListener("click", () => sendMessage(inputEl.value));
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage(inputEl.value);
  }
});
document.querySelectorAll(".example").forEach((btn) => {
  btn.addEventListener("click", () => sendMessage(btn.textContent));
});
document.getElementById("new-chat").addEventListener("click", () => {
  if (busy) return;
  conversationId = null;
  chatEl.innerHTML = "";
  chatEl.appendChild(welcomeEl);
  welcomeEl.classList.remove("hidden");
  renderConstraints({});
});
