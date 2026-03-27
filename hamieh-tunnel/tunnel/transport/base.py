"""
Abstract transport interface.

A Transport wraps the underlying network protocol (WSS, HTTPS, etc.)
and provides a uniform `open_stream(dst_host, dst_port)` interface
that the SOCKS5 proxy uses to forward connections.
"""

import abc
import asyncio
from dataclasses import dataclass


@dataclass
class TransportConnection:
    """
    A single logical stream multiplexed over the transport.
    Wraps asyncio StreamReader/Writer so callers can use familiar I/O.
    """
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    dst_host: str
    dst_port: int

    async def close(self) -> None:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except OSError:
            pass


class Transport(abc.ABC):
    """
    Abstract base for all transport implementations.

    Transports handle the outer protocol (WSS, HTTPS) and authentication.
    They do not know about SOCKS5 or routing.
    """

    @abc.abstractmethod
    async def start(self) -> None:
        """Initialise the transport (connect pool, authenticate, etc.)."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Shut down cleanly."""

    @abc.abstractmethod
    async def open_stream(
        self, dst_host: str, dst_port: int
    ) -> TransportConnection:
        """
        Open a tunneled stream to `dst_host:dst_port`.

        Raises `ConnectionError` on failure.
        """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Short human-readable name for logging."""

    @property
    def is_ready(self) -> bool:
        """Return True if the transport is up and can accept new streams."""
        return True
