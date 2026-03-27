"""
HTTPS fallback transport.

When WebSocket is blocked by a firewall/middlebox, this transport uses
plain HTTPS CONNECT (like a traditional HTTP proxy) to tunnel traffic.

The relay server handles HTTPS CONNECT upgrade requests and switches
the connection to the Nexus protocol after the 200 reply.

Traffic looks like HTTPS browsing to any DPI that doesn't perform
deep inspection — just a stream of TLS records to a well-known port.
"""

import asyncio
import logging
import ssl
from typing import Optional

from core.config import TransportConfig
from core.crypto import client_ssl_context
from core.metrics import get_metrics
from .base import Transport, TransportConnection
from .protocol import encode_open, STATUS_OK

logger = logging.getLogger(__name__)

# HTTP CONNECT response prefix
_CONNECT_OK = b"HTTP/1.1 200"


class HttpsTransport(Transport):
    """
    HTTPS CONNECT-based fallback transport.

    Establishes a TLS connection to the relay, sends an HTTP CONNECT
    request to negotiate the tunnel, then speaks the Nexus protocol
    over the resulting raw stream.
    """

    def __init__(self, cfg: TransportConfig) -> None:
        self._cfg = cfg
        self._ssl_ctx: Optional[ssl.SSLContext] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._running = False

    @property
    def name(self) -> str:
        return (
            f"HTTPS→{self._cfg.relay_host}:{self._cfg.relay_port} "
            f"(SNI={self._cfg.sni})"
        )

    async def start(self) -> None:
        self._ssl_ctx = client_ssl_context(
            sni_override=self._cfg.sni,
            verify=self._cfg.verify_relay_cert,
            ca_file=self._cfg.tls.ca_file,
        )
        self._semaphore = asyncio.Semaphore(self._cfg.pool.max_connections)
        self._running = True
        logger.info("HTTPS fallback transport ready: %s", self.name)

    async def stop(self) -> None:
        self._running = False

    async def open_stream(self, dst_host: str, dst_port: int) -> TransportConnection:
        if not self._running:
            raise ConnectionError("HTTPS transport not running")

        async with self._semaphore:
            return await self._connect(dst_host, dst_port)

    async def _connect(self, dst_host: str, dst_port: int) -> TransportConnection:
        metrics = get_metrics()

        for attempt in range(self._cfg.pool.max_retries):
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        self._cfg.relay_host,
                        self._cfg.relay_port,
                        ssl=self._ssl_ctx,
                        server_hostname=self._cfg.sni,
                    ),
                    timeout=self._cfg.pool.connect_timeout,
                )

                # HTTP CONNECT handshake — relay upgrades to raw TCP relay
                connect_req = (
                    f"CONNECT {dst_host}:{dst_port} HTTP/1.1\r\n"
                    f"Host: {dst_host}:{dst_port}\r\n"
                    f"Proxy-Authorization: Bearer {self._cfg.tls.sni_override or ''}\r\n"
                    f"X-Nexus-Version: 1\r\n"
                    f"\r\n"
                ).encode()
                writer.write(connect_req)
                await writer.drain()

                # Read HTTP response line
                response_line = await asyncio.wait_for(
                    reader.readline(), timeout=self._cfg.pool.connect_timeout
                )
                if not response_line.startswith(_CONNECT_OK):
                    writer.close()
                    raise ConnectionError(
                        f"HTTPS CONNECT failed: {response_line.decode().strip()}"
                    )

                # Drain remaining headers
                while True:
                    line = await reader.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break

                logger.debug("HTTPS stream opened to %s:%d", dst_host, dst_port)
                return TransportConnection(
                    reader=reader,
                    writer=writer,
                    dst_host=dst_host,
                    dst_port=dst_port,
                )

            except (OSError, asyncio.TimeoutError) as e:
                metrics.tunnel.transport_errors += 1
                if attempt < self._cfg.pool.max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise ConnectionError(
                    f"HTTPS connect failed after {attempt + 1} attempts: {e}"
                ) from e

        raise ConnectionError("HTTPS open_stream: exhausted retries")


class AutoTransport(Transport):
    """
    Tries WSS first; falls back to HTTPS if WSS fails.
    Remembers which transport worked last and sticks with it.
    """

    def __init__(self, cfg: TransportConfig) -> None:
        from .wss import WebSocketTransport
        self._wss = WebSocketTransport(cfg)
        self._https = HttpsTransport(cfg)
        self._active: Optional[Transport] = None

    @property
    def name(self) -> str:
        return f"Auto({self._active.name if self._active else 'none'})"

    async def start(self) -> None:
        # Try WSS first
        try:
            await self._wss.start()
            self._active = self._wss
            logger.info("AutoTransport: using WSS")
            return
        except Exception as e:
            logger.warning("WSS unavailable (%s), trying HTTPS fallback", e)

        await self._https.start()
        self._active = self._https
        logger.info("AutoTransport: using HTTPS fallback")

    async def stop(self) -> None:
        if self._active:
            await self._active.stop()

    async def open_stream(self, dst_host: str, dst_port: int) -> TransportConnection:
        if not self._active:
            raise ConnectionError("AutoTransport not started")

        try:
            return await self._active.open_stream(dst_host, dst_port)
        except ConnectionError:
            # Flip to the other transport
            if self._active is self._wss:
                logger.warning("WSS failed, switching to HTTPS fallback")
                await self._https.start()
                self._active = self._https
            else:
                logger.warning("HTTPS fallback failed, switching back to WSS")
                await self._wss.start()
                self._active = self._wss
            return await self._active.open_stream(dst_host, dst_port)
