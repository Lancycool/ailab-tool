import json

from fastapi.testclient import TestClient

from campus_runner.api import create_app
from campus_runner.mcp_server import handle
from conftest import batch


def test_api_auth_origin_and_separate_approval(service):
    app = create_app(service.store.home, service=service, background=False)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    service_headers = {"Authorization": "Bearer " + service.store.token()}
    panel_headers = {"Authorization": "Bearer " + service.store.token("panel")}
    assert client.post("/api/tools/get_status", json={}).status_code == 401
    assert client.post("/api/tools/get_status", json={}, headers={**service_headers, "Origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/tools/get_status", json={}, headers={**service_headers, "Host": "evil.test"}).status_code == 403
    result = client.post("/api/tools/create_batch", json={"batch": batch(service)}, headers=service_headers).json()
    payload = {"batch_id": result["batch_id"], "digest": result["digest"]}
    assert client.post("/api/panel/approve", json=payload, headers=service_headers).status_code == 403
    assert client.post("/api/panel/approve", json=payload, headers=panel_headers).status_code == 200
    assert "test-secret" not in client.post("/api/tools/get_status", json={}, headers=service_headers).text


def test_static_assets_and_no_inline_script(service):
    client = TestClient(create_app(service.store.home, service=service, background=False), base_url="http://127.0.0.1:8765")
    response = client.get("/")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/secret").status_code == 404
    assert "innerHTML" not in client.get("/static/app.js").text


def test_mcp_protocol_and_tools():
    call = lambda name, args: {"name": name, "args": args}
    init = handle({"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}, call)
    assert init["result"]["capabilities"]["tools"] == {"listChanged": False}
    tools = handle({"id": 2, "method": "tools/list"}, call)["result"]["tools"]
    assert len(tools) == 10
    assert not any("approve" in t["name"] for t in tools)
    assert handle({"method": "notifications/initialized"}, call) is None
    result = handle({"id": 3, "method": "tools/call", "params": {"name": "list_instances"}}, call)
    assert not result["result"]["isError"]
    assert handle({"id": 4, "method": "tools/call", "params": {"name": "arbitrary_shell"}}, call)["error"]["code"] == -32602
