"""HTTP API 测试：状态码映射、可区分错误体、幂等复用标记、生命周期接口。"""

from __future__ import annotations


def _body(resp) -> dict:
    return resp.json()


def test_provision_and_get(client):
    resp = client.post("/environments", json={
        "project": "proj", "branch": "feat", "request_key": "k1", "ttl_seconds": 30,
    })
    assert resp.status_code == 201
    data = _body(resp)
    assert data["env_id"] == "proj-feat"
    assert data["status"] == "ready"
    assert data["reused"] is False
    assert data["expires_at"] is not None

    again = client.post("/environments", json={
        "project": "proj", "branch": "feat", "request_key": "k1",
    })
    assert again.status_code == 201
    assert _body(again)["reused"] is True
    assert _body(again)["env_id"] == data["env_id"]

    got = client.get("/environments/k1")
    assert got.status_code == 200
    assert _body(got)["history"]  # 状态流转历史可见


def test_invalid_parameter_returns_400(client):
    resp = client.post("/environments", json={
        "project": "", "branch": "b", "request_key": "k",
    })
    assert resp.status_code == 422  # pydantic 层拦截空串

    resp = client.post("/environments", json={
        "project": " ", "branch": "b", "request_key": "k",
    })
    assert resp.status_code == 400
    body = _body(resp)
    assert body["error"]["code"] == "INVALID_PARAMETER"
    assert body["error"]["request_key"] == "k"


def test_identifier_conflict_returns_409(client):
    client.post("/environments", json={
        "project": "p", "branch": "a", "request_key": "k",
    })
    resp = client.post("/environments", json={
        "project": "p", "branch": "b", "request_key": "k",
    })
    assert resp.status_code == 409
    assert _body(resp)["error"]["code"] == "IDENTIFIER_CONFLICT"


def test_quota_exhausted_returns_429(client):
    for i in range(3):
        client.post("/environments", json={
            "project": "p", "branch": f"b{i}", "request_key": f"k{i}",
        })
    resp = client.post("/environments", json={
        "project": "p", "branch": "overflow", "request_key": "kx",
    })
    assert resp.status_code == 429
    assert _body(resp)["error"]["code"] == "QUOTA_EXHAUSTED"


def test_not_found_and_reclaimed_404_and_410(client):
    assert client.get("/environments/missing").status_code == 404
    assert _body(client.get("/environments/missing"))["error"]["code"] == "ENV_NOT_FOUND"

    client.post("/environments", json={
        "project": "p", "branch": "b", "request_key": "k",
    })
    rec = client.delete("/environments/k")
    assert rec.status_code == 200
    assert _body(rec)["released"] is True
    rec_again = client.delete("/environments/k")
    assert _body(rec_again)["released"] is False  # 重复回收安全

    assert client.get("/environments/k").status_code == 410
    assert _body(client.get("/environments/k"))["error"]["code"] == "ENV_RECLAIMED"


def test_retry_endpoint_after_failure(client, orchestrator, backend):
    backend.fail_once("p-bug")
    r1 = client.post("/environments", json={
        "project": "p", "branch": "bug", "request_key": "k",
    })
    assert r1.status_code == 201
    assert _body(r1)["status"] == "failed"

    r2 = client.post("/environments/k/retry")
    assert r2.status_code == 200
    assert _body(r2)["status"] == "ready"
    assert _body(r2)["failure_count"] == 1


def test_renew_and_reap_endpoints(client, orchestrator, clock):
    client.post("/environments", json={
        "project": "p", "branch": "b", "request_key": "k", "ttl_seconds": 10,
    })
    ok = client.post("/environments/k/renew", json={"ttl_seconds": 100})
    assert ok.status_code == 200

    clock.advance(101)
    reaped = client.post("/reap")
    assert reaped.status_code == 200
    assert _body(reaped)["reclaimed"] == ["p-b"]


def test_quota_endpoint(client):
    client.post("/environments", json={
        "project": "p", "branch": "b", "request_key": "k",
    })
    resp = client.get("/quota")
    assert resp.status_code == 200
    data = _body(resp)
    assert data["quota"] == 3 and data["used"] == 1 and data["available"] == 2
    assert data["status_ready"] == 1
