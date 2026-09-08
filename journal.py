import os, sqlite3, threading, time
from contextlib import contextmanager

DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
SQLITE_PATH = os.getenv('JOURNAL_DB_PATH', '/tmp/bybit_agent.db')
_lock = threading.RLock()


def _is_pg():
    return DATABASE_URL.startswith('postgres://') or DATABASE_URL.startswith('postgresql://')

@contextmanager
def _conn():
    if _is_pg():
        try:
            import psycopg
        except ImportError as e:
            raise RuntimeError('DATABASE_URL is set but psycopg is not installed') from e
        c = psycopg.connect(DATABASE_URL)
        try:
            yield c
            c.commit()
        finally:
            c.close()
    else:
        parent = os.path.dirname(SQLITE_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        c = sqlite3.connect(SQLITE_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init_db():
    with _lock, _conn() as c:
        cur = c.cursor()
        if _is_pg():
            cur.execute('''CREATE TABLE IF NOT EXISTS trade_journal (
                id BIGSERIAL PRIMARY KEY,
                external_id TEXT NOT NULL UNIQUE,
                mode TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT,
                qty DOUBLE PRECISION,
                entry_price DOUBLE PRECISION,
                exit_price DOUBLE PRECISION,
                pnl DOUBLE PRECISION,
                fee DOUBLE PRECISION,
                created_ms BIGINT,
                updated_ms BIGINT,
                reason TEXT,
                raw_json TEXT,
                synced_at DOUBLE PRECISION NOT NULL
            )''')
        else:
            cur.execute('''CREATE TABLE IF NOT EXISTS trade_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT NOT NULL UNIQUE,
                mode TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT,
                qty REAL,
                entry_price REAL,
                exit_price REAL,
                pnl REAL,
                fee REAL,
                created_ms INTEGER,
                updated_ms INTEGER,
                reason TEXT,
                raw_json TEXT,
                synced_at REAL NOT NULL
            )''')
        if _is_pg():
            cur.execute('''CREATE TABLE IF NOT EXISTS agent_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
        else:
            cur.execute('''CREATE TABLE IF NOT EXISTS agent_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)''')


def upsert_closed_pnl(mode, item):
    import json
    ext = str(item.get('orderId') or item.get('orderLinkId') or item.get('execId') or f"{item.get('symbol')}:{item.get('updatedTime')}:{item.get('closedPnl')}")
    row = (
        ext, mode, str(item.get('symbol') or ''), str(item.get('side') or ''),
        float(item.get('qty') or 0), float(item.get('avgEntryPrice') or item.get('entryPrice') or 0),
        float(item.get('avgExitPrice') or item.get('exitPrice') or 0), float(item.get('closedPnl') or 0),
        float(item.get('cumEntryValue') or 0) * 0,  # fee is not guaranteed on closed-pnl payload
        int(item.get('createdTime') or 0), int(item.get('updatedTime') or 0),
        'bybit_closed_pnl', json.dumps(item, ensure_ascii=False, separators=(',', ':')), time.time()
    )
    with _lock, _conn() as c:
        cur = c.cursor()
        if _is_pg():
            cur.execute('''INSERT INTO trade_journal
                (external_id,mode,symbol,side,qty,entry_price,exit_price,pnl,fee,created_ms,updated_ms,reason,raw_json,synced_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (external_id) DO UPDATE SET
                pnl=EXCLUDED.pnl, exit_price=EXCLUDED.exit_price, updated_ms=EXCLUDED.updated_ms,
                raw_json=EXCLUDED.raw_json, synced_at=EXCLUDED.synced_at''', row)
        else:
            cur.execute('''INSERT INTO trade_journal
                (external_id,mode,symbol,side,qty,entry_price,exit_price,pnl,fee,created_ms,updated_ms,reason,raw_json,synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(external_id) DO UPDATE SET
                pnl=excluded.pnl, exit_price=excluded.exit_price, updated_ms=excluded.updated_ms,
                raw_json=excluded.raw_json, synced_at=excluded.synced_at''', row)


def sync_closed_pnl(mode, items):
    init_db()
    count = 0
    for item in items or []:
        try:
            upsert_closed_pnl(mode, item)
            count += 1
        except Exception:
            continue
    return count


def recent(limit=100):
    init_db()
    with _lock, _conn() as c:
        cur = c.cursor()
        if _is_pg():
            cur.execute('SELECT external_id,mode,symbol,side,qty,entry_price,exit_price,pnl,fee,created_ms,updated_ms,reason FROM trade_journal ORDER BY COALESCE(updated_ms,0) DESC LIMIT %s', (int(limit),))
        else:
            cur.execute('SELECT external_id,mode,symbol,side,qty,entry_price,exit_price,pnl,fee,created_ms,updated_ms,reason FROM trade_journal ORDER BY COALESCE(updated_ms,0) DESC LIMIT ?', (int(limit),))
        return [dict(r) for r in cur.fetchall()]


def get_setting(key, default=None):
    init_db()
    with _lock, _conn() as c:
        cur=c.cursor()
        if _is_pg(): cur.execute('SELECT value FROM agent_settings WHERE key=%s',(str(key),))
        else: cur.execute('SELECT value FROM agent_settings WHERE key=?',(str(key),))
        row=cur.fetchone()
        if not row: return default
        return row[0] if not isinstance(row, dict) else row.get('value', default)

def set_setting(key, value):
    init_db()
    with _lock, _conn() as c:
        cur=c.cursor()
        if _is_pg():
            cur.execute('''INSERT INTO agent_settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value''',(str(key),str(value)))
        else:
            cur.execute('''INSERT INTO agent_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value''',(str(key),str(value)))
    return str(value)
