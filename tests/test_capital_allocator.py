from capital_allocator import allocate, validate_allocations

def test_single_strong_signal_can_use_more_than_30_but_never_all_capital():
    rows=allocate([{"symbol":"BTCUSDT","score":98}],100)
    assert len(rows)==1
    assert 70 < rows[0]["allocation"]["capital_pct"] <= 80
    assert rows[0]["allocation"]["risk_pct_of_bot"] == 10.0
    assert validate_allocations(rows)["risk_ok"]

def test_two_signals_share_risk_and_capital_by_score():
    rows=allocate([{"symbol":"AUSDT","score":95},{"symbol":"BUSDT","score":75}],100)
    assert len(rows)==2
    assert rows[0]["allocation"]["capital_pct"] > rows[1]["allocation"]["capital_pct"]
    assert abs(sum(x["allocation"]["risk_pct_of_bot"] for x in rows)-10.0) < 1e-9
    assert abs(sum(x["allocation"]["capital_pct"] for x in rows)-90.0) < 1e-9

def test_three_entries_are_capped_and_leave_reserve():
    rows=allocate([{"symbol":"AUSDT","score":99},{"symbol":"BUSDT","score":88},{"symbol":"CUSDT","score":70}],100)
    assert len(rows)==3
    check=validate_allocations(rows)
    assert check["risk_ok"] and check["concentration_ok"] and check["reserve_pct"] == 10.0
