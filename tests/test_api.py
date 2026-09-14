"""REST / WebSocket API のテスト。

TestClient を with で使うと lifespan が動くため、実際にポーリングも走る。
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from snmp_monitor.api import RANGE_SECONDS, create_app
from snmp_monitor.config import AppConfig, PollingConfig, StorageConfig
from snmp_monitor.simulator import DEMO_DEVICES


@pytest.fixture
def client(tmp_path):
    config = AppConfig(
        demo_mode=True,
        devices=[d.model_copy(deep=True) for d in DEMO_DEVICES[:2]],
        polling=PollingConfig(interval_seconds=5.0),
        storage=StorageConfig(path=tmp_path / "api-test.db"),
    )
    with TestClient(create_app(config)) as test_client:
        yield test_client


def wait_for_poll(client: TestClient, timeout: float = 20.0) -> dict:
    """最初のポーリングが終わるまで待つ (デモは起動を数秒ずらすため)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        summary = client.get("/api/summary").json()
        if any(d["status"] != "unknown" for d in summary["devices"]):
            return summary
        time.sleep(0.25)
    pytest.fail("ポーリング結果が得られませんでした")


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "SNMP Monitor" in response.text


def test_static_assets_are_served(client):
    for path in ("/static/app.js", "/static/chart.js", "/static/style.css"):
        assert client.get(path).status_code == 200


def test_summary_shape(client):
    data = client.get("/api/summary").json()
    assert data["devices_total"] == 2
    assert data["demo_mode"] is True
    assert len(data["devices"]) == 2
    device = data["devices"][0]
    for key in ("id", "name", "host", "status", "snmp_version", "ports"):
        assert key in device


def test_device_list_matches_summary(client):
    devices = client.get("/api/devices").json()
    assert {d["id"] for d in devices} == {"demo-gateway", "demo-switch-1"}


def test_unknown_device_returns_404(client):
    assert client.get("/api/devices/does-not-exist").status_code == 404
    assert client.get("/api/devices/does-not-exist/interfaces").status_code == 404
    assert client.get("/api/devices/does-not-exist/history").status_code == 404


def test_invalid_range_returns_400(client):
    response = client.get("/api/devices/demo-gateway/history?range=100y")
    assert response.status_code == 400


@pytest.mark.parametrize("range_key", list(RANGE_SECONDS))
def test_all_ranges_are_accepted(client, range_key):
    response = client.get(f"/api/devices/demo-gateway/history?range={range_key}")
    assert response.status_code == 200
    assert response.json()["range"] == range_key


def test_events_endpoint(client):
    response = client.get("/api/events?limit=5")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_events_limit_is_validated(client):
    assert client.get("/api/events?limit=0").status_code == 422
    assert client.get("/api/events?limit=100000").status_code == 422


def test_sparklines_endpoint(client):
    data = client.get("/api/sparklines?range=15m").json()
    assert "devices" in data
    assert data["range"] == "15m"


def test_websocket_sends_initial_summary(client):
    with client.websocket_connect("/ws") as websocket:
        message = json.loads(websocket.receive_text())
        assert message["type"] == "summary"
        assert message["data"]["devices_total"] == 2


def test_polling_populates_device_state(client):
    summary = wait_for_poll(client)
    device = next(d for d in summary["devices"] if d["status"] == "up")

    detail = client.get(f"/api/devices/{device['id']}").json()
    assert detail["sys_descr"]
    assert detail["interfaces"], "インターフェースが取得できていません"
    interface = detail["interfaces"][0]
    assert interface["label"]
    assert interface["oper_status_label"] in {"up", "down", "testing", "unknown"}

    interfaces = client.get(f"/api/devices/{device['id']}/interfaces").json()
    assert len(interfaces) == len(detail["interfaces"])

    history = client.get(f"/api/devices/{device['id']}/history?range=15m").json()
    assert history["resolution"] == "raw"
    assert len(history["metrics"]) >= 1
