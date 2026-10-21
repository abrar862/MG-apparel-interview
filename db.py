import os, threading
from datetime import datetime, timezone
import duckdb
from dotenv import load_dotenv
load_dotenv()
DB_PATH = os.getenv("DB_PATH", "portal.duckdb")
STATUSES = ["Confirmed", "In Production", "Ready to Ship", "Delayed", "Shipped", "Cancelled"]

# ALLOWLIST: only these columns are ever stored. Prices, credit limit, payment risk,
# account manager, etc. never enter our database, so they can't leak from it.
ORDER_SELECT = ("po_no, style_no, product, colour, qty_pcs, order_date, promised_ship_date, "
                "revised_ship_date, status, shipped_qty, carrier, tracking_no")

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS ticket_seq;
CREATE SEQUENCE IF NOT EXISTS chat_seq;
CREATE SEQUENCE IF NOT EXISTS sync_seq;
CREATE TABLE IF NOT EXISTS customers(
  customer_id VARCHAR PRIMARY KEY, company_name VARCHAR, portal_email VARCHAR, sync_run_id BIGINT);
CREATE TABLE IF NOT EXISTS orders(
  po_no VARCHAR PRIMARY KEY, customer_id VARCHAR NOT NULL, style_no VARCHAR, product VARCHAR,
  colour VARCHAR, qty_pcs INTEGER, order_date DATE, promised_ship_date DATE,
  revised_ship_date DATE, status VARCHAR, shipped_qty INTEGER, carrier VARCHAR,
  tracking_no VARCHAR, sync_run_id BIGINT);
CREATE TABLE IF NOT EXISTS users(
  customer_id VARCHAR PRIMARY KEY, email VARCHAR NOT NULL, password_hash VARCHAR);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash VARCHAR PRIMARY KEY, customer_id VARCHAR NOT NULL, expires_at TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS tickets(
  id BIGINT PRIMARY KEY DEFAULT nextval('ticket_seq'), customer_id VARCHAR NOT NULL,
  po_no VARCHAR, kind VARCHAR, message VARCHAR, summary VARCHAR,
  status VARCHAR DEFAULT 'open', created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS chat_messages(
  id BIGINT DEFAULT nextval('chat_seq'), sid VARCHAR, customer_id VARCHAR,
  role VARCHAR, content VARCHAR, created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS sync_runs(
  run_id BIGINT, finished_at TIMESTAMP, status VARCHAR, orders_loaded INTEGER,
  rows_skipped INTEGER, detail VARCHAR);
"""

_con = None
_lock = threading.Lock()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cur():
    """Per-call cursor on one shared connection (safe across FastAPI worker threads)."""
    global _con
    with _lock:
        if _con is None:
            _con = duckdb.connect(DB_PATH)
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    _con.execute(stmt)
        return _con.cursor()


def _iso(d):
    return d.isoformat() if d else None


def to_dto(r):
    """The ONE customer-facing order shape. Used by the API and by the chatbot tools."""
    po, style, product, colour, qty, od, ps, rs, status, shipped, carrier, tracking = r
    return {
        "po_no": po, "style_no": style, "product": product, "colour": colour,
        "qty_pcs": qty, "shipped_qty": shipped, "status": status,
        "order_date": _iso(od), "promised_ship_date": _iso(ps), "revised_ship_date": _iso(rs),
        "ship_date": _iso(rs or ps),          # revised date wins when the order is delayed
        "ship_date_is_revised": bool(rs),
        "carrier": carrier, "tracking_no": tracking,
    }