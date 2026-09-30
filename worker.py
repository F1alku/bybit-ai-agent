import time
from journal import init_db, sync_closed_pnl
from learning_engine import record_lessons_from_closed
from engine import MODE, _demo_closed_pnl
from trader import worker_loop

if __name__ == '__main__':
    init_db()
    # Synchronize the exchange's authoritative closed-PnL history before the first trading cycle.
    if MODE in ('demo','live'):
        try:
            sync_closed_pnl(MODE, _demo_closed_pnl(100))
            record_lessons_from_closed(1000)
        except Exception:
            pass
    worker_loop(60)
