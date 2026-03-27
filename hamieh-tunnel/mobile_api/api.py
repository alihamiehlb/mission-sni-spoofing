"""
Mobile API — REST + WebSocket interface.

Exposes tunnel control and status to:
  - Flutter mobile app (via HTTP on 127.0.0.1:8080)
  - Android VpnService bridge
  - Local CLI tooling

REST Endpoints:
  POST /api/tunnel/start       Start the tunnel
  POST /api/tunnel/stop        Stop the tunnel
  GET  /api/tunnel/status      Current status + metrics
  GET  /api/tunnel/logs        Recent log lines
  POST /api/config             Update config at runtime
  GET  /api/health             Liveness check

WebSocket:
  WS /ws/status                Real-time status + bandwidth updates (1s interval)
  WS /ws/logs                  Real-time log stream

Authentication: Bearer token in Authorization header (same token as relay).

All responses are JSON. Errors follow: {"error": "message", "code": N}
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import asdict
from typing import Any, Optional

from aiohttp import web, WSMsgType

from core.config import HamiehConfig
from core.metrics import get_metrics

logger = logging.getLogger(__name__)

# Keep last N log lines for the /api/tunnel/logs endpoint
_LOG_BUFFER: deque[dict] = deque(maxlen=500)
_WEBSOCKET_CLIENTS: set[web.WebSocketResponse] = set()


class _BufferingHandler(logging.Handler):
    """Captures log records into the in-memory buffer for the API."""

    def emit(self, record: logging.LogRecord) -> None:
        _LOG_BUFFER.append({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        })


def _install_log_buffer() -> None:
    logging.getLogger().addHandler(_BufferingHandler())


class TunnelController:
    """
    Manages tunnel lifecycle on behalf of the API.
    Holds references to SOCKS5 server, TunnelManager, and optional TUN manager.
    """

    def __init__(self) -> None:
        self._running = False
        self._start_time: Optional[float] = None
        self._tunnel_manager = None
        self._socks5_server = None
        self._tun_manager = None
        self._cfg: Optional[HamiehConfig] = None

    def attach(self, cfg, tunnel_manager, socks5_server, tun_manager=None) -> None:
        self._cfg = cfg
        self._tunnel_manager = tunnel_manager
        self._socks5_server = socks5_server
        self._tun_manager = tun_manager

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> dict:
        if self._running:
            return {"status": "already_running"}
        if not self._tunnel_manager or not self._socks5_server:
            return {"error": "Not configured", "code": 503}

        try:
            await self._tunnel_manager.start()
            await self._socks5_server.start()
            if self._tun_manager and self._cfg and self._cfg.tun.enabled:
                await self._tun_manager.start()
            self._running = True
            self._start_time = time.time()
            logger.info("Tunnel started via mobile API")
            return {"status": "started"}
        except Exception as e:
            logger.error("Start failed: %s", e)
            return {"error": str(e), "code": 500}

    async def stop(self) -> dict:
        if not self._running:
            return {"status": "not_running"}

        try:
            if self._tun_manager:
                try:
                    await self._tun_manager.stop()
                except Exception:
                    pass
            if self._socks5_server:
                await self._socks5_server.stop()
            if self._tunnel_manager:
                await self._tunnel_manager.stop()
            self._running = False
            self._start_time = None
            logger.info("Tunnel stopped via mobile API")
            return {"status": "stopped"}
        except Exception as e:
            return {"error": str(e), "code": 500}

    def status(self) -> dict:
        m = get_metrics()
        return {
            "running": self._running,
            "uptime_seconds": round(time.time() - self._start_time, 1) if self._start_time else 0,
            "socks5": {
                "host": self._cfg.socks5.bind_host if self._cfg else "127.0.0.1",
                "port": self._cfg.socks5.bind_port if self._cfg else 1080,
            },
            "transport": self._cfg.transport.type if self._cfg else "none",
            "relay": f"{self._cfg.transport.relay_host}:{self._cfg.transport.relay_port}" if self._cfg else "",
            "sni": self._cfg.transport.sni if self._cfg else "",
            "metrics": m.summary(),
        }


# Singleton controller (attached by the main process)
_controller = TunnelController()


def get_controller() -> TunnelController:
    return _controller


# ── Request handlers ─────────────────────────────────────────────────────────

def _auth_required(handler):
    """Decorator: require Bearer token authentication."""
    async def wrapper(request: web.Request) -> web.Response:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        cfg_token = _controller._cfg.auth.token if _controller._cfg else ""
        if cfg_token and token != cfg_token:
            return _json_error("Unauthorized", 401)
        return await handler(request)
    return wrapper


async def handle_start(request: web.Request) -> web.Response:
    result = await _controller.start()
    if "error" in result:
        return _json_response(result, result.get("code", 500))
    await _broadcast_status()
    return _json_response(result)


async def handle_stop(request: web.Request) -> web.Response:
    result = await _controller.stop()
    if "error" in result:
        return _json_response(result, result.get("code", 500))
    await _broadcast_status()
    return _json_response(result)


async def handle_status(request: web.Request) -> web.Response:
    return _json_response(_controller.status())


async def handle_logs(request: web.Request) -> web.Response:
    limit = int(request.rel_url.query.get("limit", 100))
    logs = list(_LOG_BUFFER)[-limit:]
    return _json_response({"logs": logs, "total": len(_LOG_BUFFER)})


async def handle_config(request: web.Request) -> web.Response:
    """Update config at runtime (limited subset)."""
    try:
        body = await request.json()
    except Exception:
        return _json_error("Invalid JSON", 400)

    # Allow updating SNI and relay host at runtime
    if _controller._cfg:
        if "sni" in body:
            _controller._cfg.transport.sni = str(body["sni"])
        if "relay_host" in body:
            _controller._cfg.transport.relay_host = str(body["relay_host"])
        if "relay_port" in body:
            _controller._cfg.transport.relay_port = int(body["relay_port"])

    return _json_response({"status": "config_updated"})


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# ── WebSocket handlers ────────────────────────────────────────────────────────

async def handle_ws_status(request: web.Request) -> web.WebSocketResponse:
    """Send real-time status updates every second."""
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _WEBSOCKET_CLIENTS.add(ws)

    try:
        # Push status every second
        async def push_loop():
            while not ws.closed:
                await ws.send_json(_controller.status())
                await asyncio.sleep(1)

        push_task = asyncio.ensure_future(push_loop())

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # Clients can send "ping" to check connectivity
                if msg.data == "ping":
                    await ws.send_str("pong")
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        push_task.cancel()
        _WEBSOCKET_CLIENTS.discard(ws)

    return ws


async def handle_ws_logs(request: web.Request) -> web.WebSocketResponse:
    """Stream log entries to the client."""
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    # Send buffered logs first
    for entry in list(_LOG_BUFFER)[-50:]:
        if ws.closed:
            break
        await ws.send_json(entry)

    # Install a handler that pushes new entries to this ws
    class WsLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if not ws.closed:
                entry = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                }
                asyncio.ensure_future(ws.send_json(entry))

    handler = WsLogHandler()
    logging.getLogger().addHandler(handler)
    try:
        async for msg in ws:
            if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        logging.getLogger().removeHandler(handler)

    return ws


# ── Broadcast ─────────────────────────────────────────────────────────────────

async def _broadcast_status() -> None:
    """Push status to all connected WebSocket clients."""
    if not _WEBSOCKET_CLIENTS:
        return
    status = _controller.status()
    dead = set()
    for ws in _WEBSOCKET_CLIENTS:
        try:
            await ws.send_json(status)
        except Exception:
            dead.add(ws)
    _WEBSOCKET_CLIENTS -= dead


# ── App factory ───────────────────────────────────────────────────────────────

async def handle_dashboard(request: web.Request) -> web.Response:
    """Serve the real-time web dashboard."""
    import os
    dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    try:
        content = open(dashboard_path).read()
    except FileNotFoundError:
        content = "<h1>Dashboard not found</h1>"
    return web.Response(text=content, content_type="text/html")


def create_app(cfg: "HamiehConfig") -> web.Application:
    _install_log_buffer()
    app = web.Application()

    # Web dashboard
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/dashboard", handle_dashboard)

    # REST routes
    app.router.add_post("/api/tunnel/start", _auth_required(handle_start))
    app.router.add_post("/api/tunnel/stop", _auth_required(handle_stop))
    app.router.add_get("/api/tunnel/status", handle_status)
    app.router.add_get("/api/tunnel/logs", _auth_required(handle_logs))
    app.router.add_post("/api/config", _auth_required(handle_config))
    app.router.add_get("/api/health", handle_health)

    # WebSocket routes
    app.router.add_get("/ws/status", handle_ws_status)
    app.router.add_get("/ws/logs", _auth_required(handle_ws_logs))

    return app


async def serve_mobile_api(
    cfg: HamiehConfig,
    bind_host: str = "127.0.0.1",
    bind_port: int = 8080,
) -> None:
    """Start the mobile API server."""
    app = create_app(cfg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, bind_host, bind_port)
    await site.start()
    logger.info("Mobile API listening on http://%s:%d", bind_host, bind_port)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data),
        status=status,
        content_type="application/json",
    )


def _json_error(message: str, status: int = 400) -> web.Response:
    return _json_response({"error": message, "code": status}, status)
