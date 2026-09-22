"""生命周期、参数校验与标识冲突测试。"""

from __future__ import annotations

import pytest

from preview_orchestrator.errors import (
    EnvironmentNotFoundError,
    IdentifierConflictError,
    InvalidParameterError,
)
from preview_orchestrator.models import EnvironmentRequest, EnvironmentStatus


class TestRequestValidation:
    @pytest.mark.parametrize(
        "project,branch,ttl",
        [
            ("", "main", 10),
            ("   ", "main", 10),
            ("proj", "", 10),
            ("proj", "  ", 10),
            ("proj", "main", 0),
            ("proj", "main", -5),
            ("proj", "main", 24 * 3600 + 1),
            ("bad project", "main", 10),
            ("proj", "ma in", 10),
        ],
    )
    def test_invalid_parameters_rejected(self, make_orchestrator, project, branch, ttl):
        orch = make_orchestrator()
        with pytest.raises(InvalidParameterError) as exc_info:
            orch.request_environment(EnvironmentRequest(project, branch, ttl, "k"))
        assert exc_info.value.code == "invalid_parameter"

    def test_default_request_key_is_project_colon_branch(self, make_orchestrator):
        orch = make_orchestrator()
        env = orch.request_environment(EnvironmentRequest("proj", "feat/x", 60))
        assert env.request_key == "proj:feat/x"


class TestLifecycle:
    def test_pending_to_ready_happy_path(self, make_orchestrator):
        orch = make_orchestrator()
        env = orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        assert env.status is EnvironmentStatus.READY
        assert env.environment_id
        assert env.attempts == 1
        assert env.failure_reason is None
        got = orch.get_environment("k1")
        assert got is env

    def test_identifier_conflict_between_distinct_keys(self, make_orchestrator):
        orch = make_orchestrator()
        orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        with pytest.raises(IdentifierConflictError) as exc_info:
            orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k2"))
        assert exc_info.value.code == "identifier_conflict"
        assert "k1" in str(exc_info.value)

    def test_reclaimed_environment_not_accessible(self, make_orchestrator):
        orch = make_orchestrator()
        orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        orch.reclaim("k1")
        with pytest.raises(EnvironmentNotFoundError) as exc_info:
            orch.get_environment("k1")
        assert exc_info.value.code == "environment_not_found"

    def test_reclaim_releases_identifier_for_reuse(self, make_orchestrator):
        orch = make_orchestrator(quota=1)
        orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        orch.reclaim("k1")
        # 同项目分支用新键可再次申请
        env = orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k2"))
        assert env.status is EnvironmentStatus.READY
        assert orch.pool.used == 1

    def test_get_unknown_key_raises_not_found(self, make_orchestrator):
        orch = make_orchestrator()
        with pytest.raises(EnvironmentNotFoundError):
            orch.get_environment("missing")

    def test_reclaim_unknown_key_raises_not_found(self, make_orchestrator):
        orch = make_orchestrator()
        with pytest.raises(EnvironmentNotFoundError):
            orch.reclaim("missing")

    def test_renew_extends_expiry(self, make_orchestrator):
        current = [1000.0]
        orch = make_orchestrator(clock=lambda: current[0])
        env = orch.request_environment(EnvironmentRequest("proj", "feat-1", 60, "k1"))
        assert env.expires_at == 1060.0
        current[0] = 1030.0
        renewed = orch.renew("k1", 60)
        assert renewed.expires_at == 1090.0
