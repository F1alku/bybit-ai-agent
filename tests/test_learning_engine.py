import learning_engine as le

def test_stats_and_no_auto_apply():
    rows=[{'net_pnl':1.0},{'net_pnl':-0.5},{'net_pnl':0.5}]
    s=le._stats(rows)
    assert s['trades']==3
    assert s['wins']==2
    assert s['losses']==1
    assert s['net_pnl']==1.0
