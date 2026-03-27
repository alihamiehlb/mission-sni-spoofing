"""
Azure Dev Tunnel transport module (optional).

Wraps the original Azure Dev Tunnel approach as a pluggable transport
so it can be swapped in/out alongside WSS and HTTPS.

Azure Dev Tunnels provide:
- Free relay infrastructure (no VPS required)
- Automatic TLS with valid Microsoft cert (so carrier DPI sees legitimate Teams cert)
- Built-in authentication via Azure AD

Requirements:
  - Azure CLI installed and logged in
  - devtunnel CLI (installed by this module if missing)
  - Active Azure subscription

Usage in config.yaml:
  transport:
    type: azure_dev
    azure_tunnel_url: ""  # auto-created if empty
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
from typing import Optional

from core.config import TransportConfig
from .base import Transport, TransportConnection
from .https_fallback import HttpsTransport

logger = logging.getLogger(__name__)

_DEVTUNNEL_INSTALL_URL = "https://aka.ms/TunnelsCliDownload/linux-x64"


class AzureDevTunnelTransport(Transport):
    """
    Uses Azure Dev Tunnel as the outer transport.

    Under the hood:
    1. Creates/reuses a dev tunnel pointing to the relay server
    2. Obtains the tunnel's public HTTPS URL (*.devtunnels.ms)
    3. Configures an HTTPS transport pointing at that URL
    4. The TLS cert from *.devtunnels.ms is valid + Microsoft-issued
       so carrier DPI classifies traffic as Microsoft traffic

    This is the "original" approach from the source repo, preserved as
    a first-class transport module.
    """

    def __init__(self, cfg: TransportConfig, local_relay_port: int = 8443) -> None:
        self._cfg = cfg
        self._local_relay_port = local_relay_port
        self._tunnel_url: str = cfg.azure_tunnel_url
        self._inner: Optional[HttpsTransport] = None
        self._tunnel_proc: Optional[asyncio.subprocess.Process] = None

    @property
    def name(self) -> str:
        return f"AzureDevTunnel→{self._tunnel_url or 'pending'}"

    async def start(self) -> None:
        if not self._tunnel_url:
            self._tunnel_url = await self._create_dev_tunnel()

        # Create an HTTPS transport pointing at the dev tunnel URL
        from dataclasses import replace
        inner_cfg = replace(
            self._cfg,
            relay_host=self._tunnel_url,
            relay_port=443,
            sni=self._tunnel_url,         # use the real Microsoft-issued cert
            verify_relay_cert=True,        # valid cert — we can verify it
        )
        self._inner = HttpsTransport(inner_cfg)
        await self._inner.start()
        logger.info("Azure Dev Tunnel transport ready: %s", self._tunnel_url)

    async def stop(self) -> None:
        if self._inner:
            await self._inner.stop()
        if self._tunnel_proc:
            self._tunnel_proc.terminate()
            await self._tunnel_proc.wait()

    async def open_stream(self, dst_host: str, dst_port: int) -> TransportConnection:
        if not self._inner:
            raise ConnectionError("AzureDevTunnel transport not started")
        return await self._inner.open_stream(dst_host, dst_port)

    async def _create_dev_tunnel(self) -> str:
        """Create an Azure Dev Tunnel and return its public URL."""
        _ensure_devtunnel_cli()

        logger.info("Creating Azure Dev Tunnel (requires az login)...")
        proc = await asyncio.create_subprocess_exec(
            "devtunnel", "host", "--port", str(self._local_relay_port),
            "--protocol", "https", "--allow-anonymous",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._tunnel_proc = proc

        # Parse the tunnel URL from devtunnel output
        tunnel_url = ""
        assert proc.stdout is not None
        async for line in proc.stdout:
            decoded = line.decode().strip()
            logger.debug("devtunnel: %s", decoded)
            if "devtunnels.ms" in decoded:
                for part in decoded.split():
                    if "devtunnels.ms" in part:
                        tunnel_url = part.strip("()")
                        break
            if tunnel_url:
                break

        if not tunnel_url:
            raise RuntimeError(
                "Failed to obtain Azure Dev Tunnel URL. "
                "Make sure you are logged in: az login"
            )

        logger.info("Azure Dev Tunnel URL: %s", tunnel_url)
        return tunnel_url


def _ensure_devtunnel_cli() -> None:
    """Install devtunnel CLI if not present (Linux only)."""
    if shutil.which("devtunnel"):
        return

    logger.info("Installing devtunnel CLI...")
    subprocess.run(
        ["bash", "-c", f"curl -sL {_DEVTUNNEL_INSTALL_URL} | sudo install /dev/stdin /usr/local/bin/devtunnel"],
        check=True,
    )
