from pathlib import Path

import pytest

from src import config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def settings() -> dict:
    return config.load_settings()


@pytest.fixture
def filters() -> dict:
    return config.load_filters()


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def read_fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()
