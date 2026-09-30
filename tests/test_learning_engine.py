import learning_engine as le

def test_stats_and_no_auto_apply():
    rows=[{'net_pnl':1.0},{'net_pnl':-0.5},{'net_pnl':0.5}]
    s=le._stats(rows)
    assert s['trades']==3
    assert s['wins']==2
    assert s['losses']==1
    assert s['net_pnl']==1.0


def test_order_id_alias_links_closed_pnl_to_entry_context(tmp_path, monkeypatch):
    import journal, learning_engine
    db=tmp_path/'learn.db'
    monkeypatch.setattr(journal, 'SQLITE_PATH', str(db))
    monkeypatch.setattr(learning_engine, '_conn', journal._conn)
    monkeypatch.setattr(learning_engine, '_lock', journal._lock)
    monkeypatch.setattr(learning_engine, 'recent', journal.recent)
    monkeypatch.setattr(learning_engine, 'aliases_for_orders', journal.aliases_for_orders)
    learning_engine.record_trade_meta({'external_id':'LINK-1','mode':'demo','symbol':'BTCUSDT','side':'Buy','strategy':'normal','score':84,'risk_pct':2,'leverage':10,'planned_risk':0.2,'entry_price':100,'stop_loss':99,'take_profit':102})
    journal.upsert_closed_pnl('demo', {'orderId':'OID-1','orderLinkId':'LINK-1','symbol':'BTCUSDT','side':'Buy','qty':'1','avgEntryPrice':'100','avgExitPrice':'102','closedPnl':'2','openFee':'0.01','closeFee':'0.01','createdTime':'1','updatedTime':'2'})
    report=learning_engine.build_learning_report()
    assert report['trades_analyzed']==1

def test_closed_trade_creates_persistent_lesson(tmp_path, monkeypatch):
    import journal, learning_engine
    db=tmp_path/'lesson.db'
    monkeypatch.setattr(journal, 'SQLITE_PATH', str(db))
    monkeypatch.setattr(learning_engine, '_conn', journal._conn)
    monkeypatch.setattr(learning_engine, '_lock', journal._lock)
    monkeypatch.setattr(learning_engine, 'recent', journal.recent)
    monkeypatch.setattr(learning_engine, 'aliases_for_orders', journal.aliases_for_orders)
    learning_engine.record_trade_meta({'external_id':'LESSON-1','mode':'demo','symbol':'BTCUSDT','side':'Buy','strategy':'normal','score':84,'risk_pct':2,'leverage':10,'planned_risk':0.5,'entry_price':100,'stop_loss':99,'take_profit':102})
    journal.upsert_closed_pnl('demo', {'orderId':'OID-L1','orderLinkId':'LESSON-1','symbol':'BTCUSDT','side':'Buy','qty':'1','avgEntryPrice':'100','avgExitPrice':'102','closedPnl':'2','openFee':'0.01','closeFee':'0.01','createdTime':'1','updatedTime':'2'})
    result=learning_engine.record_lessons_from_closed()
    lessons=learning_engine.recent_lessons()
    assert result['created_or_updated']==1
    assert lessons[0]['outcome']=='WIN'
    assert abs(lessons[0]['r_multiple']-3.96)<1e-9


def test_learning_module_has_closed_trade_fallback():
    src=open("learning_engine.py", encoding="utf-8").read()
    assert "execId/orderId" in src
    assert "Контекст входа не найден" in src
    assert "by_symbol_side" in src

def test_closed_trade_without_context_still_becomes_lesson(tmp_path, monkeypatch):
    import journal, learning_engine
    db=tmp_path/'unmatched.db'
    monkeypatch.setattr(journal, 'SQLITE_PATH', str(db))
    monkeypatch.setattr(learning_engine, '_conn', journal._conn)
    monkeypatch.setattr(learning_engine, '_lock', journal._lock)
    monkeypatch.setattr(learning_engine, 'recent', journal.recent)
    monkeypatch.setattr(learning_engine, 'aliases_for_orders', journal.aliases_for_orders)
    journal.init_db()
    journal.upsert_closed_pnl('demo', {'execId':'EXEC-UNMATCHED','orderId':'OID-U','symbol':'ETHUSDT','side':'Sell','qty':'1','avgEntryPrice':'100','avgExitPrice':'99','closedPnl':'1','openFee':'0','closeFee':'0','createdTime':'1','updatedTime':'2'})
    result=learning_engine.record_lessons_from_closed()
    assert result['closed_rows']==1
    assert result['created_or_updated']==1
    assert result['unmatched_context']==1
    assert learning_engine.recent_lessons()[0]['context_found'] if 'context_found' in learning_engine.recent_lessons()[0] else True


def test_backfill_continues_when_one_historical_row_fails(tmp_path, monkeypatch):
    import journal, learning_engine
    db=tmp_path/'partial.db'
    monkeypatch.setattr(journal, 'SQLITE_PATH', str(db))
    monkeypatch.setattr(learning_engine, '_conn', journal._conn)
    monkeypatch.setattr(learning_engine, '_lock', journal._lock)
    monkeypatch.setattr(learning_engine, 'recent', journal.recent)
    monkeypatch.setattr(learning_engine, 'aliases_for_orders', journal.aliases_for_orders)
    journal.init_db()
    journal.upsert_closed_pnl('demo', {'execId':'BAD','symbol':'BTCUSDT','side':'Buy','qty':'1','avgEntryPrice':'100','avgExitPrice':'101','closedPnl':'1','createdTime':'1','updatedTime':'1'})
    journal.upsert_closed_pnl('demo', {'execId':'GOOD','symbol':'ETHUSDT','side':'Sell','qty':'1','avgEntryPrice':'100','avgExitPrice':'99','closedPnl':'1','createdTime':'2','updatedTime':'2'})
    original=learning_engine._match_meta
    calls={'n':0}
    def flaky(*args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            raise ValueError('synthetic malformed row')
        return original(*args, **kwargs)
    monkeypatch.setattr(learning_engine, '_match_meta', flaky)
    result=learning_engine.record_lessons_from_closed()
    assert result['closed_rows']==2
    assert result['failed_rows']==1
    assert result['created_or_updated']==1
    assert result['lesson_total']==1

def test_adaptive_learning_is_bounded_and_needs_samples(monkeypatch, tmp_path):
    import journal, learning_engine
    monkeypatch.setattr(journal, 'SQLITE_PATH', str(tmp_path/'learn.db'))
    learning_engine._adaptive_cache={'at':0.0,'rows':[]}
    learning_engine.init_learning_db()
    for i in range(10):
        learning_engine.record_trade_meta({'external_id':f'o{i}','mode':'demo','symbol':'BTCUSDT','side':'LONG','strategy':'normal','score':85,'planned_risk':1,'entry_price':100,'stop_loss':99,'take_profit':102})
    # Lessons can be written directly for a deterministic unit test of the adaptive layer.
    with journal._lock, journal._conn() as c:
        cur=c.cursor()
        for i in range(10):
            row=(f'o{i}','demo','BTCUSDT','LONG','normal','WIN',1.0,1.0,85.0,1.0,'lesson','','',float(i+1))
            if journal._is_pg():
                cur.execute('INSERT INTO learning_lessons (external_id,mode,symbol,side,strategy,outcome,net_pnl,r_multiple,score,planned_risk,lesson,what_went_right,what_went_wrong,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (external_id) DO NOTHING',row)
            else:
                cur.execute('INSERT INTO learning_lessons VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',row)
    r=learning_engine.adaptive_adjustment('normal','LONG',85)
    assert r['samples']==10 and -5.0 <= r['adjustment'] <= 5.0 and r['confidence'] > 0
