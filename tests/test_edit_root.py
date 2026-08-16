"""Tests for the configurable filesystem edit root (AGENT_LEGACY_EDIT_ROOT)."""
from agent import config


def test_edit_root_defaults_to_workspace(monkeypatch):
    monkeypatch.delenv("AGENT_LEGACY_EDIT_ROOT", raising=False)
    assert config._edit_root() == config.PROJECT_ROOT / "workspace"


def test_edit_root_env_override_resolves(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_LEGACY_EDIT_ROOT", str(tmp_path))
    assert config._edit_root() == tmp_path.resolve()


def test_settings_edit_root_is_usable_directory():
    assert config.settings.EDIT_ROOT.is_dir()


def test_factory_points_backend_at_edit_root():
    import inspect

    from agent.core import agent_factory

    source = inspect.getsource(agent_factory.build_agent)
    assert "FilesystemBackend(root_dir=str(settings.EDIT_ROOT))" in source
