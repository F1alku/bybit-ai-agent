"""Explainable learning layer for the trading agent.

This module learns from CLOSED trades only. It never changes strategy parameters
or opens/closes positions by itself. It produces statistical insights and
optional, human-reviewable recommendations.
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


def record_lessons_from_closed(limit=1000):
    """Persist an explainable post-trade lesson for each closed trade."""
    init_learning_db()
    rows = _closed_rows(limit)
    with _lock, _conn() as c:
        cur = c.cursor()
        cur.execute('SELECT external_id,mode,symbol,side,strategy,score,risk_pct,planned_risk,market_regime,entry_timing,news_impact FROM learning_trade_meta')
        cols = ['external_id','mode','symbol','side','strategy','score','risk_pct','planned_risk','market_regime','entry_timing','news_impact']
        meta = {str(r[0]): dict(zip(cols, r)) for r in cur.fetchall()}
    aliases = aliases_for_orders([r.get('external_id') for r in rows])
    lessons=[]
    for r in rows:
        key=str(r['external_id']); m=meta.get(key)
        if not m and aliases.get(key): m=meta.get(str(aliases[key]))
        if not m: continue
        net=float(r.get('net_pnl') or 0); planned=float(m.get('planned_risk') or 0)
        outcome='WIN' if net>0 else 'LOSS' if net<0 else 'BREAKEVEN'
        r_mult=(net/planned) if planned>0 else None; score=float(m.get('score') or 0)
        right=[]; wrong=[]
        if net>0: right.append('Сделка закрылась в плюс после учёта комиссии.')
        elif net<0: wrong.append('Сделка закрылась в минус после учёта комиссии.')
        if score>=80 and net<=0: wrong.append(f'Высокий входной score {score:g} не подтвердился результатом.')
        if score<70 and net>0: right.append(f'Движение подтвердилось несмотря на score {score:g}.')
        lesson=f"{outcome}: {m.get('symbol','?')} {m.get('side','?')}, {m.get('strategy','?')}, net P&L {net:+.4f} USDT"
        if r_mult is not None: lesson+=f", результат {r_mult:+.2f}R"
        lesson+=f". Контекст: regime={m.get('market_regime') or 'UNKNOWN'}, timing={m.get('entry_timing') or 'UNKNOWN'}, news={m.get('news_impact') or 'UNKNOWN'}."
        row=(key,str(m.get('mode') or r.get('mode') or ''),str(m.get('symbol') or r.get('symbol') or ''),str(m.get('side') or r.get('side') or ''),str(m.get('strategy') or ''),outcome,net,r_mult,score,planned,lesson,' '.join(right),' '.join(wrong),time.time())
        with _lock, _conn() as c:
            cur=c.cursor()
            if str(c.__class__.__module__).startswith('psycopg'):
                cur.execute("""INSERT INTO learning_lessons
                    (external_id,mode,symbol,side,strategy,outcome,net_pnl,r_multiple,score,planned_risk,lesson,what_went_right,what_went_wrong,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (external_id) DO UPDATE SET outcome=EXCLUDED.outcome,net_pnl=EXCLUDED.net_pnl,r_multiple=EXCLUDED.r_multiple,lesson=EXCLUDED.lesson,what_went_right=EXCLUDED.what_went_right,what_went_wrong=EXCLUDED.what_went_wrong""", row)
            else:
                cur.execute("""INSERT INTO learning_lessons
                    (external_id,mode,symbol,side,strategy,outcome,net_pnl,r_multiple,score,planned_risk,lesson,what_went_right,what_went_wrong,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(external_id) DO UPDATE SET outcome=excluded.outcome,net_pnl=excluded.net_pnl,r_multiple=excluded.r_multiple,lesson=excluded.lesson,what_went_right=excluded.what_went_right,what_went_wrong=excluded.what_went_wrong""", row)
        lessons.append({'external_id':key,'outcome':outcome,'net_pnl':net,'r_multiple':r_mult,'lesson':lesson,'what_went_right':' '.join(right),'what_went_wrong':' '.join(wrong)})
    return {'created_or_updated':len(lessons),'lessons':lessons}


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


def build_learning_report(limit=1000):
    init_learning_db()
    rows=_closed_rows(limit)
    lesson_sync=record_lessons_from_closed(limit)
    report={
        'learning_enabled': True,
        'auto_apply': False,
        'trades_analyzed': len(rows),
        'overall': _stats(rows),
        'by_mode': _group(rows,'mode'),
        'by_symbol': _group(rows,'symbol'),
        'by_side': _group(rows,'side'),
        'minimum_samples_for_insight': 20,
        'insights': [],
        'recent_lessons': recent_lessons(50),
        'lesson_sync': {'created_or_updated': lesson_sync.get('created_or_updated', 0)},
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
    for r in rows:
        key=str(r['external_id'])
        m=meta.get(key)
        if not m:
            link=aliases.get(key)
            if link:
                m=meta.get(str(link))
        if m: enriched.append({**r, **m, 'matched_via_alias': key not in meta})
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
