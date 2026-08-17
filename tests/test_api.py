"""Tests for the local HTTP API (``etekcity_scale_daemon.api``).

Builds the aiohttp application directly (``build_app``) against a
throwaway config file and SQLite database per test, and drives it with
``aiohttp``'s own test client -- no running server or real scale needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from etekcity_scale_daemon.api import build_app
from etekcity_scale_daemon.config import (
    load_api_config,
    load_config,
    load_mqtt_config,
    load_profiles_config,
    load_report_config,
)
from etekcity_scale_daemon.storage import ensure_schema


def _write_config(
    tmp_path: Path,
    *,
    model: str = "",
    api_token: str = "",
    mqtt_enabled: bool = False,
    mqtt_topic_prefix: str = "etekcity_scale_daemon",
) -> Path:
    """Write a minimal INI config file for building a test app.

    Args:
        tmp_path: Pytest's per-test temp directory.
        model: ``[scale] model`` value. Blank means "not configured yet",
            the same state a fresh install is in before a scale is
            discovered.
        api_token: ``[api] token`` value, to exercise the auth-required path.
        mqtt_enabled: ``[mqtt] enabled`` value.
        mqtt_topic_prefix: ``[mqtt] topic_prefix`` value.

    Returns:
        Path to the written config file.
    """
    db_path = tmp_path / "measurements.db"
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        f"""
[scale]
address = AA:BB:CC:DD:EE:FF
model = {model}

[storage]
db_path = {db_path}

[daemon]
log_level = INFO

[api]
enabled = yes
token = {api_token}

[mqtt]
enabled = {"yes" if mqtt_enabled else "no"}
host = localhost
topic_prefix = {mqtt_topic_prefix}
"""
    )
    return config_path


def _build_app(tmp_path: Path, **config_kwargs: object) -> web.Application:
    """Build a fully wired aiohttp app the same way ``api.main`` does.

    Args:
        tmp_path: Pytest's per-test temp directory, used for the config
            file and SQLite database.
        **config_kwargs: Forwarded to ``_write_config``.

    Returns:
        An unstarted aiohttp Application, ready for a test client.
    """
    config_path = _write_config(tmp_path, **config_kwargs)
    daemon_config = load_config(str(config_path))
    api_config = load_api_config(str(config_path))
    report_config = load_report_config(str(config_path))
    profiles_config = load_profiles_config(str(config_path))
    mqtt_config = load_mqtt_config(str(config_path))
    ensure_schema(daemon_config.db_path)
    return build_app(
        str(config_path),
        daemon_config.db_path,
        api_config,
        report_config,
        profiles_config,
        daemon_config,
        mqtt_config,
    )


def _request(
    app: web.Application, method: str, path: str, headers: dict[str, str] | None = None
) -> tuple[int, object]:
    """Issue one request against ``app`` and return (status, JSON body).

    Spins up a throwaway ``TestServer``/``TestClient`` pair for a single
    request -- simplest way to exercise real routing/auth/serialization
    without a pytest-asyncio dependency.

    Args:
        app: The application under test.
        method: HTTP method, e.g. ``"GET"``.
        path: Request path, e.g. ``"/api/v1/health"``.
        headers: Optional request headers (e.g. ``Authorization``).

    Returns:
        A ``(status_code, json_body)`` tuple.
    """

    async def _run() -> tuple[int, object]:
        async with TestClient(TestServer(app)) as client:
            response = await client.request(method, path, headers=headers)
            # A 404 from aiohttp's default handler (an unregistered route,
            # as opposed to one of our handlers) is plain text, not JSON.
            if response.content_type != "application/json":
                return response.status, None
            body = await response.json()
            return response.status, body

    return asyncio.run(_run())


def test_health_is_served_under_api_v1(tmp_path: Path) -> None:
    """The versioned /api/v1/health path is live and unauthenticated."""
    app = _build_app(tmp_path)
    status, body = _request(app, "GET", "/api/v1/health")
    assert status == 200
    assert body["status"] == "ok"


def test_unversioned_paths_are_gone(tmp_path: Path) -> None:
    """The old bare paths (pre-versioning) no longer resolve to anything.

    Builds a fresh app per path rather than reusing one across several
    ``_request`` calls: an aiohttp ``Application`` binds to the event loop
    of its first request, and each ``_request`` call runs its own
    ``asyncio.run`` (a new loop), so reuse across calls would raise
    "initialized with different loop" instead of testing anything.
    """
    for path in ("/health", "/latest", "/report", "/assign-profile", "/capabilities"):
        app = _build_app(tmp_path)
        status, _ = _request(app, "GET", path)
        assert status == 404, f"{path} should not be routable anymore"


def test_capabilities_is_weight_only_when_no_model_configured(tmp_path: Path) -> None:
    """Before a scale is discovered, capabilities must not overclaim."""
    app = _build_app(tmp_path, model="")
    status, body = _request(app, "GET", "/api/v1/capabilities")
    assert status == 200
    assert body == {
        "daemon": "etekcity-scale",
        "api_version": "v1",
        "measurement_types": ["weight"],
        "measurement_modes": ["spot"],
        "profile_model": "assignable",
        "timestamp_fields": body["timestamp_fields"],
        "mqtt": {"enabled": False},
    }
    assert "recorded_at" in body["timestamp_fields"]
    assert "measured_at" not in body["timestamp_fields"]


def test_capabilities_reflects_heart_rate_capable_model(tmp_path: Path) -> None:
    """EFS-A591S supports impedance and heart rate but not the 500kHz band."""
    app = _build_app(tmp_path, model="EFS-A591S")
    status, body = _request(app, "GET", "/api/v1/capabilities")
    assert status == 200
    assert set(body["measurement_types"]) == {
        "weight",
        "impedance",
        "body_composition",
        "heart_rate",
    }


def test_capabilities_reflects_impedance_only_model_and_mqtt(tmp_path: Path) -> None:
    """ESF-24 has no heart rate; MQTT settings are echoed when enabled.

    This, together with the previous test, proves the response actually
    varies with the configured model instead of being a static payload.
    """
    app = _build_app(
        tmp_path, model="ESF-24", mqtt_enabled=True, mqtt_topic_prefix="scale"
    )
    status, body = _request(app, "GET", "/api/v1/capabilities")
    assert status == 200
    assert set(body["measurement_types"]) == {"weight", "impedance", "body_composition"}
    assert "heart_rate" not in body["measurement_types"]
    assert body["mqtt"] == {"enabled": True, "topic_pattern": "scale/<address>/state"}


def test_capabilities_is_unauthenticated_even_with_token_set(tmp_path: Path) -> None:
    """Like /health, /capabilities must stay reachable with no Authorization header."""
    app = _build_app(tmp_path, model="", api_token="secret")
    status, _ = _request(app, "GET", "/api/v1/capabilities")
    assert status == 200


def test_latest_requires_auth_when_token_set(tmp_path: Path) -> None:
    """Sanity check that auth still applies to the versioned non-exempt routes."""
    app = _build_app(tmp_path, model="", api_token="secret")
    status, body = _request(app, "GET", "/api/v1/latest")
    assert status == 401
    assert body["error"] == "unauthorized"
