"""
Traffic obfuscation layer.

Wraps a TransportConnection and adds:
  1. Random padding per write (disguises payload size patterns)
  2. Timing jitter (disguises inter-packet timing patterns)

Both carrier-level and network-flow analysis rely on traffic fingerprinting.
Padding + jitter make it significantly harder to identify the protocol
from side-channel measurements.

Padding format (prepended to every payload written through this layer):
  [2B pad_len BE][pad_len random bytes][original payload]

The relay strips padding before forwarding. Padding is symmetric: both
client-to-relay and relay-to-client traffic is padded.
"""

import asyncio
import logging
import random
import secrets
import struct
from typing import Optional

from .transport.base import TransportConnection

logger = logging.getLogger(__name__)


class ObfuscatedConnection:
    """
    Wraps a TransportConnection with padding and timing jitter.
    Implements the same reader/writer interface so it's a drop-in replacement.
    """

    HEADER_SIZE = 2  # 2 bytes for pad_len

    def __init__(
        self,
        inner: TransportConnection,
        padding_min: int = 16,
        padding_max: int = 256,
        jitter_ms: int = 20,
    ) -> None:
        self._inner = inner
        self._padding_min = padding_min
        self._padding_max = padding_max
        self._jitter_ms = jitter_ms
        # Obfuscated reader strips padding before delivering data
        self._reader = asyncio.StreamReader()
        self._read_task: Optional[asyncio.Task] = None

    @property
    def reader(self) -> asyncio.StreamReader:
        return self._reader

    @property
    def writer(self) -> "_ObfuscatedWriter":
        return _ObfuscatedWriter(self)

    def start(self) -> None:
        self._read_task = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self) -> None:
        """Strip padding from incoming data and feed clean data to our reader."""
        inner_reader = self._inner.reader
        try:
            while True:
                # Read header: 2B pad_len
                header = await inner_reader.readexactly(self.HEADER_SIZE)
                pad_len = struct.unpack("!H", header)[0]
                # Discard padding
                if pad_len > 0:
                    await inner_reader.readexactly(pad_len)
                # Read actual payload length (4B)
                payload_header = await inner_reader.readexactly(4)
                payload_len = struct.unpack("!I", payload_header)[0]
                if payload_len == 0:
                    continue
                payload = await inner_reader.readexactly(payload_len)
                self._reader.feed_data(payload)
        except asyncio.IncompleteReadError:
            self._reader.feed_eof()
        except Exception as e:
            logger.debug("Obfuscated read loop ended: %s", e)
            self._reader.feed_eof()

    async def write(self, data: bytes) -> None:
        """Add padding + optional jitter, then write."""
        if self._jitter_ms > 0:
            delay = random.uniform(0, self._jitter_ms / 1000)
            await asyncio.sleep(delay)

        pad_len = random.randint(self._padding_min, self._padding_max)
        padding = secrets.token_bytes(pad_len)
        payload_len = len(data)

        frame = (
            struct.pack("!H", pad_len)
            + padding
            + struct.pack("!I", payload_len)
            + data
        )

        self._inner.writer.write(frame)
        await self._inner.writer.drain()

    async def close(self) -> None:
        if self._read_task:
            self._read_task.cancel()
        await self._inner.close()


class _ObfuscatedWriter:
    """Minimal StreamWriter-compatible adapter for ObfuscatedConnection."""

    def __init__(self, conn: ObfuscatedConnection) -> None:
        self._conn = conn
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        self._buf.extend(data)

    async def drain(self) -> None:
        if self._buf:
            await self._conn.write(bytes(self._buf))
            self._buf.clear()

    def close(self) -> None:
        asyncio.ensure_future(self._conn.close())

    async def wait_closed(self) -> None:
        await self._conn.close()

    def get_extra_info(self, key: str, default=None):  # noqa: ANN001
        return default
