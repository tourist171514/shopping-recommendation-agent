"""FastAPI Web 服务：SSE 事件流 + 静态前端托管（技术选型 §4）。

接口：
- POST /api/session            新建会话
- GET  /api/session/{cid}      会话状态（约束面板数据）
- POST /api/chat               SSE：trace 事件流 → answer → done
- GET  /                       前端页面

启动：python -m app.api.server  →  http://127.0.0.1:8000
"""
from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import iterate_in_threadpool
from starlette.responses import StreamingResponse

from app import config
from app.agent.core import Agent
from app.state import SessionStore

app = FastAPI(title="购物 Agent 原型系统")
sessions = SessionStore()
agent = Agent(config.DATA_DIR)


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/session")
def create_session():
    return {"conversation_id": sessions.create().conversation_id}


@app.get("/api/session/{cid}")
def get_session(cid: str):
    s = sessions.get(cid)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return s.public_view()


@app.post("/api/chat")
async def chat(req: ChatRequest):
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is empty")
    session = sessions.get_or_create(req.conversation_id)

    async def event_stream():
        try:
            # Agent.chat 是同步阻塞（LLM 调用），放线程池迭代，避免阻塞事件循环
            async for event in iterate_in_threadpool(agent.chat(session, message)):
                yield _sse(event)
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "error", "message": f"服务内部错误: {e}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# 静态前端（路由注册之后挂载，避免吞掉 /api 路径）
app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    print("购物 Agent 服务启动: http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)
