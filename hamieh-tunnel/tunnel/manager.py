"""
Tunnel Manager: connection pooling, multi-relay load balancing,
and automatic tunnel rotation.

The manager owns N Transport instances (one per relay endpoint) and
distributes open_stream() calls across them using a round-robin policy
with health-check-based fallback.

Rotation:
  When enabled, the manager periodically switches to the next relay in
  the pool, which:
  - Prevents long-lived connections that are easier to fingerprint
  - Distributes load across relays
  - Provides resilience against relay-level blocking
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from core.config import HamiehConfig, TransportConfig, RotationConfig
from core.metrics import get_metrics
from .transport.base import Transport, TransportConnection

logger = logging.getLogger(__name__)


@dataclass
class RelayEndpoint:
    host: str
    port: int
    healthy: bool = True
    last_failure: float = 0.0
    failure_count: int = 0
    bytes_forwarded: int = 0


def _build_transport(cfg: TransportConfig) -> Transport:
    """Factory: instantiate the correct Transport based on config type."""
    t = cfg.type.lower()
    if t == "wss":
        from .transport.wss import WebSocketTransport
        return WebSocketTransport(cfg)
    elif t in ("https", "http"):
        from .transport.https_fallback import HttpsTransport
        return HttpsTransport(cfg)
    elif t == "auto":
        from .transport.https_fallback import AutoTransport
        return AutoTransport(cfg)
    elif t == "azure_dev":
        from .transport.azure_dev import AzureDevTunnelTransport
        return AzureDevTunnelTransport(cfg)
    else:
        raise ValueError(f"Unknown transport type: {t!r}")


class TunnelManager:
    """
    Manages a pool of transports and provides open_stream().
    Handles load balancing, health checking, and automatic rotation.
    """

    def __init__(self, cfg: HamiehConfig) -> None:
        self._cfg = cfg
        self._transports: list[Transport] = []
        self._endpoints: list[RelayEndpoint] = []
        self._rr_index: int = 0
        self._rotation_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Build transports for the primary relay + any rotation pool relays."""
        self._running = True
        metrics = get_metrics()

        # Primary relay
        primary = RelayEndpoint(
            self._cfg.transport.relay_host,
            self._cfg.transport.relay_port,
        )
        self._endpoints.append(primary)
        transport = _build_transport(self._cfg.transport)
        await transport.start()
        self._transports.append(transport)

        # Additional relays from rotation pool
        if self._cfg.rotation.enabled and self._cfg.rotation.relay_pool:
            for addr in self._cfg.rotation.relay_pool:
                host, _, port_str = addr.rpartition(":")
                port = int(port_str) if port_str else self._cfg.transport.relay_port
                ep = RelayEndpoint(host, port)
                self._endpoints.append(ep)

                from dataclasses import replace
                extra_cfg = replace(
                    self._cfg.transport,
                    relay_host=host,
                    relay_port=port,
                )
                extra_transport = _build_transport(extra_cfg)
                try:
                    await extra_transport.start()
                    self._transports.append(extra_transport)
                    logger.info("Extra relay loaded: %s:%d", host, port)
                except Exception as e:
                    logger.warning("Extra relay %s:%d unavailable: %s", host, port, e)
                    ep.healthy = False

        metrics.tunnel.active_tunnels = len(self._transports)

        # Start rotation task if enabled
        if self._cfg.rotation.enabled:
            self._rotation_task = asyncio.ensure_future(self._rotation_loop())
            logger.info(
                "Tunnel rotation enabled (interval=%ds)",
                self._cfg.rotation.interval_seconds,
            )

        logger.info(
            "TunnelManager ready with %d transport(s)", len(self._transports)
        )

    async def stop(self) -> None:
        self._running = False
        if self._rotation_task:
            self._rotation_task.cancel()
        for t in self._transports:
            try:
                await t.stop()
            except Exception:
                pass
        logger.info("TunnelManager stopped")

    async def open_stream(self, dst_host: str, dst_port: int) -> TransportConnection:
        """
        Open a tunneled stream using the least-recently-used healthy transport.
        Falls back to the next transport if the current one fails.
        """
        if not self._transports:
            raise ConnectionError("No transports available")

        n = len(self._transports)
        for attempt in range(n):
            idx = (self._rr_index + attempt) % n
            transport = self._transports[idx]
            ep = self._endpoints[idx] if idx < len(self._endpoints) else None

            if ep and not ep.healthy:
                # Skip unhealthy endpoints (will retry after backoff)
                continue

            try:
                conn = await transport.open_stream(dst_host, dst_port)
                self._rr_index = (idx + 1) % n
                get_metrics().conn_opened()
                return conn
            except ConnectionError as e:
                logger.warning("Transport[%d] failed: %s", idx, e)
                if ep:
                    ep.healthy = False
                    ep.last_failure = time.time()
                    ep.failure_count += 1
                get_metrics().conn_failed()
                continue

        raise ConnectionError(
            f"All {n} transports failed to connect to {dst_host}:{dst_port}"
        )

    async def _rotation_loop(self) -> None:
        """Periodically rotate to the next relay in the pool."""
        interval = self._cfg.rotation.interval_seconds
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break

            self._rr_index = (self._rr_index + 1) % max(len(self._transports), 1)
            get_metrics().tunnel.rotations += 1
            logger.info(
                "Tunnel rotated to transport[%d] (%s)",
                self._rr_index,
                self._transports[self._rr_index].name
                if self._rr_index < len(self._transports)
                else "none",
            )

            # Periodically recover unhealthy endpoints
            now = time.time()
            for i, ep in enumerate(self._endpoints):
                if not ep.healthy and (now - ep.last_failure) > 60:
                    ep.healthy = True
                    logger.info("Endpoint %s:%d marked healthy again", ep.host, ep.port)
