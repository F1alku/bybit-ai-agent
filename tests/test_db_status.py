def test_db_status_sqlite_is_not_durable(monkeypatch, tmp_path):
    import journal
    monkeypatch.setattr(journal, 'DATABASE_URL', '')
    monkeypatch.setattr(journal, 'SQLITE_PATH', str(tmp_path / 'db.sqlite'))
    s = journal.db_status()
    assert s['ok'] is True
    assert s['backend'] == 'sqlite'
    assert s['durable'] is False

