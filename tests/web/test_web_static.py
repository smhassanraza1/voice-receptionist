"""Health check, frontend hosting (SPA fallback, caching) and response headers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from receptionist.web.app import livekit_connect_sources

INDEX_HTML = "<!doctype html><title>Lakeside</title><div id=root></div>"


@pytest.fixture
def built_frontend(web_env: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML)
    (dist / "assets" / "index-3f9a1c.js").write_text("console.log('hi')")
    (dist / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    web_env.setenv("WEB_STATIC_DIR", str(dist))
    return dist


def test_healthz_reports_configuration(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "livekit": True,
        "escalation_intake": True,
        "operator_console": True,
        "frontend": False,
    }


def test_unbuilt_frontend_explains_itself(client: TestClient) -> None:
    for path in ("/", "/operator"):
        resp = client.get(path)
        assert resp.status_code == 503
        assert "npm run build" in resp.text


@pytest.mark.usefixtures("built_frontend")
def test_spa_routes_serve_index(make_client: Callable[[], TestClient]) -> None:
    client = make_client()
    for path in ("/", "/operator", "/operator/"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.text == INDEX_HTML
        assert resp.headers["cache-control"] == "no-cache"
    assert client.get("/healthz").json()["frontend"] is True


@pytest.mark.usefixtures("built_frontend")
def test_hashed_assets_are_immutable(make_client: Callable[[], TestClient]) -> None:
    client = make_client()
    asset = client.get("/assets/index-3f9a1c.js")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    icon = client.get("/favicon.svg")
    assert icon.status_code == 200
    assert icon.headers["cache-control"] == "no-cache"


@pytest.mark.usefixtures("built_frontend")
def test_unknown_paths_404_and_api_is_not_shadowed(make_client: Callable[[], TestClient]) -> None:
    client = make_client()
    assert client.get("/assets/missing.js").status_code == 404
    api_404 = client.get("/api/does-not-exist")
    assert api_404.status_code == 404
    assert api_404.json()["error"] == "not_found"
    assert client.get("/api/token").status_code == 405


def test_security_headers_on_every_response(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "microphone=(self)" in resp.headers["permissions-policy"]


def test_content_security_policy_allows_only_what_the_app_loads(client: TestClient) -> None:
    header = client.get("/healthz").headers["content-security-policy"]
    csp = dict(directive.strip().split(" ", 1) for directive in header.split(";"))
    assert csp["default-src"] == "'self'"
    assert csp["script-src"] == "'self'"
    assert csp["frame-ancestors"] == "'none'"
    assert csp["object-src"] == "'none'"
    assert "https://fonts.googleapis.com" in csp["style-src"]
    assert "https://fonts.gstatic.com" in csp["font-src"]
    assert "'unsafe-inline'" not in csp["style-src"] + csp["script-src"]
    # The test project is on LiveKit Cloud, which may hand out a regional host.
    assert csp["connect-src"].split() == [
        "'self'",
        "wss://*.livekit.cloud",
        "https://*.livekit.cloud",
    ]


@pytest.mark.parametrize(
    ("url", "sources"),
    [
        ("ws://localhost:7880", ["ws://localhost:7880", "http://localhost:7880"]),
        ("wss://livekit.example.com", ["wss://livekit.example.com", "https://livekit.example.com"]),
        ("https://proj.livekit.cloud", ["wss://*.livekit.cloud", "https://*.livekit.cloud"]),
        ("", []),
        ("not a url", []),
    ],
)
def test_livekit_connect_sources(url: str, sources: list[str]) -> None:
    assert livekit_connect_sources(url) == sources
