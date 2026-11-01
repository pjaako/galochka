import pytest

from galochka import cli, db, repo


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("GALOCHKA_DATA", str(tmp_path))
    return tmp_path


def test_migrate_prints_ok_and_creates_db(_env, capsys):
    cli.main(["migrate"])
    assert capsys.readouterr().out.strip() == "ok"
    conn = db.connect(_env / "galochka.db")
    try:
        assert repo.get_setting(conn, "session_minutes") == "30"
    finally:
        conn.close()


def test_migrate_twice_is_idempotent(capsys):
    cli.main(["migrate"])
    cli.main(["migrate"])
    assert capsys.readouterr().out.split() == ["ok", "ok"]


def test_migrate_creates_missing_data_dir(tmp_path, monkeypatch, capsys):
    target = tmp_path / "nested" / "data"
    monkeypatch.setenv("GALOCHKA_DATA", str(target))
    cli.main(["migrate"])
    assert (target / "galochka.db").exists()


def test_serve_passes_host_and_port_to_uvicorn(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    cli.main(["serve", "--host", "127.0.0.1", "--port", "8099"])
    assert len(calls) == 1
    app, kwargs = calls[0]
    assert kwargs == {"host": "127.0.0.1", "port": 8099}
    assert app.state.settings.db_path.exists()
    app.state.conn.close()


def test_serve_defaults_to_env_port(monkeypatch):
    monkeypatch.setenv("GALOCHKA_PORT", "9123")
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    cli.main(["serve"])
    assert calls[0][1] == {"host": "0.0.0.0", "port": 9123}
    calls[0][0].state.conn.close()


def test_command_is_required():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


def test_settings_from_env(monkeypatch, tmp_path):
    from galochka.config import Settings

    monkeypatch.delenv("GALOCHKA_PORT", raising=False)
    monkeypatch.delenv("GALOCHKA_EXPERT_PASSWORD", raising=False)
    monkeypatch.delenv("GALOCHKA_CASSANDRA_URL", raising=False)
    s = Settings.from_env()
    assert (s.port, s.expert_password, s.cassandra_url) == (8081, "experte", "")
    assert s.db_path == tmp_path / "galochka.db"
    assert s.photos_dir == tmp_path / "photos"
    monkeypatch.setenv("GALOCHKA_EXPERT_PASSWORD", "geheim")
    monkeypatch.setenv("GALOCHKA_CASSANDRA_URL", "http://cassandra.local:8080")
    s = Settings.from_env()
    assert (s.expert_password, s.cassandra_url) == ("geheim", "http://cassandra.local:8080")
