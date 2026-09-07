"""Keep CLI configuration in temporary directories during tests."""

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def app(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "laposte_cli.py"
    loader = importlib.machinery.SourceFileLoader("auth_contract_laposte", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(module, "CONFIG_DIR", tmp_path)
    return module
