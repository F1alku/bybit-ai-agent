from journal import recent

# Static regression test: journal.py must not call dict(tuple_row) for psycopg rows.
s=open("journal.py", encoding="utf-8").read()
assert "return [dict(r) for r in cur.fetchall()]" not in s
assert "dict(zip(cols, r))" in s
