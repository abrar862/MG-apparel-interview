import hashlib, logging, os, re, secrets, time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import timedelta
import threading

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

import chat, sync
from db import ORDER_SELECT, STATUSES, cur, to_dto, utcnow

logging.basicConfig(level=logging.INFO)
ph = PasswordHasher()
DUMMY_HASH = ph.hash("not-a-real-password")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
SESSION_HOURS = 8


@asynccontextmanager
async def lifespan(app):
    if os.getenv("SYNC_IN_APP", "1") == "1":
        threading.Thread(target=sync.loop, args=(int(os.getenv("SYNC_INTERVAL_MIN", "10")),),
                         daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

_hits = defaultdict(deque)


def allow(key, limit, window):
    now, q = time.monotonic(), _hits[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


@app.middleware("http")
async def headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


class LoginIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


def current_user(session: str | None = Cookie(default=None)):
    if not session:
        raise HTTPException(401, "Please sign in")
    th = hashlib.sha256(session.encode()).hexdigest()
    row = cur().execute(
        "SELECT s.customer_id, c.company_name FROM sessions s JOIN customers c "
        "ON c.customer_id = s.customer_id WHERE s.token_hash=? AND s.expires_at>?",
        [th, utcnow()]).fetchone()
    if not row:
        raise HTTPException(401, "Please sign in")
    return {"customer_id": row[0], "company": row[1] or "your company", "sid": th}


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    email = body.email.strip().lower()
    ip = request.client.host if request.client else "?"
    if not allow(("login-ip", ip), 30, 300) or not allow(("login", ip, email), 8, 300):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes.")
    c = cur()
    rows = c.execute("SELECT u.customer_id, u.password_hash, c.company_name FROM users u "
                     "JOIN customers c ON c.customer_id=u.customer_id WHERE lower(u.email)=?",
                     [email]).fetchall()
    ok, hashed = False, DUMMY_HASH                       # always hash-verify: no timing/user enumeration
    if len(rows) == 1 and rows[0][1]:
        hashed = rows[0][1]
    try:
        ph.verify(hashed, body.password)
        ok = len(rows) == 1 and bool(rows[0][1])
    except (VerificationError, InvalidHashError):
        ok = False
    if not ok:
        raise HTTPException(401, "Incorrect email or password")
    token = secrets.token_urlsafe(32)
    c.execute("DELETE FROM sessions WHERE expires_at < ?", [utcnow()])
    c.execute("INSERT INTO sessions VALUES (?,?,?)",
              [hashlib.sha256(token.encode()).hexdigest(), rows[0][0],
               utcnow() + timedelta(hours=SESSION_HOURS)])
    response.set_cookie("session", token, httponly=True, secure=COOKIE_SECURE, samesite="lax",
                        max_age=SESSION_HOURS * 3600, path="/")
    return {"company": rows[0][2]}


@app.post("/api/logout")
def logout(response: Response, user=Depends(current_user)):
    cur().execute("DELETE FROM sessions WHERE token_hash=?", [user["sid"]])
    response.delete_cookie("session", path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"company": user["company"]}


@app.get("/api/orders")
def my_orders(status: str | None = None, page: int = Query(1, ge=1),
              page_size: int = Query(25, ge=1, le=100), user=Depends(current_user)):
    if status and status not in STATUSES:
        raise HTTPException(400, "Unknown status")
    cid, c = user["customer_id"], cur()                   # customer_id ONLY from the session
    where, params = "customer_id=?", [cid]
    if status:
        where += " AND status=?"; params.append(status)
    total = c.execute(f"SELECT count(*) FROM orders WHERE {where}", params).fetchone()[0]
    rows = c.execute(f"SELECT {ORDER_SELECT} FROM orders WHERE {where} "
                     "ORDER BY order_date DESC, po_no LIMIT ? OFFSET ?",
                     params + [page_size, (page - 1) * page_size]).fetchall()
    counts = dict(c.execute("SELECT status, count(*) FROM orders WHERE customer_id=? GROUP BY status",
                            [cid]).fetchall())
    synced = c.execute("SELECT max(finished_at) FROM sync_runs WHERE status='ok'").fetchone()[0]
    return {"orders": [to_dto(r) for r in rows], "page": page, "page_size": page_size,
            "total": total, "total_pages": max(1, -(-total // page_size)),
            "status_counts": counts, "last_synced": synced.isoformat() + "Z" if synced else None}


@app.get("/api/orders/{po_no}")
def one_order(po_no: str, user=Depends(current_user)):
    row = None
    if re.fullmatch(r"PO-\d+", po_no.upper()):
        row = cur().execute(f"SELECT {ORDER_SELECT} FROM orders WHERE po_no=? AND customer_id=?",
                            [po_no.upper(), user["customer_id"]]).fetchone()
    if not row:
        raise HTTPException(404, "Order not found")
    return to_dto(row)


@app.get("/api/tickets")
def my_tickets(user=Depends(current_user)):
    rows = cur().execute("SELECT id, po_no, kind, message, status, created_at FROM tickets "
                         "WHERE customer_id=? ORDER BY id DESC LIMIT 50", [user["customer_id"]]).fetchall()
    return [{"id": r[0], "po_no": r[1], "kind": r[2], "message": r[3], "status": r[4],
             "created_at": r[5].isoformat() + "Z"} for r in rows]


@app.post("/api/chat")
def chat_endpoint(body: ChatIn, user=Depends(current_user)):
    if not allow(("chat", user["customer_id"]), 20, 60):
        raise HTTPException(429, "You're sending messages quickly. Please wait a moment.")
    reply, tickets = chat.run_turn(user, body.message.strip())
    return {"reply": reply, "ticket_ids": tickets}