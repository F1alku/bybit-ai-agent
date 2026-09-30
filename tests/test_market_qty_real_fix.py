from engine import _symbol_rules

def test_market_qty_prefers_max_mkt_order_qty(monkeypatch):
    import engine
    monkeypatch.setattr(engine, "instruments", lambda: [{"symbol":"ZBCNUSDT","lotSizeFilter":{"qtyStep":"1","minOrderQty":"1","maxOrderQty":"1450000000000000","maxMktOrderQty":"290000000000000"}}])
    step, minimum, maximum = _symbol_rules("ZBCNUSDT")
    assert step == 1.0
    assert minimum == 1.0
    assert maximum == 290000000000000.0

def test_format_qty_removes_float_artifact_and_respects_step(monkeypatch):
    import engine
    monkeypatch.setattr(engine, "instruments", lambda: [{"symbol":"MOVRUSDT","lotSizeFilter":{"qtyStep":"0.1","minOrderQty":"0.1","maxMktOrderQty":"100"}}])
    assert engine._format_qty("MOVRUSDT", 1.2000000000000002) == "1.2"
    assert engine._format_qty("MOVRUSDT", 1.29) == "1.2"


def test_format_qty_prefers_market_max(monkeypatch):
    import engine
    monkeypatch.setattr(engine, "instruments", lambda: [{"symbol":"MOVRUSDT","lotSizeFilter":{"qtyStep":"0.1","minOrderQty":"0.1","maxOrderQty":"1000","maxMktOrderQty":"12.3"}}])
    assert engine._format_qty("MOVRUSDT", 99.99) == "12.3"

def test_agreement_error_is_detected_and_symbols_are_temporarily_blocked():
    import trader
    trader._UNAVAILABLE_SYMBOLS.clear()
    assert trader._is_agreement_required_error('Bybit 110126: You must sign the required agreement before trading this contract.')
    trader._mark_symbol_unavailable('CBRSUSDT', 'Bybit 110126')
    assert 'CBRSUSDT' in trader._unavailable_symbols()
    trader._UNAVAILABLE_SYMBOLS.clear()
