"""Runner kernel ownership and process-slot tests."""

from kaggle_runner import sync
from kaggle_runner.kernel_manager import KernelManager, RUNNER_BUSY_MESSAGE


def test_runner_kernel_is_owned_reused_and_stoppable(
    tmp_path,
    monkeypatch,
    fake_jupyter_server,
):
    monkeypatch.setenv(
        "KAGGLE_RUNNER_HOME",
        str(tmp_path.parent / f"{tmp_path.name}-runner"),
    )
    sync.bind_state_lock(tmp_path)
    fake_jupyter_server.clear()
    client = sync.JupyterClient(fake_jupyter_server.url)

    manager = KernelManager(client, tmp_path)
    kernel_id = manager.ensure_runner_kernel()
    assert kernel_id in fake_jupyter_server.handler_cls.kernels

    second_manager = KernelManager(client, tmp_path)
    assert second_manager.ensure_runner_kernel() == kernel_id
    assert second_manager.stop_runner_kernel() is True
    assert kernel_id not in fake_jupyter_server.handler_cls.kernels


def test_runner_slot_refuses_concurrent_process_and_releases(
    tmp_path,
    monkeypatch,
    fake_jupyter_server,
):
    monkeypatch.setenv(
        "KAGGLE_RUNNER_HOME",
        str(tmp_path.parent / f"{tmp_path.name}-runner"),
    )
    client = sync.JupyterClient(fake_jupyter_server.url)
    first = KernelManager(client, tmp_path)
    second = KernelManager(client, tmp_path)

    assert first.acquire_runner_slot() is True
    assert second.acquire_runner_slot() is False
    assert RUNNER_BUSY_MESSAGE == (
        "Another kaggle-run is still active "
        "(use --stop, or --new-kernel to run in parallel)"
    )
    first.release_runner_slot()
    assert second.acquire_runner_slot() is True
    second.release_runner_slot()


def test_ephemeral_kernel_is_deleted_without_persisting_id(
    tmp_path,
    monkeypatch,
    fake_jupyter_server,
):
    monkeypatch.setenv(
        "KAGGLE_RUNNER_HOME",
        str(tmp_path.parent / f"{tmp_path.name}-runner"),
    )
    sync.bind_state_lock(tmp_path)
    fake_jupyter_server.clear()
    manager = KernelManager(sync.JupyterClient(fake_jupyter_server.url), tmp_path)

    kernel_id = manager.create_ephemeral_kernel()
    manager.shutdown_ephemeral_kernel()

    assert kernel_id not in fake_jupyter_server.handler_cls.kernels
    assert sync.StateManager(tmp_path).load_runner_kernel() is None
