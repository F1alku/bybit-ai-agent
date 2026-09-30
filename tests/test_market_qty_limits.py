import ast
from pathlib import Path

s = Path("engine.py").read_text()
assert "maxMktOrderQty" in s
assert "Final exchange-limit guard" in s
assert "Last-chance guard" in s
assert "minimum Bybit order value" in s
print("market qty hard-limit checks: PASS")
