"""
Nexus Wire Protocol — inner framing that runs inside TLS/WSS.

All transport implementations use this same protocol so the relay server
is transport-agnostic.

Frame format (binary, big-endian):

  OPEN stream:
    [1B type=0x01] [1B host_len] [host_len B hostname] [2B port] [token_len_prefix + token]

  DATA frame:
    [1B type=0x02] [4B stream_id] [4B payload_len] [payload_len B data]

  CLOSE stream:
    [1B type=0x03] [4B stream_id]

  STATUS reply (server → client):
    [1B type=0x10] [1B status]  (0x00=OK, 0x01=auth_fail, 0x02=connect_fail)

For the simple (non-multiplexed) mode used by WebSocket and HTTPS transports,
each physical connection carries exactly one stream so stream_id is always 0.
This keeps the protocol trivially simple while leaving room for multiplexing later.
"""

import struct


FRAME_OPEN = 0x01
FRAME_DATA = 0x02
FRAME_CLOSE = 0x03
FRAME_STATUS = 0x10

STATUS_OK = 0x00
STATUS_AUTH_FAIL = 0x01
STATUS_CONNECT_FAIL = 0x02
STATUS_RELAY_ERROR = 0x03


def encode_open(dst_host: str, dst_port: int, token: str = "") -> bytes:
    """Encode an OPEN frame asking the relay to connect to dst_host:dst_port."""
    host_b = dst_host.encode()
    token_b = token.encode()
    return (
        bytes([FRAME_OPEN, len(host_b)])
        + host_b
        + struct.pack("!H", dst_port)
        + struct.pack("!H", len(token_b))
        + token_b
    )


async def read_status(reader) -> int:
    """Read a STATUS reply byte from the relay."""
    data = await reader.readexactly(2)
    if data[0] != FRAME_STATUS:
        raise ValueError(f"Expected STATUS frame (0x10), got 0x{data[0]:02x}")
    return data[1]


def encode_status(status: int) -> bytes:
    return bytes([FRAME_STATUS, status])


async def read_open(reader) -> tuple[str, int, str]:
    """Parse an incoming OPEN frame on the server side. Returns (host, port, token)."""
    frame_type = (await reader.readexactly(1))[0]
    if frame_type != FRAME_OPEN:
        raise ValueError(f"Expected OPEN frame, got 0x{frame_type:02x}")

    host_len = (await reader.readexactly(1))[0]
    dst_host = (await reader.readexactly(host_len)).decode()
    dst_port = struct.unpack("!H", await reader.readexactly(2))[0]
    token_len = struct.unpack("!H", await reader.readexactly(2))[0]
    token = (await reader.readexactly(token_len)).decode() if token_len else ""

    return dst_host, dst_port, token
