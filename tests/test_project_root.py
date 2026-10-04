from pathlib import Path

from kaggle_runner import runner_paths


def test_project_root_uses_current_directory_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("KAGGLE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    assert runner_paths.resolve_project_root() == tmp_path.resolve()


def test_project_root_uses_environment_default(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("KAGGLE_PROJECT_DIR", str(project))

    assert runner_paths.resolve_project_root() == project.resolve()


def test_explicit_project_overrides_environment_default(tmp_path, monkeypatch):
    default_project = tmp_path / "default"
    selected_project = tmp_path / "selected"
    default_project.mkdir()
    selected_project.mkdir()
    monkeypatch.setenv("KAGGLE_PROJECT_DIR", str(default_project))

    assert runner_paths.resolve_project_root(selected_project) == Path(
        selected_project
    ).resolve()
