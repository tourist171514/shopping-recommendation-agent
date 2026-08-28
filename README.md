# 购物 Agent 原型系统（Agent 工作流构建 · 任务 1）

基于大模型（DeepSeek API，OpenAI 兼容接口）的 **购物 Agent Web 原型**：理解自然语言购物需求 → 检索商品 → 比较候选 → 检查约束 → 做出购买决策并解释结果。项目为《任务 1：Agent 工作流构建》的完整实现，附带需求分析、技术选型与生产级扩展展望文档。

## 功能

**主干流程（一次购买决策）**

- 需求理解：自然语言 → 结构化约束 `{item_type, tags, manufacturer, max_price, soft_prefs}`，区分硬约束与软偏好
- 商品搜索：结构化过滤 + 关键词匹配打分，返回 Top-K 及匹配明细（纯代码，0 token）
- 候选比较：仅 Top-K（K≤5）进入 LLM 上下文比较并输出排序理由
- 约束检查：硬约束逐条校验（通过/违反 + 证据），软偏好计分
- 购买决策：唯一最优商品，或给出"差在哪"的诊断
- 结果说明：自然语言解释每个约束的作用

**拓展功能（已实现）**

- 多轮对话：会话级约束状态 + 历史记忆，支持中途改条件（"太贵了换个便宜的"、"第二个看看"）
- 主动追问：信息不足 / 零结果 / 表述模糊时追问，零候选时给出可点击的放宽选项
- 成本控制：单轮 ≤2 次 LLM 调用，检索零 token；会话内思考模式统一开关（DeepSeek 禁止会话内切换）

## 技术栈

- Python 3.13 + FastAPI + Uvicorn（Web API / SSE 事件流）
- openai SDK（DeepSeek 官方兼容端点，`deepseek-v4-pro`）
- 自研函数调用工具循环（不使用 LangChain / Agent 框架）
- 前端：原生 HTML / CSS / JavaScript

## 目录结构

```text
.
├── app/          # 核心应用（agent 编排 / 检索 / LLM 封装 / 会话状态 / Web API / 前端）
├── docs/         # 01 需求分析 · 02 技术选型 · 03 企业级扩展展望
├── tests/        # 检索模块单元测试
├── 任务/         # 原始材料包（题目、商品库与任务数据、starter 模板）
├── requirements.txt
└── .env.example  # 环境变量示例
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置密钥
cp .env.example .env   # 然后填入 DEEPSEEK_API_KEY

# 3. 启动服务
python -m app.api.server

# 4. 浏览器访问
#    http://127.0.0.1:8000
```

## 运行测试

```bash
PYTHONIOENCODING=utf-8 python -m tests.test_retrieval
```

## 数据与题目来源

商品库（96 条商品，shirt/mug 两类）、公开模拟购物需求与 starter 模板位于 [`任务/`](任务/) 目录，完整题目见 [`任务/任务1_Agent工作流构建_题目.md`](任务/任务1_Agent工作流构建_题目.md)。

## License

[MIT](LICENSE)
