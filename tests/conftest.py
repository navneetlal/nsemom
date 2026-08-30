import datetime as dt
from pathlib import Path

import pytest

from nsemom.config import Config

FIXTURES = Path(__file__).parent / "fixtures"
OVERLAP_DAY = dt.date(2024, 3, 1)  # published in both formats


@pytest.fixture(scope="session")
def cfg() -> Config:
    return Config.load()


@pytest.fixture(scope="session")
def universe(cfg):
    return cfg.universe
