"""Shared test fixtures for unladen."""

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def sample_project(fixtures_dir) -> Path:
    """A minimal project with a pyproject.toml and declared dependencies."""
    return fixtures_dir / "sample_project"


@pytest.fixture
def empty_project(tmp_path) -> Path:
    """A project directory with no pyproject.toml."""
    return tmp_path


@pytest.fixture
def fake_site_packages(fixtures_dir) -> Path:
    """A fake site-packages directory with mock installed packages."""
    return fixtures_dir / "fake_site_packages"
