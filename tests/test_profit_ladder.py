import engine


def test_profit_ladder_crosses_fixed_stages():
    assert engine._profit_ladder_crossed(9.9) == []
    assert engine._profit_ladder_crossed(10) == [10.0]
    assert engine._profit_ladder_crossed(25) == [10.0,20.0]
    assert engine._profit_ladder_crossed(55) == [10.0,20.0,30.0,40.0,50.0]


def test_profit_ladder_lock_price_long_short():
    long_p = engine._profit_lock_price(100.0, 100.0, 1000.0, 'Buy', 10.0)
    short_p = engine._profit_lock_price(100.0, 100.0, 1000.0, 'Sell', 10.0)
    assert long_p == 101.0
    assert short_p == 99.0


def test_dynamic_profit_protection_tracks_peak_after_50_percent():
    assert engine._profit_peak_lock_pct(49) is None
    assert engine._profit_peak_lock_pct(50) == 40.0
    assert engine._profit_peak_lock_pct(70) == 55.0
    assert engine._profit_peak_lock_pct(100) == 85.0
