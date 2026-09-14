"""テスト共通のフィクスチャ。"""

from __future__ import annotations

import pytest

from snmp_monitor.config import AppConfig, PollingConfig, StorageConfig
from snmp_monitor.simulator import DEMO_DEVICES, reset_simulator_state
from snmp_monitor.storage.db import Database
from snmp_monitor.storage.timeseries import TimeSeriesStore


@pytest.fixture(autouse=True)
def _clean_simulator():
    reset_simulator_state()
    yield
    reset_simulator_state()


@pytest.fixture
async def database():
    db = Database(":memory:")
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
async def store(database):
    return TimeSeriesStore(database, StorageConfig(path=":memory:"))


@pytest.fixture
def demo_app_config(tmp_path) -> AppConfig:
    return AppConfig(
        demo_mode=True,
        devices=[d.model_copy(deep=True) for d in DEMO_DEVICES],
        polling=PollingConfig(interval_seconds=5.0),
        storage=StorageConfig(path=tmp_path / "test.db"),
    )
