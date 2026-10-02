from types import SimpleNamespace

import pytest

from kaggle_runner import pull, runner_paths, sync


@pytest.fixture
def isolated_project(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    runner_home = tmp_path / "runner-home"
    monkeypatch.setattr(runner_paths, "runner_home", lambda: runner_home)
    sync.bind_state_lock(project)
    return project


def test_remote_tree_list_returns_files_and_directories(
    monkeypatch,
    isolated_project,
):
    def list_directory(_client, remote_dir):
        if remote_dir == sync.REMOTE_ROOT:
            return [
                {
                    "type": "directory",
                    "path": "local-project/outputs",
                },
                {
                    "type": "file",
                    "path": "local-project/readme.txt",
                },
            ]
        if remote_dir == "local-project/outputs":
            return [
                {
                    "type": "file",
                    "path": "local-project/outputs/model.pt",
                },
            ]
        return []

    monkeypatch.setattr(sync, "_list_remote_dir", list_directory)

    entries = sync.list_tree(object(), isolated_project)

    assert {(entry["type"], entry["path"]) for entry in entries} == {
        ("directory", "local-project/outputs"),
        ("file", "local-project/readme.txt"),
        ("file", "local-project/outputs/model.pt"),
    }


def test_directory_pull_expansion_uses_tree_directories(
    monkeypatch,
    isolated_project,
):
    entries = [
        {"type": "directory", "path": "local-project/outputs"},
        {"type": "directory", "path": "local-project/outputs/nested"},
        {"type": "file", "path": "local-project/outputs/a.pt"},
        {"type": "file", "path": "local-project/outputs/nested/b.pt"},
        {"type": "file", "path": "local-project/other.pt"},
    ]
    monkeypatch.setattr(sync, "list_tree", lambda *_args: entries)

    targets, problems = pull.expand_specs(
        object(),
        isolated_project,
        ["outputs"],
    )

    assert targets == [
        "outputs/a.pt",
        "outputs/nested/b.pt",
    ]
    assert problems == []


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "outputs/../../outside.txt",
        "/outside.txt",
        "/kaggle/working/not-local-project/file",
        "C:/outside.txt",
    ],
)
def test_pull_rejects_traversal_and_external_absolute_paths(path):
    with pytest.raises(ValueError):
        pull.to_relative(path)


def test_pull_refuses_ignored_file_without_override(
    isolated_project,
    capsys,
):
    (isolated_project / ".kagglesyncignore").write_text(
        "outputs/\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(dest=None, force=True, include_ignored=False)

    assert not pull.pull_one(
        object(),
        isolated_project,
        "outputs/model.pt",
        1,
        args,
        None,
    )
    assert "use --include-ignored" in capsys.readouterr().out
    assert not (isolated_project / "outputs").exists()


def test_include_ignored_allows_explicit_ignored_pull(
    monkeypatch,
    isolated_project,
):
    (isolated_project / ".kagglesyncignore").write_text(
        "outputs/\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pull,
        "download_raw",
        lambda _client, _url, part_path, _total, _display: (
            part_path.write_bytes(b"data") or 4
        ),
    )
    args = SimpleNamespace(dest=None, force=True, include_ignored=True)
    client = SimpleNamespace(
        base_url="https://example.invalid/k/test/redacted/proxy",
        token="test-token",
    )

    assert pull.pull_one(
        client,
        isolated_project,
        "outputs/model.pt",
        4,
        args,
        None,
    )
    assert (isolated_project / "outputs" / "model.pt").read_bytes() == b"data"


def test_pull_rejects_symlink_destination_outside_project(
    isolated_project,
    tmp_path,
    capsys,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = isolated_project / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    args = SimpleNamespace(dest=None, force=True, include_ignored=True)
    assert not pull.pull_one(
        object(),
        isolated_project,
        "linked/escape.bin",
        1,
        args,
        None,
    )
    assert "outside the project" in capsys.readouterr().out
    assert not (outside / "escape.bin").exists()


@pytest.mark.parametrize(
    "name",
    [
        "partial.kaggle-pull.part",
        "partial.kaggle-sync-part",
        "partial.kaggle-sync.tmp",
    ],
)
def test_sync_iteration_skips_download_temporary_files(
    isolated_project,
    name,
):
    path = isolated_project / name
    path.write_bytes(b"partial")

    assert sync.should_skip(path)
    assert path not in sync.iter_local_files(isolated_project)


def test_jupyter_download_streams_files_endpoint(
    isolated_project,
    fake_jupyter_server,
):
    from kaggle_runner.sync import JupyterClient

    payload = b"streamed remote bytes"
    fake_jupyter_server.clear()
    fake_jupyter_server.handler_cls.storage[
        "contents/local-project/stream.bin"
    ] = payload
    client = JupyterClient(fake_jupyter_server.url)
    destination = isolated_project / "stream.bin"

    client.download_file("local-project/stream.bin", destination, len(payload))

    assert destination.read_bytes() == payload
    assert not list(isolated_project.glob("*.kaggle-sync.tmp"))


def test_jupyter_download_uses_bounded_contents_fallback(
    isolated_project,
    fake_jupyter_server,
):
    from kaggle_runner.sync import JupyterClient

    fake_jupyter_server.clear()
    fake_jupyter_server.handler_cls.storage[
        "contents/local-project/fallback.bin"
    ] = b"small fallback"
    client = JupyterClient(fake_jupyter_server.url)
    destination = isolated_project / "fallback.bin"

    client.download_file(
        "local-project/fallback.bin",
        destination,
        len(b"small fallback"),
    )
    assert destination.read_bytes() == b"small fallback"

    with pytest.raises(RuntimeError, match="100-MB"):
        client.download_file(
            "local-project/missing.bin",
            isolated_project / "large.bin",
            100 * 1024 * 1024 + 1,
        )
