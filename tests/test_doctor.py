"""Doctor diagnostic checks and cleanup tests."""

import json
from unittest.mock import patch

from kaggle_runner import doctor


def fake_execute_success(ws_base, base_url, token, kernel_id, code, on_text, user_expressions=None, timeout=None):
    exprs = {}
    if user_expressions:
        for k in user_expressions:
            if k == "root_probe":
                exprs["root_probe"] = {"data": {"text/plain": "True"}}
            elif k == "two":
                exprs["two"] = {"data": {"text/plain": "2"}}
            elif k == "info":
                payload = json.dumps({
                    "gpu": "Tesla T4 (15360 MiB)",
                    "free_gb": 18.2,
                    "python": "3.10.12",
                    "internet": True,
                })
                exprs["info"] = {"data": {"text/plain": payload}}
    return {"status": "ok", "user_expressions": exprs, "error_text": ""}


def test_doctor_healthy_fake_server(tmp_path, monkeypatch, fake_jupyter_server, capsys):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))
    fake_jupyter_server.clear()

    # Pre-create .kagglesyncignore to avoid WARN
    (tmp_path / ".kagglesyncignore").write_text("data/\n", encoding="utf-8")

    with patch("kaggle_runner.doctor.execute_in_kernel", side_effect=fake_execute_success):
        rc = doctor.run_doctor(
            project_root=tmp_path,
            url=fake_jupyter_server.url,
            deep=False,
            as_json=False,
            fix=False,
        )

    assert rc == 0
    captured = capsys.readouterr().out
    assert "[FAIL]" not in captured

    # Verify server tree is empty of temp doctor files
    storage = fake_jupyter_server.handler_cls.storage
    temp_files = [k for k in storage if "kaggle-doctor-" in k]
    assert temp_files == [], f"Doctor left temporary files behind: {temp_files}"


def test_doctor_accepts_managed_windows_launcher(tmp_path, monkeypatch):
    launcher = tmp_path / "kaggle-runner" / "cmd" / "kaggle-sync.cmd"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        "rem KAGGLE_LOCAL_RUNNER_LAUNCHER v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: str(launcher))

    result = doctor.check_old_launchers()

    assert result.status == "PASS"


def test_doctor_auth_failure(tmp_path, monkeypatch, fake_jupyter_server, capsys):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))
    fake_jupyter_server.clear()
    fake_jupyter_server.handler_cls.fail_auth = True

    rc = doctor.run_doctor(
        project_root=tmp_path,
        url=fake_jupyter_server.url,
        deep=False,
        as_json=False,
        fix=False,
    )

    assert rc == doctor.session_guard.EXIT_EXPIRED
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "expired" in out.lower()


def test_doctor_offline_exit_code(tmp_path, monkeypatch, fake_jupyter_server):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path.parent / f"{tmp_path.name}-runner"))
    fake_jupyter_server.clear()
    fake_jupyter_server.handler_cls.fail_500 = True
    (tmp_path / ".kagglesyncignore").write_text(".kagglesyncignore\n", encoding="utf-8")

    try:
        rc = doctor.run_doctor(
            project_root=tmp_path,
            url=fake_jupyter_server.url,
            deep=False,
            as_json=False,
            fix=False,
        )
        assert rc == doctor.session_guard.EXIT_OFFLINE
    finally:
        fake_jupyter_server.handler_cls.fail_500 = False


def test_doctor_warning_on_no_gpu_and_no_internet(tmp_path, monkeypatch, fake_jupyter_server, capsys):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))
    fake_jupyter_server.clear()

    def fake_no_gpu_no_net(ws_base, base_url, token, kernel_id, code, on_text, user_expressions=None, timeout=None):
        exprs = {}
        if user_expressions:
            for k in user_expressions:
                if k == "root_probe":
                    exprs["root_probe"] = {"data": {"text/plain": "True"}}
                elif k == "two":
                    exprs["two"] = {"data": {"text/plain": "2"}}
                elif k == "info":
                    payload = json.dumps({
                        "gpu": "",  # No GPU
                        "free_gb": 12.0,
                        "python": "3.10.12",
                        "internet": False,  # Internet off
                    })
                    exprs["info"] = {"data": {"text/plain": payload}}
        return {"status": "ok", "user_expressions": exprs, "error_text": ""}

    with patch("kaggle_runner.doctor.execute_in_kernel", side_effect=fake_no_gpu_no_net):
        doctor.run_doctor(
            project_root=tmp_path,
            url=fake_jupyter_server.url,
            deep=False,
            as_json=False,
            fix=False,
        )

    out = capsys.readouterr().out
    assert "enable gpu in kaggle session options" in out.lower()
    assert "Kaggle Internet is off: enable it in notebook settings" in out


def test_doctor_wrong_upload_root_fails(tmp_path, monkeypatch, fake_jupyter_server, capsys):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))
    fake_jupyter_server.clear()

    def fake_wrong_root(ws_base, base_url, token, kernel_id, code, on_text, user_expressions=None, timeout=None):
        exprs = {}
        if user_expressions:
            for k in user_expressions:
                if k == "root_probe":
                    exprs["root_probe"] = {"data": {"text/plain": "False"}}
                elif k == "two":
                    exprs["two"] = {"data": {"text/plain": "2"}}
                elif k == "info":
                    payload = json.dumps({
                        "gpu": "T4",
                        "free_gb": 10.0,
                        "python": "3.10",
                        "internet": True,
                    })
                    exprs["info"] = {"data": {"text/plain": payload}}
        return {"status": "ok", "user_expressions": exprs, "error_text": ""}

    with patch("kaggle_runner.doctor.execute_in_kernel", side_effect=fake_wrong_root):
        rc = doctor.run_doctor(
            project_root=tmp_path,
            url=fake_jupyter_server.url,
            deep=False,
            as_json=False,
            fix=False,
        )

    assert rc == 1
    out = capsys.readouterr().out
    assert "upload root is not /kaggle/working" in out.lower()


def test_doctor_cleanup_on_ctrl_c(tmp_path, monkeypatch, fake_jupyter_server):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))
    fake_jupyter_server.clear()

    def raise_interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    with patch("kaggle_runner.doctor.execute_in_kernel", side_effect=raise_interrupt):
        doctor.run_doctor(
            project_root=tmp_path,
            url=fake_jupyter_server.url,
            deep=False,
            as_json=False,
            fix=False,
        )

    # Server temp storage cleaned up even after interrupt
    storage = fake_jupyter_server.handler_cls.storage
    temp_files = [k for k in storage if "kaggle-doctor-" in k]
    assert temp_files == []


def test_doctor_json_output_valid_and_redacted(tmp_path, monkeypatch, fake_jupyter_server, capsys):
    monkeypatch.setenv("KAGGLE_RUNNER_HOME", str(tmp_path / ".kaggle-runner"))
    fake_jupyter_server.clear()

    with patch("kaggle_runner.doctor.execute_in_kernel", side_effect=fake_execute_success):
        doctor.run_doctor(
            project_root=tmp_path,
            url=fake_jupyter_server.url,
            deep=False,
            as_json=True,
            fix=False,
        )

    raw_json = capsys.readouterr().out
    parsed = json.loads(raw_json)
    assert isinstance(parsed, list)
    assert len(parsed) > 0

    # Ensure token is never present in json output
    assert "test-token-12345" not in raw_json
