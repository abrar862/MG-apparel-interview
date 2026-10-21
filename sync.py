import logging, os, random, re, threading, time
from collections import Counter
from datetime import date

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

from db import STATUSES, cur, utcnow

log = logging.getLogger("sync")
ERP = os.getenv("ERP_BASE_URL", "http://192.168.144.144:8001").rstrip("/")
HEADERS = {"X-API-Key": os.getenv("ERP_API_KEY", ""), "User-Agent": "customer-portal-sync/1.0"}
PAGE_SIZE = 50python -c "import db, os; print(repr(os.getenv('ERP_API_KEY')))"
_run_lock = threading.Lock()
_CANON = {s.lower(): s for s in STATUSES}


def _s(v, n=200):
    v = None if v is None else str(v).strip()
    return v[:n] if v else None


class OrderIn(BaseModel):
    model_config = ConfigDict(extra="ignore")          # unknown/internal ERP fields are dropped
    po_no: str
    customer_id: str
    style_no: str | None = None
    product: str | None = None
    colour: str | None = None
    qty_pcs: int | None = None
    order_date: date | None = None
    promised_ship_date: date | None = None
    revised_ship_date: date | None = None
    status: str = "Unknown"
    shipped_qty: int | None = None
    carrier: str | None = None
    tracking_no: str | None = Field(default=None, validation_alias=AliasChoices(
        "tracking_no", "tracking_number", "tracking", "tracking_num"))

    @field_validator("po_no", mode="before")
    @classmethod
    def _po(cls, v):
        v = str(v).strip().upper()
        if not re.fullmatch(r"PO-\d+", v):
            raise ValueError("bad PO")
        return v

    @field_validator("customer_id", mode="before")
    @classmethod
    def _cid(cls, v):
        v = _s(v, 50)
        if not v:
            raise ValueError("missing customer")
        return v

    @field_validator("qty_pcs", "shipped_qty", mode="before")
    @classmethod
    def _int(cls, v):
        try:
            n = int(v)
            return n if n >= 0 else None
        except (TypeError, ValueError):
            return None

    @field_validator("order_date", "promised_ship_date", "revised_ship_date", mode="before")
    @classmethod
    def _date(cls, v):
        try:
            return date.fromisoformat(str(v)[:10]) if v else None
        except ValueError:
            return None

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v):
        return _CANON.get(str(v or "").strip().lower(), "Unknown")

    @field_validator("style_no", "product", "colour", "carrier", "tracking_no", mode="before")
    @classmethod
    def _text(cls, v):
        return _s(v)


class CustomerIn(BaseModel):
    model_config = ConfigDict(extra="ignore")          # credit limit, risk, manager: dropped
    customer_id: str
    company_name: str | None = None
    portal_email: str | None = None

    @field_validator("customer_id", mode="before")
    @classmethod
    def _cid(cls, v):
        v = _s(v, 50)
        if not v:
            raise ValueError("missing id")
        return v

    @field_validator("company_name", "portal_email", mode="before")
    @classmethod
    def _t(cls, v):
        return _s(v)


class ERPError(Exception):
    pass


def erp_get(client, path, params=None, attempts=4):
    """GET with retry, exponential backoff + jitter, and Retry-After support."""
    last = None
    for i in range(attempts):
        wait = 0.0
        try:
            r = client.get(ERP + path, params=params)
            log.info("ERP GET %s %s -> %s", path, params, r.status_code)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (400, 401, 403, 404):
                raise ERPError(f"{path} -> {r.status_code}")
            try:
                wait = float(r.headers.get("Retry-After", 0))
            except ValueError:
                wait = 0.0
            last = f"{path} -> {r.status_code}"
        except (httpx.HTTPError, ValueError) as e:      # timeouts, resets, bad JSON
            last = repr(e)
        time.sleep(max(wait, min(20.0, 2 ** i + random.random())))
    raise ERPError(f"gave up: {last}")


def pull_customers(client):
    body = erp_get(client, "/v1/customers")
    out, bad = {}, 0
    for raw in (body.get("data") if isinstance(body, dict) else None) or []:
        try:
            c = CustomerIn.model_validate(raw)
            out[c.customer_id] = c
        except ValidationError:
            bad += 1
    return out, bad


def pull_orders(client):
    orders, bad, raw_rows, page, total_records = {}, 0, 0, 1, None
    while page <= 1000:
        for attempt in range(3):
            body = erp_get(client, "/v1/orders", {"page": page, "page_size": PAGE_SIZE})
            data = body.get("data") if isinstance(body, dict) else None
            total_pages = int(body.get("total_pages") or 0) if isinstance(body, dict) else 0
            # a non-final page must be full; a short page means the ERP hiccupped
            if isinstance(data, list) and (page >= total_pages or len(data) == PAGE_SIZE):
                break
            log.warning("page %s looked incomplete, retrying", page)
            time.sleep(1 + attempt)
        else:
            raise ERPError(f"page {page} unusable")
        total_records = body.get("total_records", total_records)
        for raw in data:
            raw_rows += 1
            try:
                o = OrderIn.model_validate(raw)
                orders[o.po_no] = o                     # dedupe by PO (last one wins)
            except ValidationError:
                bad += 1
        if page >= total_pages:
            break
        page += 1
    if total_records is not None:                       # live ERP: allow small drift, not big gaps
        if abs(raw_rows - int(total_records)) > max(5, 0.02 * int(total_records)):
            raise ERPError(f"incomplete pull: got {raw_rows} of {total_records}")
    return orders, bad


ORDER_COLS = ["po_no", "customer_id", "style_no", "product", "colour", "qty_pcs", "order_date",
              "promised_ship_date", "revised_ship_date", "status", "shipped_qty", "carrier",
              "tracking_no", "sync_run_id"]
UPSERT_ORDER = (f"INSERT INTO orders ({','.join(ORDER_COLS)}) VALUES ({','.join('?' * len(ORDER_COLS))}) "
                "ON CONFLICT (po_no) DO UPDATE SET " +
                ",".join(f"{c}=excluded.{c}" for c in ORDER_COLS if c != "po_no"))


def run_sync():
    """Pull everything FIRST, validate it, then write in ONE transaction.
    Failure at any point before COMMIT leaves the previous data untouched."""
    if not _run_lock.acquire(blocking=False):
        return {"status": "already_running"}
    c = cur()
    run_id = c.execute("SELECT nextval('sync_seq')").fetchone()[0]
    try:
        with httpx.Client(headers=HEADERS, timeout=httpx.Timeout(15.0)) as client:
            customers, cbad = pull_customers(client)
            orders, obad = pull_orders(client)

        prev = c.execute("SELECT count(*) FROM orders").fetchone()[0]
        if not customers or not orders or (prev >= 50 and len(orders) < prev * 0.5):
            raise ERPError(f"suspicious result ({len(customers)} customers, {len(orders)} orders, had {prev})")

        email_count = Counter((x.portal_email or "").lower() for x in customers.values())
        c.execute("BEGIN")
        try:
            c.executemany(
                "INSERT INTO customers VALUES (?,?,?,?) ON CONFLICT (customer_id) DO UPDATE SET "
                "company_name=excluded.company_name, portal_email=excluded.portal_email, "
                "sync_run_id=excluded.sync_run_id",
                [(x.customer_id, x.company_name, x.portal_email, run_id) for x in customers.values()])
            c.executemany(UPSERT_ORDER, [
                (o.po_no, o.customer_id, o.style_no, o.product, o.colour, o.qty_pcs, o.order_date,
                 o.promised_ship_date, o.revised_ship_date, o.status, o.shipped_qty, o.carrier,
                 o.tracking_no, run_id) for o in orders.values()])
            # anything the ERP no longer returns is removed in the same transaction
            c.execute("DELETE FROM orders WHERE sync_run_id <> ?", [run_id])
            c.execute("DELETE FROM customers WHERE sync_run_id <> ?", [run_id])
            for x in customers.values():                # login identity = portal_email
                e = (x.portal_email or "").strip().lower()
                if not e:
                    continue
                if email_count[e] > 1:
                    log.error("portal_email %s is shared by several customers; skipped", e)
                    continue
                c.execute("INSERT INTO users (customer_id, email) VALUES (?,?) "
                          "ON CONFLICT (customer_id) DO UPDATE SET email=excluded.email",
                          [x.customer_id, e])
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        c.execute("INSERT INTO sync_runs VALUES (?,?,?,?,?,?)",
                  [run_id, utcnow(), "ok", len(orders), obad + cbad, ""])
        log.info("sync ok: %s customers, %s orders, %s skipped", len(customers), len(orders), obad + cbad)
        return {"status": "ok", "orders": len(orders)}
    except Exception as e:
        log.exception("sync failed; previous data kept")
        c.execute("INSERT INTO sync_runs VALUES (?,?,?,?,?,?)",
                  [run_id, utcnow(), "failed", 0, 0, str(e)[:500]])
        return {"status": "failed", "error": str(e)}
    finally:
        _run_lock.release()


def loop(minutes):
    while True:
        try:
            run_sync()
        except Exception:
            log.exception("sync loop error")
        time.sleep(minutes * 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_sync())