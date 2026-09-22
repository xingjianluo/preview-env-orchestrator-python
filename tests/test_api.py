"""HTTP 接口集成测试：状态码、错误分类与端到端生命周期。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from preview_orchestrator.api import create_app
from preview_orchestrator.orchestrator import Orchestrator
from preview_orchestrator.resources import Provisioner


@pytest.fixture
def client():
    orch = Orchestrator(quota=2)
    app = create_app(orch)
    return TestClient(app), orch


def test_request_then_idempotent_duplicate(client):
    c, _ = client
    r1 = c.post("/environments", json={"project": "proj", "branch": "feat-1", "ttl_seconds": 60})
    assert r1.status_code == 201
    body1 = r1.json()
    assert body1["status"] == "ready"
    r2 = c.post("/environments", json={"project": "proj", "branch": "feat-1", "ttl_seconds": 60})
    assert r2.status_code == 201
    assert r2.json()["environment_id"] == body1["environment_id"]


def test_distinct_error_codes_map_to_distinct_http_status(client):
    c, orch = client
    # 参数非法 -> 400 invalid_parameter
    r = c.post("/environments", json={"project": "", "branch": "b"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_parameter"
    # 标识冲突 -> 409 identifier_conflict
    c.post("/environments", json={"project": "p", "branch": "b", "request_key": "k1"})
    r = c.post("/environments", json={"project": "p", "branch": "b", "request_key": "k2"})
    assert r.status_code == 409 and r.json()["error"] == "identifier_conflict"
    # 配额耗尽 -> 429 quota_exhausted
    c.post("/environments", json={"project": "p2", "branch": "b", "request_key": "k3"})
    r = c.post("/environments", json={"project": "p3", "branch": "b", "request_key": "k4"})
    assert r.status_code == 429 and r.json()["error"] == "quota_exhausted"
    # 未找到 -> 404 environment_not_found
    r = c.get("/environments/nope")
    assert r.status_code == 404 and r.json()["error"] == "environment_not_found"


def test_full_lifecycle_over_http():
    provisioner = Provisioner()
    provisioner.always_fail_ids.add("env-000001")
    orch = Orchestrator(quota=1, provisioner=provisioner)
    c = TestClient(create_app(orch))

    r = c.post("/environments", json={"project": "p", "branch": "b", "ttl_seconds": 60, "request_key": "k1"})
    assert r.status_code == 201 and r.json()["status"] == "failed"

    # 修复故障后通过 retry 端点重试
    provisioner.always_fail_ids.clear()
    r = c.post("/environments/k1/retry")
    assert r.status_code == 200 and r.json()["status"] == "ready"

    r = c.post("/environments/k1/renew", json={"ttl_seconds": 120})
    assert r.status_code == 200 and r.json()["ttl_seconds"] == 120

    r = c.get("/quota")
    assert r.json() == {
        "capacity": 1,
        "used": 1,
        "available": 0,
        "active_resources": ["env-000001"],
    }

    r = c.delete("/environments/k1")
    assert r.status_code == 200 and r.json()["status"] == "reclaimed"
    # 重复回收仍然 200（幂等）
    assert c.delete("/environments/k1").status_code == 200
    # 回收后不可访问
    assert c.get("/environments/k1").status_code == 404
    assert c.get("/quota").json()["used"] == 0


def test_reap_expired_endpoint():
    class Clock:
        def __init__(self):
            self.t = 1000.0

        def __call__(self):
            return self.t

    clock = Clock()
    orch = Orchestrator(quota=2, clock=clock)
    c = TestClient(create_app(orch))
    c.post("/environments", json={"project": "p", "branch": "b1", "ttl_seconds": 10, "request_key": "k1"})
    clock.t += 10
    r = c.post("/reap-expired")
    assert r.status_code == 200 and r.json() == {"reclaimed": ["k1"]}
