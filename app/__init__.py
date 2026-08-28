"""购物 Agent 原型系统核心包。

模块划分（见 docs/02_技术选型.md §7）：
- config:    环境与路径配置
- llm:       DeepSeek 调用封装（重试/记账/流式/思考开关）
- retrieval: 商品库索引与结构化检索（纯代码，0 token）
- state:     多轮会话状态管理
- agent/:    Agent 工具循环（编排 + 工具 + 提示词）
- api/:      FastAPI Web 服务（SSE）
"""
