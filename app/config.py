"""全局配置：路径与模型参数。

密钥只从 .env / 环境变量读取（需求 N5），绝不硬编码。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录 = 材料包根目录（data/ 与 starter/ 的同级）
ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

def _find_data_dir() -> Path:
    """兼容两种目录布局：官方材料在根目录，或整理进 任务/ 子目录。"""
    for cand in (ROOT_DIR / "data", ROOT_DIR / "任务" / "data"):
        if cand.exists():
            return cand
    return ROOT_DIR / "data"


DATA_DIR = _find_data_dir()
OUTPUT_DIR = ROOT_DIR / "outputs"
WEB_DIR = Path(__file__).resolve().parent / "web"

# ---- DeepSeek 模型配置 ----
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

# ---- Agent 循环参数（对照技术选型 §2）----
MAX_TOOL_ROUNDS = 5        # 工具循环硬上限，防失控
TOP_K = 5                  # 进入 LLM 比较的最大候选数（需求 F3）
HISTORY_LIMIT = 20         # 进入提示词的历史消息条数上限（滑动窗口）
                           # 截断安全：购物状态由结构化约束承载，不依赖原始历史
LLM_TIMEOUT_SECONDS = 60.0
LLM_MAX_RETRIES = 2

# ---- 思考模式（会话级开关）----
# 实测发现 DeepSeek 的 API 约束：同一会话内禁止中途切换思考开关
# （首轮关思考、次轮开思考 → 400: reasoning_content must be passed back）。
# 因此"分步骤选择性开关"不可行，降级为会话级统一开关：
#   True  = 全程思考（默认，保决策质量）
#   False = 全程不思考（批量评测时用于质量/成本对比实验）
THINKING_ENABLED = True
