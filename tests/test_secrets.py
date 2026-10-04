"""Secret protection, built-in exclusions, and doctor secret scan tests."""

from kaggle_runner import doctor, ignore_rules, sync, urlstore


def test_secrets_never_uploaded_even_with_negation(tmp_path, monkeypatch, fake_jupyter_server):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path.parent / f"{tmp_path.name}-runner"))
    monkeypatch.delenv("KAGGLE_SYNC_ALLOW_SECRETS", raising=False)
    fake_jupyter_server.clear()

    # Create .kagglesyncignore with negation !.env and !kaggle.json
    (tmp_path / ".kagglesyncignore").write_text(
        "!.env\n!kaggle.json\n!*.pem\n.kagglesyncignore\n",
        encoding="utf-8",
    )

    # Create secret files
    (tmp_path / ".env").write_text("SECRET_KEY=12345\n", encoding="utf-8")
    (tmp_path / "kaggle.json").write_text('{"key": "secret"}', encoding="utf-8")
    (tmp_path / "cert.pem").write_text("PEM_DATA", encoding="utf-8")
    (tmp_path / "normal.py").write_text("print('hello')", encoding="utf-8")

    # Verify is_ignored returns True for secrets despite negation
    assert ignore_rules.is_ignored(tmp_path, ".env") is True
    assert ignore_rules.is_ignored(tmp_path, "kaggle.json") is True
    assert ignore_rules.is_ignored(tmp_path, "cert.pem") is True
    assert ignore_rules.is_ignored(tmp_path, "normal.py") is False

    client = sync.JupyterClient(fake_jupyter_server.url)
    synced = sync.sync_once(client, tmp_path)

    # Only normal.py should be synced
    assert synced == 1
    storage = fake_jupyter_server.handler_cls.storage
    assert "contents/local-project/normal.py" in storage
    assert "contents/local-project/.env" not in storage
    assert "contents/local-project/kaggle.json" not in storage
    assert "contents/local-project/cert.pem" not in storage


def test_doctor_scan_reports_hit_without_printing_secret(tmp_path, capsys):
    secret_token = "very-secret-token-xyz-987"
    proxy_url = f"https://kkb-production.jupyter-proxy.kaggle.net/k/sess-abc/{secret_token}/proxy"

    leak_file = tmp_path / "leak.py"
    leak_file.write_text(f"# My URL: {proxy_url}\n", encoding="utf-8")

    res = doctor.check_secrets_scan(tmp_path)
    assert res.status == "WARN"
    assert "leak.py:1" in res.message

    # Crucial: token must NEVER appear in message or hint
    assert secret_token not in res.message
    assert secret_token not in res.hint


def test_allow_secrets_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_SYNC_ALLOW_SECRETS", "1")
    assert ignore_rules.is_secret_path(".env") is False
    assert ignore_rules.is_secret_path("kaggle.json") is False


def test_runner_home_inside_project_is_not_synced(
    tmp_path,
    monkeypatch,
    fake_jupyter_server,
):
    runner_home = tmp_path / "private-runner-home"
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(runner_home))
    fake_jupyter_server.clear()
    urlstore.save_url(tmp_path, fake_jupyter_server.url)
    (tmp_path / "script.py").write_text("print('safe')", encoding="utf-8")

    client = sync.JupyterClient(fake_jupyter_server.url)
    sync.sync_once(client, tmp_path)

    assert not any(
        "private-runner-home" in key
        for key in fake_jupyter_server.handler_cls.storage
    )
    assert doctor.check_runner_home_location(tmp_path).status == "WARN"
