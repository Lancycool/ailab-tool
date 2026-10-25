import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .service import Service


def create_app(home, service=None, background=True, port=8765):
    manager = service or Service(home)

    @asynccontextmanager
    async def lifespan(app):
        if background:
            manager.start()
        yield
        manager.close()

    app = FastAPI(title="Campus Runner", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager
    static = Path(__file__).with_name("static")

    @app.middleware("http")
    async def boundary(request, call_next):
        valid_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if request.headers.get("host") not in valid_hosts:
            return JSONResponse({"error": "InvalidHost"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in {"http://" + h for h in valid_hosts}:
            return JSONResponse({"error": "InvalidOrigin"}, status_code=403)
        if request.url.path.startswith("/api/"):
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            panel = hmac.compare_digest(token, manager.store.token("panel"))
            service_ok = hmac.compare_digest(token, manager.store.token())
            if not (panel or service_ok):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if request.url.path.startswith("/api/panel/") and not panel:
                return JSONResponse({"error": "PanelApprovalRequired"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        message = "；".join(e["msg"] for e in exc.errors()) if isinstance(exc, ValidationError) else str(exc)
        return JSONResponse({"error": type(exc).__name__, "message": message}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"error": "NotFound", "message": str(exc)}, status_code=404)

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    @app.get("/static/{name}")
    def asset(name: str):
        if name not in ("app.js", "style.css"):
            raise HTTPException(404)
        return FileResponse(static / name)

    @app.post("/api/panel/instances")
    def save_instance(payload: dict):
        value = dict(payload)
        password = value.pop("password", None)
        return manager.save_instance(value, password)

    @app.post("/api/panel/host-key")
    def host_key(payload: dict):
        return manager.confirm_key(payload["instance_id"], payload["fingerprint"])

    @app.post("/api/panel/approve")
    def approve(payload: dict):
        return manager.approve(payload["batch_id"], payload["digest"])

    @app.post("/api/tools/{name}")
    async def tool(name: str, payload: dict):
        return await run_in_threadpool(dispatch, manager, name, payload)

    return app


def dispatch(manager, name, args):
    calls = {
        "list_instances": lambda: manager.list_instances(),
        "inspect_instance": lambda: manager.operation(name, lambda: manager.inspect_instance(**args)),
        "prepare_snapshot": lambda: manager.operation(name, lambda: manager.prepare_snapshot(**args)),
        "create_batch": lambda: manager.create_batch(args["batch"]),
        "submit_batch": lambda: manager.operation(name, lambda: manager.submit_batch(args["key"])),
        "get_status": lambda: manager.get_status(**args),
        "read_logs": lambda: manager.read_logs(**args),
        "get_metrics": lambda: manager.get_metrics(**args),
        "fetch_artifacts": lambda: manager.operation(name, lambda: manager.fetch_artifacts(**args)),
        "cancel_jobs": lambda: manager.operation(name, lambda: manager.cancel_jobs(**args)),
    }
    if name not in calls:
        raise ValueError("未知工具")
    try:
        return calls[name]()
    except TypeError as exc:
        raise ValueError("工具参数不正确：" + str(exc)) from exc
