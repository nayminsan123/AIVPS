import json
import os
import re
import secrets
import sqlite3
import time
from collections import OrderedDict, deque
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pwdlib import PasswordHash
from starlette.middleware.sessions import SessionMiddleware

APP_USERNAME = os.environ["APP_USERNAME"]
APP_PASSWORD = os.environ["APP_PASSWORD"]
SESSION_SECRET = os.environ["SESSION_SECRET"]
LLAMA_URL = os.getenv("LLAMA_URL", "http://llama:8080").rstrip("/")
MODEL_NAME = os.getenv("MODEL_NAME", "Local model")
DB_PATH = os.getenv("DB_PATH", "/data/chat.sqlite3")
if APP_PASSWORD.startswith("replace-") or len(APP_PASSWORD) < 14:
    raise RuntimeError("APP_PASSWORD must be a unique password of at least 14 characters")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,31}", APP_USERNAME):
    raise RuntimeError("APP_USERNAME must be 3–32 letters, numbers, dots, dashes or underscores")
if SESSION_SECRET.startswith("replace-") or len(SESSION_SECRET) < 64:
    raise RuntimeError("SESSION_SECRET must be a random secret of at least 64 characters")

password_hash = PasswordHash.recommended()
dummy_password_hash = password_hash.hash("not-a-real-password-for-timing")
login_attempts = OrderedDict()
DEFAULT_SETTINGS = {
    "system_prompt": "",
    "temperature": 0.7,
    "top_p": 0.9,
    "max_tokens": 1024,
}


class RequestBodyLimitMiddleware:
    def __init__(self, app, max_bytes=1_000_000):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                body = b'{"detail":"Request body too large"}'
                await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            chunks.append(message)
            if not message.get("more_body", False):
                break
        position = 0

        async def replay():
            nonlocal position
            if position < len(chunks):
                message = chunks[position]
                position += 1
                return message
            return await receive()

        await self.app(scope, replay, send)


def connect_db():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


async def json_object(request: Request):
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON request")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    return data


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
            active INTEGER NOT NULL DEFAULT 1,
            session_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        for key, value in DEFAULT_SETTINGS.items():
            db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value)))
        count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            db.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (?, ?, 'admin')",
                (APP_USERNAME, password_hash.hash(APP_PASSWORD)),
            )


init_db()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="chat_session",
    max_age=60 * 60 * 12,
    same_site="strict",
    https_only=True,
)
app.add_middleware(RequestBodyLimitMiddleware, max_bytes=1_000_000)


def check_origin(request: Request):
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if not origin or origin != expected:
        raise HTTPException(status_code=403, detail="Request origin rejected")


def current_user(request: Request):
    user_id = request.session.get("uid")
    version = request.session.get("sv")
    if not isinstance(user_id, int) or not isinstance(version, int):
        return None
    with connect_db() as db:
        user = db.execute("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone()
    if not user or user["session_version"] != version:
        request.session.clear()
        return None
    return user


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    return user


def require_admin(request: Request):
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


def read_settings():
    with connect_db() as db:
        rows = db.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: json.loads(row["value"]) for row in rows}


def validate_username(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,31}", value):
        raise HTTPException(status_code=400, detail="Username must be 3–32 letters, numbers, dots, dashes or underscores")
    return value


def validate_password(value):
    if not isinstance(value, str) or len(value) < 12 or len(value) > 256:
        raise HTTPException(status_code=400, detail="Password must be 12–256 characters")
    return value


def active_admin_count(db):
    return db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1").fetchone()[0]


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def home():
    return FileResponse(Path(__file__).with_name("index.html"))


@app.get("/api/session")
async def session(request: Request):
    user = current_user(request)
    return {
        "authenticated": bool(user),
        "username": user["username"] if user else None,
        "role": user["role"] if user else None,
        "model": MODEL_NAME,
    }


@app.post("/api/login")
async def login(request: Request):
    check_origin(request)
    now = time.monotonic()
    client_ip = request.client.host if request.client else "unknown"
    attempts = login_attempts.pop(client_ip, deque())
    while attempts and now - attempts[0] > 15 * 60:
        attempts.popleft()
    if len(attempts) >= 8:
        login_attempts[client_ip] = attempts
        if len(login_attempts) > 4096:
            login_attempts.popitem(last=False)
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again later.")
    data = await json_object(request)
    username = data.get("username", "")
    password = data.get("password", "")
    if not isinstance(username, str) or not isinstance(password, str):
        raise HTTPException(status_code=400, detail="Invalid sign-in data")
    if len(username) > 128 or len(password) > 256:
        raise HTTPException(status_code=400, detail="Invalid sign-in data")
    with connect_db() as db:
        user = db.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
    # Verify a dummy hash for unknown users to reduce username timing differences.
    if user:
        valid = password_hash.verify(password, user["password_hash"])
    else:
        password_hash.verify(password or "invalid", dummy_password_hash)
        valid = False
    if not user or not user["active"] or not valid:
        attempts.append(now)
        login_attempts[client_ip] = attempts
        if len(login_attempts) > 4096:
            login_attempts.popitem(last=False)
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    login_attempts.pop(client_ip, None)
    request.session.clear()
    request.session.update({"uid": user["id"], "sv": user["session_version"]})
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request):
    check_origin(request)
    user = require_user(request)
    with connect_db() as db:
        db.execute("UPDATE users SET session_version = session_version + 1 WHERE id = ?", (user["id"],))
    request.session.clear()
    return {"ok": True}


@app.get("/api/admin/settings")
async def get_admin_settings(request: Request):
    require_admin(request)
    return {"settings": read_settings(), "context_length": 4096}


@app.put("/api/admin/settings")
async def update_admin_settings(request: Request):
    check_origin(request)
    require_admin(request)
    data = await json_object(request)
    prompt = data.get("system_prompt", "")
    if not isinstance(prompt, str) or len(prompt) > 4000:
        raise HTTPException(status_code=400, detail="System prompt must be at most 4,000 characters")
    try:
        temperature = float(data.get("temperature"))
        top_p = float(data.get("top_p"))
        max_tokens = int(data.get("max_tokens"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid AI setting values")
    if not 0 <= temperature <= 2:
        raise HTTPException(status_code=400, detail="Temperature must be between 0 and 2")
    if not 0.01 <= top_p <= 1:
        raise HTTPException(status_code=400, detail="Top-p must be between 0.01 and 1")
    if not 16 <= max_tokens <= 2048:
        raise HTTPException(status_code=400, detail="Output tokens must be between 16 and 2048")
    values = {"system_prompt": prompt, "temperature": temperature, "top_p": top_p, "max_tokens": max_tokens}
    with connect_db() as db:
        for key, value in values.items():
            db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value)))
    return {"settings": values, "context_length": 4096}


@app.get("/api/admin/users")
async def list_users(request: Request):
    require_admin(request)
    with connect_db() as db:
        users = db.execute("SELECT id, username, role, active, created_at FROM users ORDER BY id").fetchall()
    return {"users": [dict(row) for row in users]}


@app.post("/api/admin/users")
async def add_user(request: Request):
    check_origin(request)
    require_admin(request)
    data = await json_object(request)
    username = validate_username(data.get("username"))
    password = validate_password(data.get("password"))
    role = data.get("role", "user")
    if role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="Role must be user or admin")
    try:
        with connect_db() as db:
            cursor = db.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (?, ?, ?)",
                (username, password_hash.hash(password), role),
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="That username already exists")
    return {"id": user_id, "username": username, "role": role, "active": 1}


@app.patch("/api/admin/users/{user_id}")
async def edit_user(user_id: int, request: Request):
    check_origin(request)
    admin = require_admin(request)
    data = await json_object(request)
    with connect_db() as db:
        target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        role = data.get("role", target["role"])
        active = data.get("active", bool(target["active"]))
        if role not in ("admin", "user") or not isinstance(active, bool):
            raise HTTPException(status_code=400, detail="Invalid role or active value")
        removing_admin = target["role"] == "admin" and target["active"] and (role != "admin" or not active)
        if removing_admin and active_admin_count(db) <= 1:
            raise HTTPException(status_code=409, detail="At least one active administrator must remain")
        new_password = data.get("password")
        if new_password is not None:
            validate_password(new_password)
            db.execute(
                "UPDATE users SET role = ?, active = ?, password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                (role, int(active), password_hash.hash(new_password), user_id),
            )
        else:
            db.execute(
                "UPDATE users SET role = ?, active = ?, session_version = session_version + ? WHERE id = ?",
                (role, int(active), int(not active or role != target["role"]), user_id),
            )
    if user_id == admin["id"] and (role != "admin" or not active or new_password is not None):
        request.session.clear()
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}")
async def delete_user(user_id: int, request: Request):
    check_origin(request)
    admin = require_admin(request)
    with connect_db() as db:
        target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if target["role"] == "admin" and target["active"] and active_admin_count(db) <= 1:
            raise HTTPException(status_code=409, detail="At least one active administrator must remain")
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    if user_id == admin["id"]:
        request.session.clear()
    return {"ok": True}


@app.post("/api/chat")
async def chat(request: Request):
    check_origin(request)
    require_user(request)
    body = await json_object(request)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 40:
        raise HTTPException(status_code=400, detail="Invalid conversation")
    safe_messages = []
    total_chars = 0
    for item in messages:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            raise HTTPException(status_code=400, detail="Invalid message")
        content = item.get("content")
        if not isinstance(content, str) or len(content) > 12000:
            raise HTTPException(status_code=400, detail="Message is too long")
        total_chars += len(content)
        if total_chars > 48000:
            raise HTTPException(status_code=400, detail="Conversation is too long; start a new chat")
        safe_messages.append({"role": item["role"], "content": content})
    settings = read_settings()
    if settings["system_prompt"]:
        safe_messages.insert(0, {"role": "system", "content": settings["system_prompt"]})

    async def generate():
        timeout = httpx.Timeout(connect=15, read=None, write=30, pool=15)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                async with client.stream(
                    "POST", f"{LLAMA_URL}/v1/chat/completions",
                    json={
                        "messages": safe_messages,
                        "stream": True,
                        "max_tokens": settings["max_tokens"],
                        "temperature": settings["temperature"],
                        "top_p": settings["top_p"],
                    },
                ) as response:
                    if response.status_code >= 400:
                        yield "data: [ERROR] Model server returned an error.\n\n"
                        return
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            yield line + "\n\n"
            except httpx.HTTPError:
                yield "data: [ERROR] Could not reach the model server.\n\n"

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "assets"), name="static")
