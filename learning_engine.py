"""Explainable learning layer for the trading agent.

This module learns from CLOSED trades only. It never changes strategy parameters
or opens/closes positions by itself. It produces statistical insights and
optional, human-reviewable recommendations.
"""
import math
import time
from journal import _conn, _lock, init_db, recent


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
            captured_at REAL
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS learning_insights (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        )''')


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
        str(meta.get('news_impact') or ''), float(meta.get('planned_risk') or 0), time.time()
    )
    with _lock, _conn() as c:
        cur = c.cursor()
        if str(c.__class__.__module__).startswith('psycopg'):
            cur.execute('''INSERT INTO learning_trade_meta
                (external_id,mode,symbol,side,strategy,score,risk_pct,leverage,atr_pct,market_regime,entry_timing,news_impact,planned_risk,captured_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (external_id) DO UPDATE SET score=EXCLUDED.score,risk_pct=EXCLUDED.risk_pct,leverage=EXCLUDED.leverage,
                atr_pct=EXCLUDED.atr_pct,market_regime=EXCLUDED.market_regime,entry_timing=EXCLUDED.entry_timing,
                news_impact=EXCLUDED.news_impact,planned_risk=EXCLUDED.planned_risk''', fields)
        else:
            cur.execute('''INSERT INTO learning_trade_meta
                (external_id,mode,symbol,side,strategy,score,risk_pct,leverage,atr_pct,market_regime,entry_timing,news_impact,planned_risk,captured_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(external_id) DO UPDATE SET score=excluded.score,risk_pct=excluded.risk_pct,leverage=excluded.leverage,
                atr_pct=excluded.atr_pct,market_regime=excluded.market_regime,entry_timing=excluded.entry_timing,
                news_impact=excluded.news_impact,planned_risk=excluded.planned_risk''', fields)
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


def build_learning_report(limit=1000):
    init_learning_db()
    rows=_closed_rows(limit)
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
        'generated_at': int(time.time()*1000),
    }
    # Contextual grouping is only possible when entry metadata exists.
    with _lock, _conn() as c:
        cur=c.cursor()
        cur.execute('SELECT external_id,mode,symbol,side,strategy,score,risk_pct,leverage,atr_pct,market_regime,entry_timing,news_impact,planned_risk FROM learning_trade_meta')
        meta={str(r[0]):dict(zip(['external_id','mode','symbol','side','strategy','score','risk_pct','leverage','atr_pct','market_regime','entry_timing','news_impact','planned_risk'],r)) for r in cur.fetchall()}
    enriched=[]
    for r in rows:
        m=meta.get(str(r['external_id']))
        if m: enriched.append({**r, **m})
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
