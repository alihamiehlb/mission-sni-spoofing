"""
Live metrics and bandwidth accounting for Hamieh Tunnel.

Exposes a Prometheus /metrics endpoint and provides an in-process
registry that all subsystems update directly.
"""

import asyncio
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import MetricsConfig


# ---------------------------------------------------------------------------
# In-process metrics registry (no Prometheus dependency required)
# ---------------------------------------------------------------------------

@dataclass
class ConnectionMetrics:
    total_opened: int = 0
    total_closed: int = 0
    active: int = 0
    failed: int = 0


@dataclass
class BandwidthMetrics:
    bytes_sent: int = 0
    bytes_recv: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def add_sent(self, n: int) -> None:
        with self._lock:
            self.bytes_sent += n

    def add_recv(self, n: int) -> None:
        with self._lock:
            self.bytes_recv += n

    @property
    def mb_sent(self) -> float:
        return self.bytes_sent / (1024 * 1024)

    @property
    def mb_recv(self) -> float:
        return self.bytes_recv / (1024 * 1024)


@dataclass
class TunnelMetrics:
    rotations: int = 0
    active_tunnels: int = 0
    transport_errors: int = 0


class HamiehMetrics:
    """Central metrics store. Thread-safe for use from asyncio + threads."""

    def __init__(self) -> None:
        self.start_time = time.time()
        self.connections = ConnectionMetrics()
        self.bandwidth = BandwidthMetrics()
        self.tunnel = TunnelMetrics()
        self._lock = Lock()

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.start_time

    def conn_opened(self) -> None:
        with self._lock:
            self.connections.total_opened += 1
            self.connections.active += 1

    def conn_closed(self) -> None:
        with self._lock:
            self.connections.total_closed += 1
            self.connections.active = max(0, self.connections.active - 1)

    def conn_failed(self) -> None:
        with self._lock:
            self.connections.failed += 1

    def summary(self) -> dict:
        return {
            "uptime_s": round(self.uptime_seconds, 1),
            "connections": {
                "active": self.connections.active,
                "total": self.connections.total_opened,
                "failed": self.connections.failed,
            },
            "bandwidth": {
                "sent_mb": round(self.bandwidth.mb_sent, 3),
                "recv_mb": round(self.bandwidth.mb_recv, 3),
            },
            "tunnel": {
                "active": self.tunnel.active_tunnels,
                "rotations": self.tunnel.rotations,
                "errors": self.tunnel.transport_errors,
            },
        }


# Singleton
_metrics = HamiehMetrics()


def get_metrics() -> HamiehMetrics:
    return _metrics


# ---------------------------------------------------------------------------
# Optional Prometheus HTTP server
# ---------------------------------------------------------------------------

async def serve_metrics(cfg: "MetricsConfig") -> None:
    """Serve Prometheus-compatible /metrics over HTTP."""
    if not cfg.enabled:
        return

    try:
        from aiohttp import web
    except ImportError:
        return

    async def handle_metrics(request: web.Request) -> web.Response:  # noqa: ARG001
        m = get_metrics()
        lines = [
            "# HELP nexus_uptime_seconds Tunnel uptime in seconds",
            "# TYPE nexus_uptime_seconds gauge",
            f"nexus_uptime_seconds {m.uptime_seconds:.1f}",
            "",
            "# HELP nexus_connections_active Active connections",
            "# TYPE nexus_connections_active gauge",
            f"nexus_connections_active {m.connections.active}",
            "",
            "# HELP nexus_connections_total Total connections opened",
            "# TYPE nexus_connections_total counter",
            f"nexus_connections_total {m.connections.total_opened}",
            "",
            "# HELP nexus_bytes_sent_total Bytes sent through tunnel",
            "# TYPE nexus_bytes_sent_total counter",
            f"nexus_bytes_sent_total {m.bandwidth.bytes_sent}",
            "",
            "# HELP nexus_bytes_recv_total Bytes received through tunnel",
            "# TYPE nexus_bytes_recv_total counter",
            f"nexus_bytes_recv_total {m.bandwidth.bytes_recv}",
        ]
        return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")

    app = web.Application()
    app.router.add_get("/metrics", handle_metrics)
    app.router.add_get("/health", lambda _: web.Response(text="ok"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.bind_host, cfg.bind_port)
    await site.start()
