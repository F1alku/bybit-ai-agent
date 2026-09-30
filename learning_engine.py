"""Explainable learning layer for the trading agent.

This module learns from CLOSED trades only. It never changes strategy parameters
or opens/closes positions by itself. It produces statistical insights and
a bounded adaptive score adjustment based on closed-trade evidence. It never changes risk limits and never opens/closes a position by itself.
"""
import math
import time
from journal import _conn, _lock, init_db, recent, aliases_for_orders


def init_learning_db():
    init_db()
    with _lock, _conn() as c:
        cur = c.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS learning_trade_meta (
            external_id TEXT PRIMARY KEY,
            mode TEXT,
            symbol TEXT,
            side TEXT,
            strategy TEXT,
            score REAL,
            risk_pct REAL,
            leverage REAL,
            atr_pct REAL,
            market_regime TEXT,
            entry_timing TEXT,
            news_impact TEXT,
            planned_risk REAL,
            stop_loss REAL,
            take_profit REAL,
            entry_price REAL,
            captured_at REAL
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS learning_insights (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS trade_thesis (
            position_key TEXT PRIMARY KEY, symbol TEXT, side TEXT, entry_price REAL,
            initial_score REAL, initial_reasons TEXT, latest_score REAL, latest_decision TEXT,
            latest_positive TEXT, latest_negative TEXT, updated_at REAL
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS learning_lessons (
            external_id TEXT PRIMARY KEY,
            mode TEXT, symbol TEXT, side TEXT, strategy TEXT, outcome TEXT,
            net_pnl REAL, r_multiple REAL, score REAL, planned_risk REAL,
            lesson TEXT NOT NULL, what_went_right TEXT, what_went_wrong TEXT,
            created_at REAL NOT NULL
        )''')
        for col, typ in [('stop_loss','REAL'),('take_profit','REAL'),('entry_price','REAL')]:
            try:
                cur.execute(f'ALTER TABLE learning_trade_meta ADD COLUMN {col} {typ}')
            except Exception:
                pass


def record_trade_meta(meta):
    """Persist contextual data known at entry; safe to call more than once."""
    init_learning_db()
    import json
    ext = str(meta.get('external_id') or meta.get('order_id') or '')
    if not ext:
        return False
    fields = (
        ext, str(meta.get('mode') or ''), str(meta.get('symbol') or ''),
        str(meta.get('side') or ''), str(meta.get('strategy') or ''),
        float(meta.get('score') or 0), float(meta.get('risk_pct') or 0),
        float(meta.get('leverage') or 0), float(meta.get('atr_pct') or 0),
        str(meta.get('market_regime') or ''), str(meta.get('entry_timing') or ''),
        str(meta.get('news_impact') or ''), float(meta.get('planned_risk') or 0), float(meta.get('stop_loss') or 0), float(meta.get('take_profit') or 0), float(meta.get('entry_price') or 0), time.time()
    )
    with _lock, _conn() as c:
        cur = c.cursor()
        if str(c.__class__.__module__).startswith('psycopg'):
            cur.execute('''INSERT INTO learning_trade_meta
                (external_id,mode,symbol,side,strategy,score,risk_pct,leverage,atr_pct,market_regime,entry_timing,news_impact,planned_risk,stop_loss,take_profit,entry_price,captured_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (external_id) DO UPDATE SET score=EXCLUDED.score,risk_pct=EXCLUDED.risk_pct,leverage=EXCLUDED.leverage,
                atr_pct=EXCLUDED.atr_pct,market_regime=EXCLUDED.market_regime,entry_timing=EXCLUDED.entry_timing,
                news_impact=EXCLUDED.news_impact,planned_risk=EXCLUDED.planned_risk,stop_loss=EXCLUDED.stop_loss,take_profit=EXCLUDED.take_profit,entry_price=EXCLUDED.entry_price''', fields)
        else:
            cur.execute('''INSERT INTO learning_trade_meta
                (external_id,mode,symbol,side,strategy,score,risk_pct,leverage,atr_pct,market_regime,entry_timing,news_impact,planned_risk,stop_loss,take_profit,entry_price,captured_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(external_id) DO UPDATE SET score=excluded.score,risk_pct=excluded.risk_pct,leverage=excluded.leverage,
                atr_pct=excluded.atr_pct,market_regime=excluded.market_regime,entry_timing=excluded.entry_timing,
                news_impact=excluded.news_impact,planned_risk=excluded.planned_risk,stop_loss=excluded.stop_loss,take_profit=excluded.take_profit,entry_price=excluded.entry_price''', fields)
    return True


def _closed_rows(limit=1000):
    rows = recent(limit)
    out=[]
    for r in rows:
        pnl=float(r.get('pnl') or 0); fee=float(r.get('fee') or 0)
        net=pnl-fee
        out.append({**r, 'net_pnl': net})
    return out


def _stats(rows):
    n=len(rows)
    if not n: return {'trades':0}
    wins=[r for r in rows if r['net_pnl']>0]
    losses=[r for r in rows if r['net_pnl']<0]
    total=sum(r['net_pnl'] for r in rows)
    gross_win=sum(r['net_pnl'] for r in wins)
    gross_loss=abs(sum(r['net_pnl'] for r in losses))
    return {
        'trades':n,
        'wins':len(wins),
        'losses':len(losses),
        'win_rate_pct':round(len(wins)/n*100,2),
        'net_pnl':round(total,6),
        'avg_net_pnl':round(total/n,6),
        'avg_win':round(gross_win/len(wins),6) if wins else 0,
        'avg_loss':round(gross_loss/len(losses),6) if losses else 0,
        'profit_factor':round(gross_win/gross_loss,4) if gross_loss else None,
    }


def _group(rows, key):
    groups={}
    for r in rows:
        value=r.get(key) or 'UNKNOWN'
        groups.setdefault(value,[]).append(r)
    return {str(k):_stats(v) for k,v in groups.items()}


def _load_meta_indexes():
    """Load entry context plus secondary indexes used to reconcile Bybit ids (execId/orderId/orderLinkId)."""
    with _lock, _conn() as c:
        cur = c.cursor()
        cur.execute('SELECT external_id,mode,symbol,side,strategy,score,risk_pct,planned_risk,market_regime,entry_timing,news_impact,entry_price,captured_at FROM learning_trade_meta')
        cols = ['external_id','mode','symbol','side','strategy','score','risk_pct','planned_risk','market_regime','entry_timing','news_impact','entry_price','captured_at']
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    meta = {str(r['external_id']): r for r in rows}
    by_symbol_side = {}
    for m in rows:
        by_symbol_side.setdefault((str(m.get('symbol') or '').upper(), str(m.get('side') or '').upper()), []).append(m)
    return meta, by_symbol_side


def _match_meta(r, meta, by_symbol_side, aliases):
    key = str(r.get('external_id') or '')
    if key in meta:
        return meta[key], 'external_id'
    alias = aliases.get(key)
    if alias and str(alias) in meta:
        return meta[str(alias)], 'alias'
    symbol = str(r.get('symbol') or '').upper()
    side = str(r.get('side') or '').upper()
    candidates = list(by_symbol_side.get((symbol, side), []))
    ep = float(r.get('entry_price') or 0)
    if ep:
        close = [m for m in candidates if abs(float(m.get('entry_price') or 0)-ep) <= max(abs(ep)*1e-6, 1e-12)]
        if close:
            candidates = close
    if candidates:
        # Prefer the closest entry price, then latest captured context.
        candidates.sort(key=lambda m: (abs(float(m.get('entry_price') or 0)-ep) if ep else 0, -float(m.get('captured_at') or 0)))
        return candidates[0], 'symbol_side_entry_price' if ep else 'symbol_side'
    return None, 'none'


def _learning_lesson_count():
    init_learning_db()
    with _lock, _conn() as c:
        cur=c.cursor()
        cur.execute('SELECT COUNT(*) FROM learning_lessons')
        row=cur.fetchone()
        return int((row[0] if row else 0) or 0)


def learning_counts():
    """Return durable journal/lesson counts without touching Bybit."""
    init_learning_db()
    with _lock, _conn() as c:
        cur=c.cursor()
        cur.execute('SELECT COUNT(*) FROM trade_journal')
        closed=int((cur.fetchone() or [0])[0] or 0)
        cur.execute('SELECT COUNT(*) FROM learning_lessons')
        lessons=int((cur.fetchone() or [0])[0] or 0)
    return {'closed_rows': closed, 'lesson_total': lessons, 'pending': max(0, closed-lessons)}


def record_lessons_from_closed(limit=1000):
    """Idempotently backfill every closed journal row into learning_lessons.

    This is a background operation, never a page-load dependency. Each row is
    isolated with a savepoint so one malformed historical row cannot abort the
    remaining 43/44 lessons. Missing entry context is still a valid lesson.
    """
    init_learning_db()
    rows = _closed_rows(limit)
    meta, by_symbol_side = _load_meta_indexes()
    aliases = aliases_for_orders([r.get('external_id') for r in rows])
    lessons=[]
    matched=0
    unmatched=0
    match_methods={}
    errors=[]
    with _lock, _conn() as c:
        cur=c.cursor()
        for idx, r in enumerate(rows):
            key=str(r.get('external_id') or '')
            try:
                m, match_method = _match_meta(r, meta, by_symbol_side, aliases)
                if m: matched += 1
                else: unmatched += 1
                match_methods[match_method] = match_methods.get(match_method, 0) + 1
                net=float(r.get('net_pnl') or 0)
                planned=float(m.get('planned_risk') or 0) if m else 0
                outcome='WIN' if net>0 else 'LOSS' if net<0 else 'BREAKEVEN'
                r_mult=(net/planned) if planned>0 else None
                score=float(m.get('score') or 0) if m else 0
                right=[]; wrong=[]
                if net>0: right.append('Сделка закрылась в плюс после учёта комиссии.')
                elif net<0: wrong.append('Сделка закрылась в минус после учёта комиссии.')
                if m:
                    if score>=80 and net<=0: wrong.append(f'Высокий входной score {score:g} не подтвердился результатом.')
                    if score<70 and net>0: right.append(f'Движение подтвердилось несмотря на score {score:g}.')
                symbol=str((m or {}).get('symbol') or r.get('symbol') or '?')
                side=str((m or {}).get('side') or r.get('side') or '?')
                strategy=str((m or {}).get('strategy') or 'UNKNOWN')
                if m:
                    lesson=f"{outcome}: {symbol} {side}, {strategy}, net P&L {net:+.4f} USDT"
                else:
                    wrong.append('Контекст входа не найден; результат Bybit всё равно сохранён для обучения.')
                    lesson=f"{outcome}: {symbol} {side}, контекст входа не найден, net P&L {net:+.4f} USDT"
                if r_mult is not None: lesson+=f", результат {r_mult:+.2f}R"
                lesson+=f". Контекст: regime={(m.get('market_regime') if m else None) or 'UNKNOWN'}, timing={(m.get('entry_timing') if m else None) or 'UNKNOWN'}, news={(m.get('news_impact') if m else None) or 'UNKNOWN'}."
                row=(key,str((m or {}).get('mode') or r.get('mode') or ''),symbol,side,str((m or {}).get('strategy') or 'UNKNOWN'),outcome,net,r_mult,score,planned,lesson,' '.join(right),' '.join(wrong),time.time())
                savepoint=f'lesson_row_{idx}'
                cur.execute(f'SAVEPOINT {savepoint}')
                try:
                    if str(c.__class__.__module__).startswith('psycopg'):
                        cur.execute("""INSERT INTO learning_lessons
                            (external_id,mode,symbol,side,strategy,outcome,net_pnl,r_multiple,score,planned_risk,lesson,what_went_right,what_went_wrong,created_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (external_id) DO UPDATE SET mode=EXCLUDED.mode,symbol=EXCLUDED.symbol,side=EXCLUDED.side,strategy=EXCLUDED.strategy,outcome=EXCLUDED.outcome,net_pnl=EXCLUDED.net_pnl,r_multiple=EXCLUDED.r_multiple,score=EXCLUDED.score,planned_risk=EXCLUDED.planned_risk,lesson=EXCLUDED.lesson,what_went_right=EXCLUDED.what_went_right,what_went_wrong=EXCLUDED.what_went_wrong""", row)
                    else:
                        cur.execute("""INSERT INTO learning_lessons
                            (external_id,mode,symbol,side,strategy,outcome,net_pnl,r_multiple,score,planned_risk,lesson,what_went_right,what_went_wrong,created_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(external_id) DO UPDATE SET mode=excluded.mode,symbol=excluded.symbol,side=excluded.side,strategy=excluded.strategy,outcome=excluded.outcome,net_pnl=excluded.net_pnl,r_multiple=excluded.r_multiple,score=excluded.score,planned_risk=excluded.planned_risk,lesson=excluded.lesson,what_went_right=excluded.what_went_right,what_went_wrong=excluded.what_went_wrong""", row)
                    cur.execute(f'RELEASE SAVEPOINT {savepoint}')
                except Exception:
                    cur.execute(f'ROLLBACK TO SAVEPOINT {savepoint}')
                    cur.execute(f'RELEASE SAVEPOINT {savepoint}')
                    raise
                lessons.append({'external_id':key,'outcome':outcome,'net_pnl':net,'r_multiple':r_mult,'lesson':lesson,'what_went_right':' '.join(right),'what_went_wrong':' '.join(wrong),'context_found':bool(m),'match_method':match_method})
            except Exception as e:
                if len(errors) < 20:
                    errors.append(f'{key or "<empty>"}: {str(e)[:240]}')
    if lessons:
        _adaptive_cache['at']=0.0
    total=_learning_lesson_count()
    return {'created_or_updated':len(lessons),'lesson_total':total,'closed_rows':len(rows),'matched_context':matched,'unmatched_context':unmatched,'match_methods':match_methods,'errors':errors,'failed_rows':len(errors),'pending':max(0,len(rows)-len(lessons)),'lessons':lessons}


def recent_lessons(limit=50):
    init_learning_db()
    with _lock, _conn() as c:
        cur=c.cursor()
        cols=['external_id','mode','symbol','side','strategy','outcome','net_pnl','r_multiple','score','planned_risk','lesson','what_went_right','what_went_wrong','created_at']
        if str(c.__class__.__module__).startswith('psycopg'):
            cur.execute('SELECT external_id,mode,symbol,side,strategy,outcome,net_pnl,r_multiple,score,planned_risk,lesson,what_went_right,what_went_wrong,created_at FROM learning_lessons ORDER BY created_at DESC LIMIT %s',(int(limit),))
        else:
            cur.execute('SELECT external_id,mode,symbol,side,strategy,outcome,net_pnl,r_multiple,score,planned_risk,lesson,what_went_right,what_went_wrong,created_at FROM learning_lessons ORDER BY created_at DESC LIMIT ?',(int(limit),))
        return [dict(zip(cols,r)) for r in cur.fetchall()]


_adaptive_cache = {"at": 0.0, "rows": []}


def adaptive_adjustment(strategy=None, side=None, score=None):
    """Return a small evidence-based score adjustment, capped at +/-5.

    Learning is deliberately conservative: at least 8 closed lessons are needed,
    and the adjustment is shrunk toward zero. This makes every closed trade useful
    without allowing a short winning/losing streak to hijack the entry engine.
    """
    now=time.time()
    if now - float(_adaptive_cache.get("at", 0)) > 60:
        init_learning_db()
        with _lock, _conn() as c:
            cur=c.cursor()
            cur.execute('SELECT strategy,side,score,outcome,net_pnl,r_multiple FROM learning_lessons ORDER BY created_at DESC LIMIT 1000')
            _adaptive_cache["rows"]=[dict(zip(['strategy','side','score','outcome','net_pnl','r_multiple'],r)) for r in cur.fetchall()]
            _adaptive_cache["at"]=now
    rows=_adaptive_cache.get("rows",[])
    filt=[]
    for r in rows:
        if strategy and str(r.get('strategy') or '').upper() != str(strategy).upper(): continue
        if side and str(r.get('side') or '').upper() != str(side).upper(): continue
        if score is not None:
            try:
                if abs(float(r.get('score') or 0)-float(score)) > 12: continue
            except Exception: continue
        filt.append(r)
    if len(filt) < 8:
        return {'adjustment':0.0,'samples':len(filt),'confidence':0.0,'basis':'insufficient_samples'}
    wins=sum(1 for r in filt if str(r.get('outcome'))=='WIN')
    wr=wins/len(filt)
    avg_r=sum(float(r.get('r_multiple') or 0) for r in filt)/len(filt)
    # Win-rate edge is the primary signal; R is a secondary stabilizer.
    raw=((wr-0.5)*12.0) + max(-2.0,min(2.0,avg_r*1.5))
    shrink=min(1.0, len(filt)/40.0)
    adj=max(-5.0,min(5.0,raw*shrink))
    return {'adjustment':round(adj,3),'samples':len(filt),'confidence':round(shrink,3),'win_rate_pct':round(wr*100,2),'avg_r':round(avg_r,3),'basis':'closed_trade_evidence'}


def build_learning_report(limit=1000):
    init_learning_db()
    rows=_closed_rows(limit)
    lesson_sync=learning_counts()
    report={
        'learning_enabled': True,
        'auto_apply': False,
        'trades_analyzed': len(rows),
        'closed_trades': len(rows),
        'overall': _stats(rows),
        'by_mode': _group(rows,'mode'),
        'by_symbol': _group(rows,'symbol'),
        'by_side': _group(rows,'side'),
        'minimum_samples_for_insight': 20,
        'insights': [],
        'recent_lessons': recent_lessons(50),
        'lesson_sync': {k: lesson_sync.get(k, 0) for k in ('closed_rows','lesson_total','pending')},
        'generated_at': int(time.time()*1000),
    }
    # Contextual grouping is only possible when entry metadata exists.
    with _lock, _conn() as c:
        cur=c.cursor()
        cur.execute('SELECT external_id,mode,symbol,side,strategy,score,risk_pct,leverage,atr_pct,market_regime,entry_timing,news_impact,planned_risk,stop_loss,take_profit,entry_price FROM learning_trade_meta')
        meta={str(r[0]):dict(zip(['external_id','mode','symbol','side','strategy','score','risk_pct','leverage','atr_pct','market_regime','entry_timing','news_impact','planned_risk','stop_loss','take_profit','entry_price'],r)) for r in cur.fetchall()}
    enriched=[]
    order_ids=[r.get('external_id') for r in rows]
    aliases=aliases_for_orders(order_ids)
    _, by_symbol_side = _load_meta_indexes()
    matched_context=0
    for r in rows:
        m, method = _match_meta(r, meta, by_symbol_side, aliases)
        if m:
            matched_context += 1
            enriched.append({**r, **m, 'matched_via': method})
    report['contextual_trades']=matched_context
    report['trades_without_entry_context']=max(0, len(rows)-matched_context)
    for key in ('strategy','side','market_regime','entry_timing','news_impact'):
        groups={}
        for r in enriched: groups.setdefault(r.get(key) or 'UNKNOWN',[]).append(r)
        for label, vals in groups.items():
            s=_stats(vals)
            if s['trades']>=20 and s.get('avg_net_pnl',0)<0:
                report['insights'].append({
                    'type':'review', 'dimension':key, 'value':label,
                    'message':f'{key}={label} has negative average net P&L over {s["trades"]} trades.',
                    'stats':s,
                    'action':'REVIEW_ONLY'
                })
    if len(rows)<20:
        report['learning_status']='COLLECTING_DATA'
        report['note']='Not enough closed trades for strategy changes. No automatic changes are applied.'
    else:
        report['learning_status']='ANALYZABLE'
        report['note']='Insights are statistical suggestions only. Strategy parameters are not changed automatically.'
    return report


def save_thesis(position_key, thesis, initial=False):
    init_learning_db(); import json, time
    key=str(position_key)
    fields=(key,str(thesis.get('symbol') or ''),str(thesis.get('side') or ''),float(thesis.get('entry_price') or 0),float(thesis.get('thesis_health') or 0),json.dumps(thesis.get('positive_factors') or [],ensure_ascii=False),float(thesis.get('thesis_health') or 0),str(thesis.get('thesis_decision') or ''),json.dumps(thesis.get('positive_factors') or [],ensure_ascii=False),json.dumps(thesis.get('negative_factors') or [],ensure_ascii=False),time.time())
    with _lock, _conn() as c:
        cur=c.cursor()
        if str(c.__class__.__module__).startswith('psycopg'):
            cur.execute("""INSERT INTO trade_thesis(position_key,symbol,side,entry_price,initial_score,initial_reasons,latest_score,latest_decision,latest_positive,latest_negative,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(position_key) DO UPDATE SET latest_score=EXCLUDED.latest_score,latest_decision=EXCLUDED.latest_decision,latest_positive=EXCLUDED.latest_positive,latest_negative=EXCLUDED.latest_negative,updated_at=EXCLUDED.updated_at""",fields)
        else:
            cur.execute("""INSERT INTO trade_thesis(position_key,symbol,side,entry_price,initial_score,initial_reasons,latest_score,latest_decision,latest_positive,latest_negative,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(position_key) DO UPDATE SET latest_score=excluded.latest_score,latest_decision=excluded.latest_decision,latest_positive=excluded.latest_positive,latest_negative=excluded.latest_negative,updated_at=excluded.updated_at""",fields)
    return True
