"""FIT Knowledge Chatbot web application.

Serves a single-page chat UI backed by either a published Microsoft Foundry
Agent or the existing RAG API. Conversation history is stored in local SQLite.

Environment variables:
    CHAT_PROVIDER  - "foundry_agent" or "rag" (default: rag)
    FOUNDRY_PROJECT_ENDPOINT - full Foundry project endpoint
    FOUNDRY_AGENT_NAME - published Foundry agent name
    FOUNDRY_AGENT_VERSION - published Foundry agent version
    RAG_BASE_URL   - base URL of the RAG API (default http://localhost:8000)
    RAG_API_KEY    - X-API-Key for RAG API (optional)
    CHAT_DB_PATH   - path to SQLite file (default: ./chat.db)
    CHATBOT_API_KEY - bearer token to protect this chatbot (optional)

Run:
    python -m uvicorn chatbot.app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security.api_key import APIKeyHeader
from dotenv import load_dotenv
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).with_name(".env"))

CHAT_PROVIDER: str = os.getenv("CHAT_PROVIDER", "rag").strip().lower()
RAG_BASE_URL: str = os.getenv("RAG_BASE_URL", "http://localhost:8000")
RAG_API_KEY: str = os.getenv("RAG_API_KEY", "")
FOUNDRY_PROJECT_ENDPOINT: str = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
FOUNDRY_AGENT_NAME: str = os.getenv("FOUNDRY_AGENT_NAME", "").strip()
FOUNDRY_AGENT_VERSION: str = os.getenv("FOUNDRY_AGENT_VERSION", "").strip()
DB_PATH: str = os.getenv("CHAT_DB_PATH", str(Path(__file__).parent / "chat.db"))
CHATBOT_API_KEY: str = os.getenv("CHATBOT_API_KEY", "")
HISTORY_LIMIT: int = 200  # max messages returned per session

# ---------------------------------------------------------------------------
# Memory / follow-up settings
# ---------------------------------------------------------------------------

CONTEXT_TURNS: int = 3  # 帶入前 N 輪對話（6 則訊息）
CONTEXT_CHAR_LIMIT: int = 3000  # 歷史記憶總字元上限
MSG_CHAR_LIMIT: int = 800  # 單則訊息最多傳送字元數

# 追問偵測：訊息很短，或以參照詞開頭
_FOLLOWUP_RE = re.compile(
    r'^(那|這|他|她|它|還有|繼續|詳細|再說|更多|為何|為什麼'
    r'|怎麼|如何|所以|但是|不過|讓我|語我|告訴|請問'
    r'|另外|然後|補充|說明|解釋|舉例|例如|剛才|剛剛'
    r'|上面|前面|之前|可以再|能再|can you|could you|what about|how about'
    r'|tell me more|explain|elaborate|why|how)',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# SQLite – synchronous (DB ops are fast enough; avoids aiosqlite dep)
# ---------------------------------------------------------------------------


def _init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id  TEXT    PRIMARY KEY,
                title       TEXT    DEFAULT '新對話',
                created_at  REAL    NOT NULL,
                updated_at  REAL    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT    NOT NULL,
                role        TEXT    NOT NULL,   -- 'user' | 'assistant'
                content     TEXT    NOT NULL,
                sources     TEXT,               -- JSON array
                created_at  REAL    NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session " "ON messages(session_id, created_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT    NOT NULL,
                message_id  INTEGER NOT NULL,
                rating      INTEGER NOT NULL,   -- 1 = up, -1 = down
                comment     TEXT,
                created_at  REAL    NOT NULL,
                UNIQUE(message_id)
            )
        """)
        conn.commit()


_init_db()


def _build_context_history(session_id: str) -> list[dict[str, str]]:
    """取最近 CONTEXT_TURNS 輪對話，總字元不超過 CONTEXT_CHAR_LIMIT。"""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (session_id, CONTEXT_TURNS * 2),
        ).fetchall()
    rows = list(reversed(rows))  # 轉為時間正序
    history: list[dict[str, str]] = []
    total = 0
    for role, content in rows:
        if role not in ("user", "assistant"):
            continue
        trimmed = content[:MSG_CHAR_LIMIT]
        total += len(trimmed)
        if total > CONTEXT_CHAR_LIMIT:
            break
        history.append({"role": role, "content": trimmed})
    return history


def _is_followup(message: str, history: list[dict]) -> bool:
    """簡易追問偵測：訊息很短或以參照詞開頭。"""
    if not history:
        return False
    msg = message.strip()
    if _FOLLOWUP_RE.match(msg):
        return True
    return False


def _call_foundry_agent_sync(
    message: str,
    history: list[dict[str, str]],
) -> str:
    """Use the official Foundry SDK and published agent reference."""
    missing = [
        name
        for name, value in (
            ("FOUNDRY_PROJECT_ENDPOINT", FOUNDRY_PROJECT_ENDPOINT),
            ("FOUNDRY_AGENT_NAME", FOUNDRY_AGENT_NAME),
            ("FOUNDRY_AGENT_VERSION", FOUNDRY_AGENT_VERSION),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"缺少必要環境變數：{', '.join(missing)}")

    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise RuntimeError("缺少 Foundry SDK；請安裝 chatbot/requirements.txt") from exc

    agent_input = [
        {"role": item["role"], "content": item["content"]}
        for item in history
        if item.get("role") in ("user", "assistant") and item.get("content")
    ]
    agent_input.append({"role": "user", "content": message})

    with DefaultAzureCredential() as credential:
        with AIProjectClient(
            endpoint=FOUNDRY_PROJECT_ENDPOINT,
            credential=credential,
        ) as project_client:
            with project_client.get_openai_client() as openai_client:
                response = openai_client.responses.create(
                    input=agent_input,
                    extra_body={
                        "agent_reference": {
                            "name": FOUNDRY_AGENT_NAME,
                            "version": FOUNDRY_AGENT_VERSION,
                            "type": "agent_reference",
                        }
                    },
                )

    answer = (response.output_text or "").strip()
    return answer or "（Agent 未回傳文字內容）"


async def _stream_foundry_agent(
    message: str,
    history: list[dict[str, str]],
) -> AsyncIterator[str]:
    """Yield text deltas from the Foundry Responses streaming API."""
    missing = [
        name
        for name, value in (
            ("FOUNDRY_PROJECT_ENDPOINT", FOUNDRY_PROJECT_ENDPOINT),
            ("FOUNDRY_AGENT_NAME", FOUNDRY_AGENT_NAME),
            ("FOUNDRY_AGENT_VERSION", FOUNDRY_AGENT_VERSION),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"缺少必要環境變數：{', '.join(missing)}")

    try:
        from azure.ai.projects.aio import AIProjectClient
        from azure.identity.aio import DefaultAzureCredential
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Foundry 非同步 SDK；請安裝 chatbot/requirements.txt"
        ) from exc

    agent_input = [
        {"role": item["role"], "content": item["content"]}
        for item in history
        if item.get("role") in ("user", "assistant") and item.get("content")
    ]
    agent_input.append({"role": "user", "content": message})

    async with DefaultAzureCredential() as credential:
        async with AIProjectClient(
            endpoint=FOUNDRY_PROJECT_ENDPOINT,
            credential=credential,
        ) as project_client:
            async with project_client.get_openai_client() as openai_client:
                stream = await openai_client.responses.create(
                    input=agent_input,
                    extra_body={
                        "agent_reference": {
                            "name": FOUNDRY_AGENT_NAME,
                            "version": FOUNDRY_AGENT_VERSION,
                            "type": "agent_reference",
                        }
                    },
                    stream=True,
                )
                async for event in stream:
                    if event.type == "response.output_text.delta" and event.delta:
                        yield event.delta


async def _request_chat_backend(
    question: str,
    history: list[dict[str, str]],
    *,
    use_rag: bool,
    top_k: int,
    category_filter: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Call the configured chat provider and normalize its response."""
    if CHAT_PROVIDER == "foundry_agent":
        try:
            answer = await asyncio.to_thread(
                _call_foundry_agent_sync,
                question,
                history,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Foundry Agent 呼叫失敗：{exc}",
            ) from exc
        return answer, []

    if CHAT_PROVIDER != "rag":
        raise HTTPException(
            status_code=503,
            detail="CHAT_PROVIDER 必須是 rag 或 foundry_agent",
        )

    headers: dict[str, str] = {}
    if RAG_API_KEY:
        headers["X-API-Key"] = RAG_API_KEY
    payload: dict[str, Any] = {
        "question": question,
        "top_k": top_k,
        "history": history,
    }
    if category_filter:
        payload["category_filter"] = category_filter
    endpoint = "/query" if use_rag else "/chat"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{RAG_BASE_URL}{endpoint}",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"RAG API error {exc.response.status_code}",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"RAG API unreachable: {exc}",
        ) from exc

    data = response.json()
    return data.get("answer", "（無回應）"), data.get("sources", [])


def _save(
    session_id: str,
    role: str,
    content: str,
    sources: list[dict] | None = None,
) -> int:
    now = time.time()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO messages "
            "(session_id, role, content, sources, created_at) VALUES (?,?,?,?,?)",
            (
                session_id,
                role,
                content,
                json.dumps(sources or [], ensure_ascii=False),
                now,
            ),
        )
        msg_id = cur.lastrowid
        # Update session's updated_at timestamp
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )
        conn.commit()
    return int(msg_id or 0)


def _get_history(session_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT m.id, m.role, m.content, m.sources, m.created_at, f.rating "
            "FROM messages m LEFT JOIN feedback f ON f.message_id = m.id "
            "WHERE m.session_id=? "
            "ORDER BY m.created_at ASC LIMIT ?",
            (session_id, HISTORY_LIMIT),
        ).fetchall()
    return [
        {
            "id": r[0],
            "role": r[1],
            "content": r[2],
            "sources": json.loads(r[3] or "[]"),
            "created_at": r[4],
            "rating": r[5],
        }
        for r in rows
    ]


def _list_sessions(limit: int = 50) -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT s.session_id,
                   s.title,
                   s.created_at,
                   s.updated_at,
                   MIN(m.content)      AS first_msg,
                   MAX(m.created_at)   AS last_at,
                   COUNT(m.id)         AS msg_count
            FROM   sessions s
            LEFT   JOIN messages m ON s.session_id = m.session_id AND m.role = 'user'
            GROUP  BY s.session_id
            ORDER  BY s.updated_at DESC
            LIMIT  ?
            """,
            (limit,),
        ).fetchall()
    result = []
    for r in rows:
        preview = r[4] if r[4] else ""
        if len(preview) > 42:
            preview = preview[:42] + "…"
        result.append(
            {
                "session_id": r[0],
                "title": r[1],
                "created_at": r[2],
                "updated_at": r[3],
                "first_msg": preview,
                "last_at": r[5],
                "msg_count": r[6],
            }
        )
    return result


# ---------------------------------------------------------------------------
# FastAPI app + optional auth
# ---------------------------------------------------------------------------

app = FastAPI(title="FIT Chat", docs_url=None, redoc_url=None)

_api_key_header = APIKeyHeader(name="X-Chatbot-Key", auto_error=False)


def _check_key(key: str | None = Security(_api_key_header)) -> None:
    if CHATBOT_API_KEY and key != CHATBOT_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing key")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str
    use_rag: bool = True  # 是否使用 RAG 查詢知識庫，預設為 True
    top_k: int = 5
    category_filter: str | None = None
    date_from: str | None = None
    date_to: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    sources: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, _: None = Security(_check_key)) -> ChatResponse:
    session_id = (req.session_id or "").strip() or str(uuid.uuid4())

    # 如果是新會話，註冊到 sessions 表
    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO sessions (session_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, "新對話", time.time(), time.time()),
            )
            conn.commit()

    # 建立歷史記憶（在儲存本次訊息之前）
    history = _build_context_history(session_id)
    is_followup = _is_followup(req.message, history)

    # 追問時展開 query：加上上一則用戶問題作為讖別參照
    if is_followup and history:
        last_user_q = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        rag_query = f"{last_user_q} {req.message}".strip() if last_user_q else req.message
    else:
        rag_query = req.message

    backend_question = req.message if CHAT_PROVIDER == "foundry_agent" else rag_query
    answer, sources = await _request_chat_backend(
        backend_question,
        history,
        use_rag=req.use_rag,
        top_k=req.top_k,
        category_filter=req.category_filter,
    )

    _save(session_id, "user", req.message)  # 儲原始訊息，不儲展開後的 rag_query
    _save(session_id, "assistant", answer, sources)

    return ChatResponse(session_id=session_id, answer=answer, sources=sources)


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, _: None = Security(_check_key)):
    """Streaming version of /api/chat — proxies SSE from RAG /query/stream or /chat/stream."""
    session_id = (req.session_id or "").strip() or str(uuid.uuid4())

    # 如果是新會話，註冊到 sessions 表
    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO sessions (session_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, "新對話", time.time(), time.time()),
            )
            conn.commit()

    # 建立歷史記憶（在儲存本次訊息之前）
    history = _build_context_history(session_id)
    is_followup = _is_followup(req.message, history)

    if is_followup and history:
        last_user_q = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        rag_query = f"{last_user_q} {req.message}".strip() if last_user_q else req.message
    else:
        rag_query = req.message

    backend_question = req.message if CHAT_PROVIDER == "foundry_agent" else rag_query

    headers: dict[str, str] = {}
    if RAG_API_KEY:
        headers["X-API-Key"] = RAG_API_KEY

    payload: dict[str, Any] = {
        "question": rag_query,
        "top_k": req.top_k,
        "history": history,
    }
    if req.category_filter:
        payload["category_filter"] = req.category_filter

    _save(session_id, "user", req.message)  # 儲原始訊息

    if CHAT_PROVIDER == "foundry_agent":

        async def _generate_foundry():
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
            yield f"data: {json.dumps({'type': 'sources', 'sources': []})}\n\n"
            full_answer: list[str] = []
            try:
                async for delta in _stream_foundry_agent(backend_question, history):
                    full_answer.append(delta)
                    event = {"type": "token", "text": delta}
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as exc:
                event = {"type": "error", "text": f"Foundry Agent 串流失敗：{exc}"}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                return

            answer = "".join(full_answer).strip() or "（Agent 未回傳文字內容）"
            msg_id = _save(session_id, "assistant", answer, [])
            yield f"data: {json.dumps({'type': 'done', 'message_id': msg_id})}\n\n"

        return StreamingResponse(
            _generate_foundry(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 根據 use_rag 決定調用哪個端點
    stream_endpoint = "/query/stream" if req.use_rag else "/chat/stream"

    async def _generate():
        # tell client the session id immediately
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"

        full_answer: list[str] = []
        sources_data: list[dict] = []

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    f"{RAG_BASE_URL}{stream_endpoint}",
                    json=payload,
                    headers=headers,
                ) as resp:
                    if resp.status_code == 404:
                        # Backward compatibility: fallback to non-streaming version
                        fallback_endpoint = "/query" if req.use_rag else "/chat"
                        fallback = await client.post(
                            f"{RAG_BASE_URL}{fallback_endpoint}",
                            json=payload,
                            headers=headers,
                        )
                        if fallback.status_code != 200:
                            yield f"data: {json.dumps({'type': 'error', 'text': f'RAG API error {fallback.status_code}'})}\n\n"
                            return
                        data = fallback.json()
                        sources_data = data.get("sources", [])
                        answer = data.get("answer", "")
                        yield f"data: {json.dumps({'type': 'sources', 'sources': sources_data}, ensure_ascii=False)}\n\n"
                        if answer:
                            yield f"data: {json.dumps({'type': 'token', 'text': answer}, ensure_ascii=False)}\n\n"
                        msg_id = _save(session_id, "assistant", answer, sources_data)
                        yield f"data: {json.dumps({'type': 'done', 'message_id': msg_id})}\n\n"
                        return
                    if resp.status_code != 200:
                        yield f"data: {json.dumps({'type': 'error', 'text': f'RAG API error {resp.status_code}'})}\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        try:
                            event = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        if event["type"] == "sources":
                            sources_data = event["sources"]
                        elif event["type"] == "token":
                            full_answer.append(event["text"])
                        elif event["type"] == "done":
                            msg_id = _save(
                                session_id, "assistant", "".join(full_answer), sources_data
                            )
                            event["message_id"] = msg_id
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except httpx.RequestError as e:
            yield f"data: {json.dumps({'type': 'error', 'text': f'RAG API unreachable: {e}'})}\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/history/{session_id}")
def get_history(session_id: str, _: None = Security(_check_key)):
    return _get_history(session_id)


@app.delete("/api/history/{session_id}")
def delete_history(session_id: str, _: None = Security(_check_key)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        conn.commit()
    return {"ok": True}


@app.get("/api/sessions")
def get_sessions(_: None = Security(_check_key)):
    return _list_sessions()


@app.get("/api/health")
def health(_: None = Security(_check_key)):
    """Report backend selection and whether required configuration is present."""
    if CHAT_PROVIDER == "foundry_agent":
        missing = [
            name
            for name, value in (
                ("FOUNDRY_PROJECT_ENDPOINT", FOUNDRY_PROJECT_ENDPOINT),
                ("FOUNDRY_AGENT_NAME", FOUNDRY_AGENT_NAME),
                ("FOUNDRY_AGENT_VERSION", FOUNDRY_AGENT_VERSION),
            )
            if not value
        ]
        return {
            "status": "ok" if not missing else "configuration_error",
            "provider": CHAT_PROVIDER,
            "agent_name": FOUNDRY_AGENT_NAME or None,
            "agent_version": FOUNDRY_AGENT_VERSION or None,
            "missing": missing,
        }
    return {
        "status": "ok" if CHAT_PROVIDER == "rag" else "configuration_error",
        "provider": CHAT_PROVIDER,
        "rag_base_url": RAG_BASE_URL if CHAT_PROVIDER == "rag" else None,
    }


@app.put("/api/sessions/{session_id}")
def rename_session(session_id: str, title: str = Query(...), _: None = Security(_check_key)):
    """更新會話標題。"""
    title = (title or "").strip() or "新對話"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ?",
            (title, time.time(), session_id),
        )
        conn.commit()
    return {"ok": True, "title": title}


@app.get("/api/document/{doc_id}")
async def get_document(doc_id: str, _: None = Security(_check_key)):
    """代理到 RAG /document/{doc_id}，回傳完整信件內容。"""
    headers: dict[str, str] = {}
    if RAG_API_KEY:
        headers["X-API-Key"] = RAG_API_KEY
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{RAG_BASE_URL}/document/{doc_id}", headers=headers)
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="找不到該文件")
            resp.raise_for_status()
            return resp.json()
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502, detail=f"RAG API error {e.response.status_code}"
        ) from e
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"RAG API unreachable: {e}") from e


# ---------------------------------------------------------------------------
# Feedback / Search / Auto-title
# ---------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    message_id: int
    rating: int  # 1 = thumbs up, -1 = thumbs down, 0 = clear
    comment: str | None = None


@app.post("/api/feedback")
def submit_feedback(req: FeedbackRequest, _: None = Security(_check_key)):
    if req.rating not in (-1, 0, 1):
        raise HTTPException(status_code=400, detail="rating must be -1, 0 or 1")
    with sqlite3.connect(DB_PATH) as conn:
        if req.rating == 0:
            conn.execute("DELETE FROM feedback WHERE message_id=?", (req.message_id,))
        else:
            conn.execute(
                "INSERT INTO feedback (session_id, message_id, rating, comment, created_at) "
                "SELECT session_id, ?, ?, ?, ? FROM messages WHERE id=? "
                "ON CONFLICT(message_id) DO UPDATE SET rating=excluded.rating, comment=excluded.comment",
                (req.message_id, req.rating, req.comment, time.time(), req.message_id),
            )
        conn.commit()
    return {"ok": True, "rating": req.rating}


@app.get("/api/sessions/search")
def search_sessions(q: str = Query(..., min_length=1), _: None = Security(_check_key)):
    """搜尋對話：同時比對 session 標題與 message 內容。"""
    like = f"%{q}%"
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT s.session_id, s.title, s.updated_at,
                   (SELECT content FROM messages
                    WHERE session_id=s.session_id AND content LIKE ?
                    ORDER BY created_at ASC LIMIT 1) AS hit,
                   (SELECT COUNT(*) FROM messages
                    WHERE session_id=s.session_id AND content LIKE ?) AS hit_count
            FROM sessions s
            WHERE s.title LIKE ? OR EXISTS (
                SELECT 1 FROM messages m
                WHERE m.session_id = s.session_id AND m.content LIKE ?
            )
            ORDER BY s.updated_at DESC
            LIMIT 30
            """,
            (like, like, like, like),
        ).fetchall()
    results = []
    for sid, title, updated_at, hit, hit_count in rows:
        snippet = hit or ""
        if len(snippet) > 80:
            idx = snippet.lower().find(q.lower())
            start = max(0, idx - 20)
            snippet = ("…" if start > 0 else "") + snippet[start : start + 80] + "…"
        results.append(
            {
                "session_id": sid,
                "title": title,
                "updated_at": updated_at,
                "snippet": snippet,
                "hit_count": hit_count or 0,
            }
        )
    return results


@app.post("/api/sessions/{session_id}/auto-title")
async def auto_title(session_id: str, _: None = Security(_check_key)):
    """根據前兩則訊息請 LLM 生成 10 字內標題。"""
    msgs = _get_history(session_id)
    if len(msgs) < 2:
        raise HTTPException(status_code=400, detail="尚無足夠對話可生成標題")
    user_msg = next((m["content"] for m in msgs if m["role"] == "user"), "")[:400]
    ai_msg = next((m["content"] for m in msgs if m["role"] == "assistant"), "")[:400]
    prompt = (
        "請依以下對話生成一個 10 個中文字以內的簡潔標題，"
        "只回傳標題本身、不要任何引號或標點：\n\n"
        f"使用者：{user_msg}\n助理：{ai_msg}"
    )
    try:
        answer, _ = await _request_chat_backend(
            prompt,
            [],
            use_rag=False,
            top_k=1,
        )
        title = answer.strip().strip('「」"\'`。.')
        title = title.split("\n")[0][:30] or "新對話"
    except HTTPException:
        title = user_msg[:20] or "新對話"

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ?",
            (title, time.time(), session_id),
        )
        conn.commit()
    return {"ok": True, "title": title}


# ---------------------------------------------------------------------------
# Serve single-page HTML
# ---------------------------------------------------------------------------

_HTML = Path(__file__).parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_HTML.read_text(encoding="utf-8"))
