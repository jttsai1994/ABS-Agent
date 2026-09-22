"""FIT Knowledge Chatbot web application.

Serves a single-page chat UI backed by either a published Microsoft Foundry
Agent or the existing RAG API. Conversation history is stored in local SQLite.

Environment variables:
    CHAT_PROVIDER  - "foundry_agent" or "rag" (default: rag)
    FOUNDRY_AGENTS_FILE - JSON file containing allowed Foundry agents
    FOUNDRY_DEFAULT_AGENT_ID - default agent ID from the JSON file
    RAG_BASE_URL   - base URL of the RAG API (default http://localhost:8000)
    RAG_API_KEY    - X-API-Key for RAG API (optional)
    CHAT_DB_PATH   - path to SQLite file (default: ./chat.db)
    CHATBOT_API_KEY - bearer token to protect this chatbot (optional)

Run:
    python -m uvicorn chatbot.app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, Security
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security.api_key import APIKeyHeader
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from dotenv import load_dotenv
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).with_name(".env"))

logger = logging.getLogger(__name__)

CHAT_PROVIDER: str = os.getenv("CHAT_PROVIDER", "rag").strip().lower()
RAG_BASE_URL: str = os.getenv("RAG_BASE_URL", "http://localhost:8000")
RAG_API_KEY: str = os.getenv("RAG_API_KEY", "")
FOUNDRY_AGENTS_FILE: str = os.getenv(
    "FOUNDRY_AGENTS_FILE",
    str(Path(__file__).with_name("agents.json")),
).strip()
FOUNDRY_DEFAULT_AGENT_ID: str = os.getenv(
    "FOUNDRY_DEFAULT_AGENT_ID",
    "sharepoint",
).strip()
AZURE_TENANT_ID: str = os.getenv("AZURE_TENANT_ID", "").strip()
DB_PATH: str = os.getenv("CHAT_DB_PATH", str(Path(__file__).parent / "chat.db"))
CHATBOT_API_KEY: str = os.getenv("CHATBOT_API_KEY", "")
LOCAL_AUTH_ENABLED: bool = os.getenv("LOCAL_AUTH_ENABLED", "true").strip().lower() == "true"
ALLOW_SELF_REGISTRATION: bool = (
    os.getenv("ALLOW_SELF_REGISTRATION", "true").strip().lower() == "true"
)
BOOTSTRAP_ADMIN_USERNAME: str = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "").strip()
BOOTSTRAP_ADMIN_PASSWORD: str = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
AUTH_SESSION_DAYS: int = int(os.getenv("AUTH_SESSION_DAYS", "14"))
HISTORY_LIMIT: int = 200  # max messages returned per session

SESSION_COOKIE = "abs_session"
CSRF_HEADER = "X-CSRF-Token"
password_hasher = PasswordHasher()

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
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin', 'user')),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_login_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                group_id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                description TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS group_members (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(group_id, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                csrf_token TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL,
                last_seen_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                action TEXT NOT NULL,
                resource_type TEXT,
                resource_id TEXT,
                details TEXT,
                ip_address TEXT,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                folder_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(user_id, name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                tag_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                color TEXT NOT NULL DEFAULT '#64748b',
                created_at REAL NOT NULL,
                UNIQUE(user_id, name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_tags (
                session_id TEXT NOT NULL,
                tag_id TEXT NOT NULL,
                PRIMARY KEY(session_id, tag_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id  TEXT    PRIMARY KEY,
                title       TEXT    DEFAULT '新對話',
                agent_id    TEXT,
                user_id     TEXT,
                folder_id   TEXT,
                is_temporary INTEGER NOT NULL DEFAULT 0,
                is_pinned   INTEGER NOT NULL DEFAULT 0,
                is_archived INTEGER NOT NULL DEFAULT 0,
                expires_at  REAL,
                created_at  REAL    NOT NULL,
                updated_at  REAL    NOT NULL
            )
        """)
        session_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        for name, definition in (
            ("agent_id", "TEXT"),
            ("user_id", "TEXT"),
            ("folder_id", "TEXT"),
            ("is_temporary", "INTEGER NOT NULL DEFAULT 0"),
            ("is_pinned", "INTEGER NOT NULL DEFAULT 0"),
            ("is_archived", "INTEGER NOT NULL DEFAULT 0"),
            ("expires_at", "REAL"),
        ):
            if name not in session_columns:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at)"
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
        if BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD:
            existing_admin = conn.execute(
                "SELECT 1 FROM users WHERE role='admin' LIMIT 1"
            ).fetchone()
            if not existing_admin:
                now = time.time()
                conn.execute(
                    "INSERT INTO users (user_id, username, password_hash, role, created_at, updated_at) VALUES (?, ?, ?, 'admin', ?, ?)",
                    (
                        str(uuid.uuid4()),
                        BOOTSTRAP_ADMIN_USERNAME,
                        password_hasher.hash(BOOTSTRAP_ADMIN_PASSWORD),
                        now,
                        now,
                    ),
                )
        conn.commit()


_init_db()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _audit(
    action: str,
    request: Request | None = None,
    *,
    user_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    ip_address = request.client.host if request and request.client else None
    with _db() as conn:
        conn.execute(
            "INSERT INTO audit_logs (user_id, action, resource_type, resource_id, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                action,
                resource_type,
                resource_id,
                json.dumps(details or {}, ensure_ascii=False),
                ip_address,
                time.time(),
            ),
        )


def _user_groups(user_id: str) -> list[str]:
    with _db() as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT g.name FROM groups g JOIN group_members gm ON gm.group_id=g.group_id WHERE gm.user_id=? ORDER BY g.name",
                (user_id,),
            ).fetchall()
        ]


def _public_user(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
        "groups": _user_groups(row["user_id"]),
    }


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _create_auth_session(user_id: str) -> tuple[str, str]:
    token, csrf_token = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    now = time.time()
    with _db() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, csrf_token, expires_at, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            (_token_hash(token), user_id, csrf_token, now + AUTH_SESSION_DAYS * 86400, now, now),
        )
    return token, csrf_token


def _get_current_user(request: Request) -> dict[str, Any]:
    if not LOCAL_AUTH_ENABLED:
        return {"user_id": "legacy", "username": "legacy", "role": "admin", "groups": []}
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="請先登入")
    with _db() as conn:
        row = conn.execute(
            "SELECT u.*, s.csrf_token, s.expires_at FROM auth_sessions s JOIN users u ON u.user_id=s.user_id WHERE s.token_hash=?",
            (_token_hash(token),),
        ).fetchone()
        if not row or not row["is_active"] or row["expires_at"] < time.time():
            conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (_token_hash(token),))
            raise HTTPException(status_code=401, detail="登入已失效，請重新登入")
        conn.execute(
            "UPDATE auth_sessions SET last_seen_at=? WHERE token_hash=?",
            (time.time(), _token_hash(token)),
        )
    user = _public_user(row)
    user["csrf_token"] = row["csrf_token"]
    return user


def _require_csrf(request: Request, user: dict[str, Any]) -> None:
    if LOCAL_AUTH_ENABLED and not secrets.compare_digest(
        request.headers.get(CSRF_HEADER, ""), user.get("csrf_token", "")
    ):
        raise HTTPException(status_code=403, detail="無效的 CSRF token")


def _require_admin(user: dict[str, Any] = Depends(_get_current_user)) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理員權限")
    return user


def _agent_allowed(agent: dict[str, str], user: dict[str, Any]) -> bool:
    allowed_roles = agent.get("allowed_roles", [])
    allowed_groups = agent.get("allowed_groups", [])
    return user["role"] == "admin" or (
        (not allowed_roles or user["role"] in allowed_roles)
        and (not allowed_groups or bool(set(user["groups"]) & set(allowed_groups)))
    )


def _ensure_session(
    session_id: str,
    requested_agent_id: str | None,
    user: dict[str, Any],
    *,
    temporary: bool = False,
) -> str | None:
    """Create a session or enforce its existing Agent binding."""
    selected_agent_id: str | None = None
    if CHAT_PROVIDER == "foundry_agent":
        agent = _get_foundry_agent(requested_agent_id)
        if not _agent_allowed(agent, user):
            raise HTTPException(status_code=403, detail="您沒有使用此 Agent 的權限")
        selected_agent_id = agent["id"]

    now = time.time()
    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT agent_id, user_id FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO sessions "
                "(session_id, title, agent_id, user_id, is_temporary, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    "新對話",
                    selected_agent_id,
                    user["user_id"],
                    int(temporary),
                    now,
                    now,
                ),
            )
            conn.commit()
            return selected_agent_id

        stored_agent_id, owner_id = existing
        if (not owner_id or owner_id != user["user_id"]) and user["role"] != "admin":
            raise HTTPException(status_code=404, detail="找不到對話")
        if CHAT_PROVIDER != "foundry_agent":
            return None
        if not stored_agent_id:
            conn.execute(
                "UPDATE sessions SET agent_id = ? WHERE session_id = ?",
                (selected_agent_id, session_id),
            )
            conn.commit()
            return selected_agent_id
        if stored_agent_id != selected_agent_id:
            raise HTTPException(
                status_code=409,
                detail="此對話已綁定其他 Agent；請建立新對話後再切換。",
            )
        _get_foundry_agent(stored_agent_id)
        return stored_agent_id


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


def _load_foundry_agents() -> dict[str, dict[str, Any]]:
    """Load and validate the server-side Foundry Agent allowlist."""
    if CHAT_PROVIDER != "foundry_agent":
        return {}

    config_path = Path(FOUNDRY_AGENTS_FILE)
    if not config_path.is_absolute():
        config_path = Path(__file__).parent / config_path
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"無法讀取 Foundry Agent 設定檔：{config_path}") from exc

    agents: dict[str, dict[str, Any]] = {}
    for item in raw.get("agents", []):
        if not item.get("enabled", True):
            continue
        required = ("id", "label", "project_endpoint", "agent_name", "version")
        missing = [name for name in required if not str(item.get(name, "")).strip()]
        if missing:
            raise RuntimeError(f"Agent 設定缺少欄位：{', '.join(missing)}")
        agent_id = str(item["id"]).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,49}", agent_id):
            raise RuntimeError(f"Agent id 格式無效：{agent_id}")
        if agent_id in agents:
            raise RuntimeError(f"Agent id 重複：{agent_id}")
        endpoint = str(item["project_endpoint"]).strip().rstrip("/")
        if not endpoint.startswith("https://") or "/api/projects/" not in endpoint:
            raise RuntimeError(f"Agent Project Endpoint 格式無效：{agent_id}")
        allowed_roles = item.get("allowed_roles", [])
        allowed_groups = item.get("allowed_groups", [])
        if not isinstance(allowed_roles, list) or not all(
            role in ("admin", "user") for role in allowed_roles
        ):
            raise RuntimeError(f"Agent allowed_roles 格式無效：{agent_id}")
        if not isinstance(allowed_groups, list) or not all(
            isinstance(group, str) and group.strip() for group in allowed_groups
        ):
            raise RuntimeError(f"Agent allowed_groups 格式無效：{agent_id}")
        agents[agent_id] = {
            "id": agent_id,
            "label": str(item["label"]).strip(),
            "project_endpoint": endpoint,
            "agent_name": str(item["agent_name"]).strip(),
            "version": str(item["version"]).strip(),
            "allowed_roles": allowed_roles,
            "allowed_groups": [group.strip() for group in allowed_groups],
        }

    if not agents:
        raise RuntimeError("Foundry Agent 設定檔沒有已啟用的 Agent")
    if FOUNDRY_DEFAULT_AGENT_ID not in agents:
        raise RuntimeError(f"FOUNDRY_DEFAULT_AGENT_ID 不存在：{FOUNDRY_DEFAULT_AGENT_ID}")
    return agents


FOUNDRY_AGENTS = _load_foundry_agents()


def _get_foundry_agent(agent_id: str | None) -> dict[str, Any]:
    """Resolve a requested ID against the allowlist without exposing endpoints."""
    selected_id = (agent_id or FOUNDRY_DEFAULT_AGENT_ID).strip()
    agent = FOUNDRY_AGENTS.get(selected_id)
    if not agent:
        raise HTTPException(status_code=400, detail="無效或未啟用的 Agent")
    return agent


def _foundry_error_event(exc: Exception) -> dict[str, str]:
    """Return a safe client error while keeping credential details server-side."""
    details = " ".join(
        str(error) for error in (exc, exc.__cause__, exc.__context__) if error is not None
    ).lower()
    auth_markers = (
        "defaultazurecredential",
        "clientauthenticationerror",
        "credentialunavailable",
        "failed to retrieve a token",
        "refresh token has expired",
        "azureclicredential",
    )
    if any(marker in details for marker in auth_markers):
        command = "az login --use-device-code"
        if AZURE_TENANT_ID:
            command += f" --tenant {AZURE_TENANT_ID}"
        return {
            "type": "error",
            "code": "azure_auth_required",
            "text": "Azure 登入已失效。請在啟動服務的終端機重新登入，完成後按「重試」。",
            "command": command,
            "action_url": "https://microsoft.com/devicelogin",
            "action_label": "開啟 Microsoft 裝置登入頁",
        }

    return {
        "type": "error",
        "code": "foundry_request_failed",
        "text": "Foundry Agent 暫時無法使用，請稍後重試；詳細資訊請查看伺服器日誌。",
    }


def _call_foundry_agent_sync(
    message: str,
    history: list[dict[str, str]],
    agent: dict[str, str],
) -> str:
    """Use the official Foundry SDK and published agent reference."""
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
            endpoint=agent["project_endpoint"],
            credential=credential,
        ) as project_client:
            with project_client.get_openai_client() as openai_client:
                response = openai_client.responses.create(
                    input=agent_input,
                    extra_body={
                        "agent_reference": {
                            "name": agent["agent_name"],
                            "version": agent["version"],
                            "type": "agent_reference",
                        }
                    },
                )

    answer = (response.output_text or "").strip()
    return answer or "（Agent 未回傳文字內容）"


async def _stream_foundry_agent(
    message: str,
    history: list[dict[str, str]],
    agent: dict[str, str],
) -> AsyncIterator[str]:
    """Yield text deltas from the Foundry Responses streaming API."""
    try:
        from azure.ai.projects.aio import AIProjectClient
        from azure.identity.aio import DefaultAzureCredential
    except ImportError as exc:
        raise RuntimeError("缺少 Foundry 非同步 SDK；請安裝 chatbot/requirements.txt") from exc

    agent_input = [
        {"role": item["role"], "content": item["content"]}
        for item in history
        if item.get("role") in ("user", "assistant") and item.get("content")
    ]
    agent_input.append({"role": "user", "content": message})

    async with DefaultAzureCredential() as credential:
        async with AIProjectClient(
            endpoint=agent["project_endpoint"],
            credential=credential,
        ) as project_client:
            async with project_client.get_openai_client() as openai_client:
                stream = await openai_client.responses.create(
                    input=agent_input,
                    extra_body={
                        "agent_reference": {
                            "name": agent["agent_name"],
                            "version": agent["version"],
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
    agent_id: str | None,
    use_rag: bool,
    top_k: int,
    category_filter: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Call the configured chat provider and normalize its response."""
    if CHAT_PROVIDER == "foundry_agent":
        agent = _get_foundry_agent(agent_id)
        try:
            answer = await asyncio.to_thread(
                _call_foundry_agent_sync,
                question,
                history,
                agent,
            )
        except Exception as exc:
            logger.exception("Foundry Agent request failed")
            raise HTTPException(
                status_code=502,
                detail=_foundry_error_event(exc),
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


def _get_history(session_id: str, user: dict[str, Any]) -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        owner = conn.execute(
            "SELECT user_id FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not owner or ((not owner[0] or owner[0] != user["user_id"]) and user["role"] != "admin"):
            raise HTTPException(status_code=404, detail="找不到對話")
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


def _list_sessions(
    user: dict[str, Any], limit: int = 50, *, archived: bool = False
) -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT s.session_id,
                   s.title,
                   s.created_at,
                   s.updated_at,
                     s.agent_id,
                   MIN(m.content)      AS first_msg,
                   MAX(m.created_at)   AS last_at,
                     COUNT(m.id)         AS msg_count,
                     s.is_pinned, s.is_archived, s.is_temporary, s.folder_id,
                       (SELECT f.name FROM folders f WHERE f.folder_id=s.folder_id) AS folder_name,
                       (SELECT GROUP_CONCAT(t.name, '|') FROM tags t JOIN session_tags st ON st.tag_id=t.tag_id WHERE st.session_id=s.session_id) AS tag_names
            FROM   sessions s
            LEFT   JOIN messages m ON s.session_id = m.session_id AND m.role = 'user'
                 WHERE  s.user_id = ? AND s.is_archived = ? AND s.is_temporary = 0
            GROUP  BY s.session_id
                 ORDER  BY s.is_pinned DESC, s.updated_at DESC
            LIMIT  ?
            """,
            (user["user_id"], int(archived), limit),
        ).fetchall()
    result = []
    for r in rows:
        preview = r[5] if r[5] else ""
        if len(preview) > 42:
            preview = preview[:42] + "…"
        result.append(
            {
                "session_id": r[0],
                "title": r[1],
                "created_at": r[2],
                "updated_at": r[3],
                "agent_id": r[4],
                "first_msg": preview,
                "last_at": r[6],
                "msg_count": r[7],
                "is_pinned": bool(r[8]),
                "is_archived": bool(r[9]),
                "is_temporary": bool(r[10]),
                "folder_id": r[11],
                "folder_name": r[12],
                "tags": (r[13] or "").split("|") if r[13] else [],
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
    agent_id: str | None = None
    use_rag: bool = True  # 是否使用 RAG 查詢知識庫，預設為 True
    top_k: int = 5
    category_filter: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    temporary: bool = False


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    sources: list[dict[str, Any]]


class CredentialsRequest(BaseModel):
    username: str
    password: str
    confirm_password: str | None = None


def _validate_credentials(
    req: CredentialsRequest, *, require_confirmation: bool = False
) -> tuple[str, str]:
    username = req.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,50}", username):
        raise HTTPException(status_code=400, detail="帳號需為 3 至 50 個英數、底線、點或連字號")
    if len(req.password) < 12 or len(req.password) > 256:
        raise HTTPException(status_code=400, detail="密碼長度需介於 12 至 256 字元")
    if require_confirmation and req.confirm_password != req.password:
        raise HTTPException(status_code=400, detail="兩次輸入的密碼不一致")
    return username, req.password


def _login_response(response: Response, user_id: str) -> dict[str, Any]:
    token, csrf_token = _create_auth_session(user_id)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=AUTH_SESSION_DAYS * 86400,
        httponly=True,
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        samesite="lax",
        path="/",
    )
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return {"user": _public_user(row), "csrf_token": csrf_token}


# ---------------------------------------------------------------------------
# Local authentication
# ---------------------------------------------------------------------------


@app.post("/api/auth/register")
def register(req: CredentialsRequest, request: Request, response: Response):
    if not LOCAL_AUTH_ENABLED:
        raise HTTPException(status_code=404, detail="本地登入未啟用")
    if not ALLOW_SELF_REGISTRATION:
        raise HTTPException(status_code=403, detail="目前未開放自行註冊")
    username, password = _validate_credentials(req, require_confirmation=True)
    now, user_id = time.time(), str(uuid.uuid4())
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO users (user_id, username, password_hash, role, created_at, updated_at) VALUES (?, ?, ?, 'user', ?, ?)",
                (user_id, username, password_hasher.hash(password), now, now),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="此帳號已被使用") from exc
    _audit("auth.register", request, user_id=user_id, resource_type="user", resource_id=user_id)
    return _login_response(response, user_id)


@app.post("/api/auth/login")
def login(req: CredentialsRequest, request: Request, response: Response):
    username, password = _validate_credentials(req)
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        valid = False
        if row and row["is_active"]:
            try:
                valid = password_hasher.verify(row["password_hash"], password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                valid = False
            if valid:
                conn.execute(
                    "UPDATE users SET last_login_at=?, updated_at=? WHERE user_id=?",
                    (time.time(), time.time(), row["user_id"]),
                )
        if not valid:
            _audit(
                "auth.login_failed", request, resource_type="user", details={"username": username}
            )
            raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    _audit(
        "auth.login",
        request,
        user_id=row["user_id"],
        resource_type="user",
        resource_id=row["user_id"],
    )
    return _login_response(response, row["user_id"])


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, user: dict[str, Any] = Depends(_get_current_user)):
    _require_csrf(request, user)
    token = request.cookies.get(SESSION_COOKIE, "")
    with _db() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (_token_hash(token),))
    response.delete_cookie(SESSION_COOKIE, path="/")
    _audit("auth.logout", request, user_id=user["user_id"])
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: dict[str, Any] = Depends(_get_current_user)):
    return {
        "user": {key: value for key, value in user.items() if key != "csrf_token"},
        "csrf_token": user.get("csrf_token"),
    }


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    request: Request,
    _: None = Security(_check_key),
    user: dict[str, Any] = Depends(_get_current_user),
) -> ChatResponse:
    _require_csrf(request, user)
    session_id = (req.session_id or "").strip() or str(uuid.uuid4())
    agent_id = _ensure_session(session_id, req.agent_id, user, temporary=req.temporary)

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
        agent_id=agent_id,
        use_rag=req.use_rag,
        top_k=req.top_k,
        category_filter=req.category_filter,
    )

    _save(session_id, "user", req.message)  # 儲原始訊息，不儲展開後的 rag_query
    _save(session_id, "assistant", answer, sources)

    return ChatResponse(session_id=session_id, answer=answer, sources=sources)


@app.post("/api/chat/stream")
async def chat_stream(
    req: ChatRequest,
    request: Request,
    _: None = Security(_check_key),
    user: dict[str, Any] = Depends(_get_current_user),
):
    """Streaming version of /api/chat — proxies SSE from RAG /query/stream or /chat/stream."""
    _require_csrf(request, user)
    session_id = (req.session_id or "").strip() or str(uuid.uuid4())
    agent_id = _ensure_session(session_id, req.agent_id, user, temporary=req.temporary)

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
        agent = _get_foundry_agent(agent_id)

        async def _generate_foundry():
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
            yield f"data: {json.dumps({'type': 'sources', 'sources': []})}\n\n"
            full_answer: list[str] = []
            try:
                async for delta in _stream_foundry_agent(
                    backend_question,
                    history,
                    agent,
                ):
                    full_answer.append(delta)
                    event = {"type": "token", "text": delta}
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as exc:
                logger.exception("Foundry Agent streaming request failed")
                event = _foundry_error_event(exc)
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
def get_history(
    session_id: str,
    _: None = Security(_check_key),
    user: dict[str, Any] = Depends(_get_current_user),
):
    return _get_history(session_id, user)


@app.delete("/api/history/{session_id}")
def delete_history(
    session_id: str,
    request: Request,
    _: None = Security(_check_key),
    user: dict[str, Any] = Depends(_get_current_user),
):
    _require_csrf(request, user)
    with sqlite3.connect(DB_PATH) as conn:
        owner = conn.execute(
            "SELECT user_id FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not owner or ((not owner[0] or owner[0] != user["user_id"]) and user["role"] != "admin"):
            raise HTTPException(status_code=404, detail="找不到對話")
        conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        conn.commit()
    _audit(
        "session.delete",
        request,
        user_id=user["user_id"],
        resource_type="session",
        resource_id=session_id,
    )
    return {"ok": True}


@app.get("/api/sessions")
def get_sessions(
    archived: bool = False,
    _: None = Security(_check_key),
    user: dict[str, Any] = Depends(_get_current_user),
):
    return _list_sessions(user, archived=archived)


@app.get("/api/agents")
def get_agents(_: None = Security(_check_key), user: dict[str, Any] = Depends(_get_current_user)):
    """Return safe public metadata; project endpoints remain server-side."""
    return {
        "default_agent_id": FOUNDRY_DEFAULT_AGENT_ID,
        "agents": [
            {
                "id": agent["id"],
                "label": agent["label"],
                "agent_name": agent["agent_name"],
                "version": agent["version"],
            }
            for agent in FOUNDRY_AGENTS.values()
            if _agent_allowed(agent, user)
        ],
    }


@app.get("/api/health")
def health(_: None = Security(_check_key)):
    """Report backend selection and whether required configuration is present."""
    if CHAT_PROVIDER == "foundry_agent":
        return {
            "status": "ok",
            "provider": CHAT_PROVIDER,
            "default_agent_id": FOUNDRY_DEFAULT_AGENT_ID,
            "agent_count": len(FOUNDRY_AGENTS),
        }
    return {
        "status": "ok" if CHAT_PROVIDER == "rag" else "configuration_error",
        "provider": CHAT_PROVIDER,
        "rag_base_url": RAG_BASE_URL if CHAT_PROVIDER == "rag" else None,
    }


@app.put("/api/sessions/{session_id}")
def rename_session(
    session_id: str,
    request: Request,
    title: str = Query(...),
    _: None = Security(_check_key),
    user: dict[str, Any] = Depends(_get_current_user),
):
    """更新會話標題。"""
    title = (title or "").strip() or "新對話"
    _require_csrf(request, user)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ? AND user_id = ?",
            (title, time.time(), session_id, user["user_id"]),
        )
        if not conn.total_changes:
            raise HTTPException(status_code=404, detail="找不到對話")
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
def submit_feedback(
    req: FeedbackRequest,
    request: Request,
    _: None = Security(_check_key),
    user: dict[str, Any] = Depends(_get_current_user),
):
    _require_csrf(request, user)
    if req.rating not in (-1, 0, 1):
        raise HTTPException(status_code=400, detail="rating must be -1, 0 or 1")
    with sqlite3.connect(DB_PATH) as conn:
        if req.rating == 0:
            conn.execute("DELETE FROM feedback WHERE message_id=?", (req.message_id,))
        else:
            conn.execute(
                "INSERT INTO feedback (session_id, message_id, rating, comment, created_at) "
                "SELECT m.session_id, ?, ?, ?, ? FROM messages m JOIN sessions s ON s.session_id=m.session_id WHERE m.id=? AND s.user_id=? "
                "ON CONFLICT(message_id) DO UPDATE SET rating=excluded.rating, comment=excluded.comment",
                (
                    req.message_id,
                    req.rating,
                    req.comment,
                    time.time(),
                    req.message_id,
                    user["user_id"],
                ),
            )
        conn.commit()
    return {"ok": True, "rating": req.rating}


@app.get("/api/sessions/search")
def search_sessions(
    q: str = Query(..., min_length=1),
    _: None = Security(_check_key),
    user: dict[str, Any] = Depends(_get_current_user),
):
    """搜尋對話：同時比對 session 標題與 message 內容。"""
    like = f"%{q}%"
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT s.session_id, s.title, s.updated_at, s.agent_id,
                   (SELECT content FROM messages
                    WHERE session_id=s.session_id AND content LIKE ?
                    ORDER BY created_at ASC LIMIT 1) AS hit,
                   (SELECT COUNT(*) FROM messages
                    WHERE session_id=s.session_id AND content LIKE ?) AS hit_count
            FROM sessions s
            WHERE s.user_id = ? AND s.is_temporary = 0 AND (s.title LIKE ? OR EXISTS (
                SELECT 1 FROM messages m
                WHERE m.session_id = s.session_id AND m.content LIKE ?
            ))
            ORDER BY s.updated_at DESC
            LIMIT 30
            """,
            (like, like, user["user_id"], like, like),
        ).fetchall()
    results = []
    for sid, title, updated_at, agent_id, hit, hit_count in rows:
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
                "agent_id": agent_id,
                "snippet": snippet,
                "hit_count": hit_count or 0,
            }
        )
    return results


@app.post("/api/sessions/{session_id}/auto-title")
async def auto_title(
    session_id: str,
    request: Request,
    _: None = Security(_check_key),
    user: dict[str, Any] = Depends(_get_current_user),
):
    """根據前兩則訊息請 LLM 生成 10 字內標題。"""
    _require_csrf(request, user)
    msgs = _get_history(session_id, user)
    if len(msgs) < 2:
        raise HTTPException(status_code=400, detail="尚無足夠對話可生成標題")
    user_msg = next((m["content"] for m in msgs if m["role"] == "user"), "")[:400]
    ai_msg = next((m["content"] for m in msgs if m["role"] == "assistant"), "")[:400]
    prompt = (
        "請依以下對話生成一個 10 個中文字以內的簡潔標題，"
        "只回傳標題本身、不要任何引號或標點：\n\n"
        f"使用者：{user_msg}\n助理：{ai_msg}"
    )
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT agent_id FROM sessions WHERE session_id = ? AND user_id = ?",
            (session_id, user["user_id"]),
        ).fetchone()
    agent_id = row[0] if row else None
    try:
        answer, _ = await _request_chat_backend(
            prompt,
            [],
            agent_id=agent_id,
            use_rag=False,
            top_k=1,
        )
        title = answer.strip().strip('「」"\'`。.')
        title = title.split("\n")[0][:30] or "新對話"
    except HTTPException:
        title = user_msg[:20] or "新對話"

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ? AND user_id = ?",
            (title, time.time(), session_id, user["user_id"]),
        )
        conn.commit()
    return {"ok": True, "title": title}


# ---------------------------------------------------------------------------
# Workspace organisation and administration
# ---------------------------------------------------------------------------


class NameRequest(BaseModel):
    name: str


class TagRequest(NameRequest):
    color: str = "#64748b"


class UserUpdateRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    group_ids: list[str] | None = None


def _owned_session(session_id: str, user: dict[str, Any]) -> None:
    with _db() as conn:
        row = conn.execute(
            "SELECT user_id FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
    if not row or (
        (not row["user_id"] or row["user_id"] != user["user_id"]) and user["role"] != "admin"
    ):
        raise HTTPException(status_code=404, detail="找不到對話")


@app.get("/api/workspace/folders")
def list_folders(_: None = Security(_check_key), user: dict[str, Any] = Depends(_get_current_user)):
    with _db() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT folder_id, name, created_at FROM folders WHERE user_id=? ORDER BY name",
                (user["user_id"],),
            )
        ]


@app.post("/api/workspace/folders")
def create_folder(
    req: NameRequest,
    request: Request,
    _: None = Security(_check_key),
    user: dict[str, Any] = Depends(_get_current_user),
):
    _require_csrf(request, user)
    name = req.name.strip()[:50]
    if not name:
        raise HTTPException(status_code=400, detail="資料夾名稱不可空白")
    folder_id = str(uuid.uuid4())
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO folders (folder_id, user_id, name, created_at) VALUES (?, ?, ?, ?)",
                (folder_id, user["user_id"], name, time.time()),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="已有同名資料夾") from exc
    return {"folder_id": folder_id, "name": name}


@app.get("/api/workspace/tags")
def list_tags(_: None = Security(_check_key), user: dict[str, Any] = Depends(_get_current_user)):
    with _db() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT tag_id, name, color, created_at FROM tags WHERE user_id=? ORDER BY name",
                (user["user_id"],),
            )
        ]


@app.post("/api/workspace/tags")
def create_tag(
    req: TagRequest,
    request: Request,
    _: None = Security(_check_key),
    user: dict[str, Any] = Depends(_get_current_user),
):
    _require_csrf(request, user)
    name, color = req.name.strip()[:30], req.color.strip()
    if not name or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise HTTPException(status_code=400, detail="標籤名稱或色彩格式無效")
    tag_id = str(uuid.uuid4())
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO tags (tag_id, user_id, name, color, created_at) VALUES (?, ?, ?, ?, ?)",
                (tag_id, user["user_id"], name, color, time.time()),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="已有同名標籤") from exc
    return {"tag_id": tag_id, "name": name, "color": color}


@app.put("/api/sessions/{session_id}/state")
def update_session_state(
    session_id: str,
    request: Request,
    pinned: bool | None = None,
    archived: bool | None = None,
    folder_id: str | None = None,
    _: None = Security(_check_key),
    user: dict[str, Any] = Depends(_get_current_user),
):
    _require_csrf(request, user)
    _owned_session(session_id, user)
    updates, values = [], []
    if pinned is not None:
        updates.extend(["is_pinned=?"])
        values.append(int(pinned))
    if archived is not None:
        updates.extend(["is_archived=?"])
        values.append(int(archived))
    if folder_id is not None:
        with _db() as conn:
            if (
                folder_id
                and not conn.execute(
                    "SELECT 1 FROM folders WHERE folder_id=? AND user_id=?",
                    (folder_id, user["user_id"]),
                ).fetchone()
            ):
                raise HTTPException(status_code=400, detail="資料夾不存在")
        updates.extend(["folder_id=?"])
        values.append(folder_id or None)
    if not updates:
        raise HTTPException(status_code=400, detail="沒有可更新的狀態")
    values.extend([time.time(), session_id])
    with _db() as conn:
        conn.execute(
            f"UPDATE sessions SET {', '.join(updates)}, updated_at=? WHERE session_id=?", values
        )
    return {"ok": True}


@app.put("/api/sessions/{session_id}/tags")
def update_session_tags(
    session_id: str,
    tag_ids: list[str],
    request: Request,
    _: None = Security(_check_key),
    user: dict[str, Any] = Depends(_get_current_user),
):
    _require_csrf(request, user)
    _owned_session(session_id, user)
    with _db() as conn:
        valid_ids = {
            row[0]
            for row in conn.execute(
                f"SELECT tag_id FROM tags WHERE user_id=? AND tag_id IN ({','.join('?' for _ in tag_ids) or 'NULL'})",
                (user["user_id"], *tag_ids),
            )
        }
        if valid_ids != set(tag_ids):
            raise HTTPException(status_code=400, detail="包含無效標籤")
        conn.execute("DELETE FROM session_tags WHERE session_id=?", (session_id,))
        conn.executemany(
            "INSERT INTO session_tags (session_id, tag_id) VALUES (?, ?)",
            [(session_id, tag_id) for tag_id in tag_ids],
        )
    return {"ok": True}


@app.get("/api/admin/users")
def admin_users(_: dict[str, Any] = Depends(_require_admin)):
    with _db() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return [_public_user(row) for row in rows]


@app.get("/api/admin/groups")
def admin_groups(_: dict[str, Any] = Depends(_require_admin)):
    with _db() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT g.group_id, g.name, g.description, g.created_at, COUNT(gm.user_id) AS member_count FROM groups g LEFT JOIN group_members gm ON gm.group_id=g.group_id GROUP BY g.group_id ORDER BY g.name"
            )
        ]


@app.post("/api/admin/groups")
def admin_create_group(
    req: NameRequest, request: Request, admin: dict[str, Any] = Depends(_require_admin)
):
    _require_csrf(request, admin)
    name = req.name.strip()[:50]
    if not name:
        raise HTTPException(status_code=400, detail="群組名稱不可空白")
    group_id = str(uuid.uuid4())
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO groups (group_id, name, created_at) VALUES (?, ?, ?)",
                (group_id, name, time.time()),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="已有同名群組") from exc
    _audit(
        "group.create",
        request,
        user_id=admin["user_id"],
        resource_type="group",
        resource_id=group_id,
    )
    return {"group_id": group_id, "name": name}


@app.put("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: str,
    req: UserUpdateRequest,
    request: Request,
    admin: dict[str, Any] = Depends(_require_admin),
):
    _require_csrf(request, admin)
    if req.role is not None and req.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="無效角色")
    with _db() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
            raise HTTPException(status_code=404, detail="找不到使用者")
        if req.role is not None:
            conn.execute(
                "UPDATE users SET role=?, updated_at=? WHERE user_id=?",
                (req.role, time.time(), user_id),
            )
        if req.is_active is not None:
            conn.execute(
                "UPDATE users SET is_active=?, updated_at=? WHERE user_id=?",
                (int(req.is_active), time.time(), user_id),
            )
        if req.group_ids is not None:
            conn.execute("DELETE FROM group_members WHERE user_id=?", (user_id,))
            for group_id in req.group_ids:
                if conn.execute("SELECT 1 FROM groups WHERE group_id=?", (group_id,)).fetchone():
                    conn.execute(
                        "INSERT INTO group_members (group_id, user_id, created_at) VALUES (?, ?, ?)",
                        (group_id, user_id, time.time()),
                    )
    _audit(
        "user.update", request, user_id=admin["user_id"], resource_type="user", resource_id=user_id
    )
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(
    user_id: str,
    request: Request,
    admin: dict[str, Any] = Depends(_require_admin),
):
    """刪除使用者及其所有相關資料（對話、訊息、反饋等）。"""
    _require_csrf(request, admin)
    if user_id == admin["user_id"]:
        raise HTTPException(status_code=400, detail="無法刪除自己")
    with _db() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
            raise HTTPException(status_code=404, detail="找不到使用者")
        # 刪除使用者相關的所有資料
        session_ids = [
            row[0]
            for row in conn.execute(
                "SELECT session_id FROM sessions WHERE user_id=?", (user_id,)
            ).fetchall()
        ]
        for sid in session_ids:
            conn.execute("DELETE FROM feedback WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM session_tags WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM group_members WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM folders WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM tags WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        conn.commit()
    _audit(
        "user.delete", request, user_id=admin["user_id"], resource_type="user", resource_id=user_id
    )
    return {"ok": True}


@app.get("/api/admin/analytics")
def admin_analytics(_: dict[str, Any] = Depends(_require_admin)):
    since = time.time() - 30 * 86400
    with _db() as conn:
        return {
            "users": conn.execute("SELECT COUNT(*) FROM users WHERE is_active=1").fetchone()[0],
            "sessions_30d": conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE created_at>=?", (since,)
            ).fetchone()[0],
            "messages_30d": conn.execute(
                "SELECT COUNT(*) FROM messages WHERE created_at>=?", (since,)
            ).fetchone()[0],
            "feedback": dict(
                conn.execute(
                    "SELECT COALESCE(SUM(rating=1),0) AS positive, COALESCE(SUM(rating=-1),0) AS negative FROM feedback"
                ).fetchone()
            ),
            "agents": [
                dict(row)
                for row in conn.execute(
                    "SELECT agent_id, COUNT(*) AS session_count FROM sessions WHERE created_at>=? GROUP BY agent_id ORDER BY session_count DESC",
                    (since,),
                )
            ],
        }


@app.get("/api/admin/audit")
def admin_audit(limit: int = Query(100, ge=1, le=500), _: dict[str, Any] = Depends(_require_admin)):
    with _db() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT a.*, u.username FROM audit_logs a LEFT JOIN users u ON u.user_id=a.user_id ORDER BY a.created_at DESC LIMIT ?",
                (limit,),
            )
        ]


# ---------------------------------------------------------------------------
# Serve single-page HTML
# ---------------------------------------------------------------------------

_HTML = Path(__file__).parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_HTML.read_text(encoding="utf-8"))
