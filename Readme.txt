Customer Order Portal & Chatbot

A customer-facing order portal where a buyer logs in, sees only their own orders, and asks a chatbot
"where is my order / when does it ship / what's the tracking number". Changes, cancellations and
complaints are never actioned by the bot: they become support tickets for the account manager.

Stack: Python, FastAPI, DuckDB, Gemini API. (The React/Next.js frontend is not part of this repo yet.)

Architecture

```
ERP API --(sync.py: allowlist, validate, one transaction)--> DuckDB
                                                               ^
                              app.py (login, orders, tickets, chat endpoint)
                                                               ^
                              chat.py (scoped tools + Gemini + output guard)
```

The portal and the chatbot read only from DuckDB. The ERP is called only by the sync job, because
every ERP call is logged and costs the factory.

| File | Purpose |
| `db.py` | Schema, connection, and `to_dto`, the single customer-facing order shape |
| `sync.py` | ERP to DuckDB sync: retries, validation, atomic write, removal of vanished orders |
| `app.py` | FastAPI: login/logout, orders, tickets, chat endpoint, rate limits, security headers |
| `chat.py` | Gemini tool loop, scoped tools, ticket creation, output guard |
| `manage.py` | Sets a customer's password (the ERP has no passwords) |
| `eval.py` | End-to-end checks against the running API, including attack attempts |
| `.env` | Configuration and secrets (never commit) |

Setup (Windows PowerShell)

powershell
cd "C:\Users\Abrar PC\Desktop\MG Apparel"
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.venv\Scripts\Activate.ps1
pip install fastapi "uvicorn[standard]" duckdb httpx pydantic argon2-cffi python-dotenv
```

Create `.env` next to `db.py` (no quotes, no spaces around `=`):

```
ERP_BASE_URL=http://192.168.144.144:8001
ERP_API_KEY=mg-interview-2026
GEMINI_API_KEY=your-key
GEMINI_MODEL=gemini-2.5-flash
DB_PATH=portal.duckdb
SYNC_IN_APP=0
SYNC_INTERVAL_MIN=10
COOKIE_SECURE=0
TICKET_LIMIT_PER_HOUR=10
```

`SYNC_IN_APP=0` means the server never syncs by itself, so every ERP call is deliberate. Set it to `1`
for a background refresh every `SYNC_INTERVAL_MIN` minutes. Set `COOKIE_SECURE=1` behind HTTPS.

Run sequence

DuckDB allows one process on the database file. Steps 1 to 3 need the **server stopped**.

1. Check the ERP is reachable:** `Test-NetConnection 192.168.144.144 -Port 8001` must show `TcpTestSucceeded : True`.
2. Sync once:** `python sync.py` should print `{'status': 'ok', 'orders': ...}`.
3. See the login emails:**
   `python -c "from db import cur; print(cur().execute('select customer_id, email from users limit 10').fetchall())"`
   then **set a password:** `python manage.py set-password <email-from-the-list>` (min 10 characters).
4. Start the server (Terminal 1):** `uvicorn app:app --port 8000`
5. Run the eval (Terminal 2):**
   ```powershell
   $env:EVAL_EMAIL="<that email>"
   $env:EVAL_PASSWORD="<that password>"
   python eval.py
   ```

To re-sync later: stop the server (Ctrl+C), run `python sync.py`, start the server again.

API

| Method and path | Description |
| `POST /api/login` | `{email, password}`, sets an httpOnly session cookie |
| `POST /api/logout` | Ends the session |
| `GET /api/me` | Company name of the logged-in customer |
| `GET /api/orders?status=&page=&page_size=` | The customer's own orders, status counts, last sync time |
| `GET /api/orders/{po_no}` | One order, or 404 if not found or not theirs |
| `GET /api/tickets` | The customer's own support tickets |
| `POST /api/chat` | `{message}`, returns `{reply, ticket_ids}` |

Security design

Tenant isolation
- The customer ID comes only from the server-side session, never from a request field or the model.
- Every query filters on it. A PO that belongs to someone else returns the same 404 or "not found" answer as one that does not exist.
- PO numbers are validated against `^PO-\d+$` before use.

Field allowlist (not a blocklist)
- `OrderIn` and `CustomerIn` use `extra="ignore"`, so any ERP field not named is dropped. A new internal field added to the ERP fails safe.
- Stored: PO, style, product, colour, quantities, dates, status, carrier, tracking number.
- Never stored or exposed:

| Field | Why excluded |
| `customer_id` | Used for ownership checks only; the customer never needs it |
| `unit_price_usd` | Commercial data; buyers' questions are about shipping (confirm with the business) |
| `credit_limit_usd`, `payment_risk` | Internal financial assessments |
| `account_manager`, `contact_name`, `country` | Internal or not needed for the use case |

Authentication and sessions
- Passwords are hashed with argon2. A hash is verified even for unknown emails (no timing leak), and the error is always "Incorrect email or password".
- Session tokens are random; only their SHA-256 hash is stored. Cookies are httpOnly and SameSite=Lax. Sessions last 8 hours.
- Login is rate limited per IP and per email.

Chatbot (the model is treated as untrusted)
- Three server-side tools only: `list_orders`, `get_order`, `create_ticket`. The server injects the customer ID; the model cannot choose whose data it reads. There is no free-form SQL.
- The model only sees the same customer-safe order shape the API returns.
- Output guard in code: a reply is replaced with a safe message if it mentions a PO not on the account, another customer's name or ID, an internal field name, or another email address.
- The system prompt makes the bot answer only from tool results, quote the revised ship date, admit missing data, decline unrelated topics, and write like an account manager. The prompt is a helper, not the control.
- Limits: 1000 characters per message, 20 messages per minute, at most 5 tool rounds per message, last 6 messages of history, capped output size.

Tickets
- Change, cancel and complaint requests create a ticket (customer, PO, message, timestamp, status `open`) and the bot tells the customer their account manager will follow up. The order is never changed.
- If the model forgets to call the tool, a keyword fallback in code creates the ticket anyway.
- The PO is stored only if the customer owns it. Tickets are limited per customer per hour.

Sync behavior

- Pulls customers and all order pages (page size 50), retrying with exponential backoff and jitter and honoring `Retry-After`. 401/403/404 are not retried.
- A short page in the middle of the pull is treated as an ERP hiccup and re-fetched.
- Everything is validated in memory first, then written in **one transaction**. Any failure rolls back and the previous data stays.
- Rows are stamped with a run ID. In the same transaction, rows from older runs are deleted, so orders the ERP no longer returns do not linger.
- Re-running is safe: upsert by PO, no duplicates, and a lock prevents two syncs at once.
- Refuses to apply a result that is empty, under half the previous order count, or far short of the ERP's `total_records`.
- Bad rows (bad PO, null quantity, odd types) are skipped and counted. Unknown statuses become "Unknown".
- Each run is recorded in `sync_runs` (status, count, skipped, error detail).
- The tracking number is read from `tracking_no`, `tracking_number`, `tracking` or `tracking_num`. If the ERP has none, it is stored as null and the bot says it is not on record.

Eval

`eval.py` logs in as a test customer and prints PASS/FAIL per check plus a total. It covers:
- login and bad-password handling, and that chat requires login
- no internal fields in the orders API
- another customer's PO returns 404, path-injection string rejected, oversized message rejected
- own order status, revised ship date, tracking number (quoted or honestly missing)
- a foreign PO, prompt injection, impersonation of another customer, and pricing/credit requests, all without leaks
- an invoice question the data cannot answer (must not invent)
- a cancel request and a complaint, each creating a ticket
- an off-topic request being declined

It makes 2 ERP calls to find another customer's PO. Chatbot wording checks can occasionally flake; the isolation checks are enforced in code.

Known limitations

- Full pull on every sync, because the ERP has no customer filter or updated-since parameter. Orders that change mid-pull are corrected on the next sync.
- Rate limits are in memory, so they apply per process.
- Tickets are stored but there is no staff view or notification yet.
- No CSRF token beyond SameSite cookies, no MFA, no audit dashboard.
- The frontend is not included.
- The Gemini tool schema uses lowercase types; if the API returns a 400, change them to uppercase in `chat.py`.

With more time

- Next.js mobile-first frontend (login, orders with status filter, chat widget), proxying `/api` so the cookie stays same-origin.
- Staff view or Slack/email notification for new tickets, with their own auth role.
- Incremental sync if the ERP gains an updated-since filter; Redis-backed rate limiting.
- Audit logging, MFA, monitoring and alerting on failed syncs and guard blocks.
- Move from DuckDB to a multi-writer database for production.
