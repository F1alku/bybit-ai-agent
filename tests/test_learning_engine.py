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
