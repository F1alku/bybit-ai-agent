from engine import _symbol_rules

def test_market_qty_prefers_max_mkt_order_qty(monkeypatch):
    import engine
    monkeypatch.setattr(engine, "instruments", lambda: [{"symbol":"ZBCNUSDT","lotSizeFilter":{"qtyStep":"1","minOrderQty":"1","maxOrderQty":"1450000000000000","maxMktOrderQty":"290000000000000"}}])
    step, minimum, maximum = _symbol_rules("ZBCNUSDT")
    assert step == 1.0
    assert minimum == 1.0
    assert maximum == 290000000000000.0
