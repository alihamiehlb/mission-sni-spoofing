"""
TUN interface + tun2socks integration for full traffic capture.

This module provides an alternative to the SOCKS5 proxy for capturing
ALL traffic from the system (including non-proxy-aware apps).

Architecture:
  1. Create a TUN device (e.g. nexus0) with a fake gateway IP
  2. Add default route via that gateway
  3. Launch tun2socks which reads raw IP packets from the TUN device
     and forwards them as SOCKS5 connections to our SOCKS5 proxy
  4. iptables rules redirect traffic to the TUN device

Android equivalent:
  VpnService.Builder creates a TUN descriptor; tun2socks runs as a library.

Requires:
  - Linux with TUN/TAP kernel module
  - tun2socks binary in PATH or configured path
  - Root / CAP_NET_ADMIN capability
"""

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from core.config import TunConfig, Socks5Config

logger = logging.getLogger(__name__)


class TunManager:
    """
    Manages the TUN device, iptables rules, and tun2socks process.
    Designed for Linux; Android support uses VpnService instead.
    """

    def __init__(self, tun_cfg: TunConfig, socks5_cfg: Socks5Config) -> None:
        self._cfg = tun_cfg
        self._socks5 = socks5_cfg
        self._tun2socks_proc: Optional[asyncio.subprocess.Process] = None
        self._original_routes: list[str] = []
        self._iptables_rules: list[list[str]] = []

    async def start(self) -> None:
        """Set up TUN device, routes, and tun2socks."""
        if os.geteuid() != 0:
            raise PermissionError("TUN interface setup requires root privileges")

        self._check_dependencies()

        logger.info("Setting up TUN device: %s", self._cfg.name)
        await self._create_tun_device()
        await self._configure_routes()
        await self._setup_iptables()
        await self._start_tun2socks()
        logger.info("TUN interface %s active — all traffic captured", self._cfg.name)

    async def stop(self) -> None:
        """Tear down tun2socks, routes, and TUN device."""
        await self._stop_tun2socks()
        await self._remove_iptables()
        await self._restore_routes()
        await self._remove_tun_device()
        logger.info("TUN interface %s removed", self._cfg.name)

    # ── TUN device ────────────────────────────────────────────────────────

    async def _create_tun_device(self) -> None:
        await _run(["ip", "tuntap", "add", "dev", self._cfg.name, "mode", "tun"])
        await _run(["ip", "addr", "add",
                    f"{self._cfg.address}/{_mask_to_prefix(self._cfg.netmask)}",
                    "dev", self._cfg.name])
        await _run(["ip", "link", "set", "dev", self._cfg.name, "mtu", str(self._cfg.mtu)])
        await _run(["ip", "link", "set", "dev", self._cfg.name, "up"])

    async def _remove_tun_device(self) -> None:
        try:
            await _run(["ip", "link", "set", "dev", self._cfg.name, "down"])
            await _run(["ip", "tuntap", "del", "dev", self._cfg.name, "mode", "tun"])
        except Exception as e:
            logger.warning("Failed to remove TUN device: %s", e)

    # ── Routing ───────────────────────────────────────────────────────────

    async def _configure_routes(self) -> None:
        """
        Route all traffic through the TUN device EXCEPT traffic going to
        the relay server itself (which must use the real interface).
        """
        # Save current default gateway for relay traffic
        try:
            result = await asyncio.create_subprocess_shell(
                "ip route show default",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await result.communicate()
            self._original_routes = [stdout.decode().strip()]
        except Exception:
            pass

        # Route all traffic via TUN
        await _run(["ip", "route", "add", "0.0.0.0/1", "dev", self._cfg.name])
        await _run(["ip", "route", "add", "128.0.0.0/1", "dev", self._cfg.name])

    async def _restore_routes(self) -> None:
        try:
            await _run(["ip", "route", "del", "0.0.0.0/1"])
            await _run(["ip", "route", "del", "128.0.0.0/1"])
        except Exception as e:
            logger.warning("Failed to restore routes: %s", e)

    # ── iptables ──────────────────────────────────────────────────────────

    async def _setup_iptables(self) -> None:
        """
        Redirect all TCP/UDP to go through the TUN device.
        Exclude traffic destined for the SOCKS5 proxy itself
        and loopback traffic.
        """
        socks_port = str(self._socks5.bind_port)
        rules = [
            # Don't redirect SOCKS5 proxy traffic itself
            ["iptables", "-t", "nat", "-A", "OUTPUT",
             "-p", "tcp", "--dport", socks_port, "-j", "RETURN"],
            # Don't redirect loopback
            ["iptables", "-t", "nat", "-A", "OUTPUT",
             "-o", "lo", "-j", "RETURN"],
            # Redirect all TCP through TUN
            ["iptables", "-t", "nat", "-A", "OUTPUT",
             "-p", "tcp", "-j", "REDIRECT", "--to-ports", socks_port],
        ]
        for rule in rules:
            await _run(rule)
            self._iptables_rules.append(rule)

    async def _remove_iptables(self) -> None:
        for rule in reversed(self._iptables_rules):
            delete_rule = [rule[0]] + ["-D" if a == "-A" else a for a in rule[1:]]
            try:
                await _run(delete_rule)
            except Exception as e:
                logger.warning("Failed to remove iptables rule: %s", e)

    # ── tun2socks ─────────────────────────────────────────────────────────

    async def _start_tun2socks(self) -> None:
        """
        Launch tun2socks to bridge the TUN device to our SOCKS5 proxy.

        tun2socks reads raw IP packets from the TUN device, wraps them
        as SOCKS5 connections, and sends them to our proxy.
        """
        bin_path = shutil.which(self._cfg.tun2socks_bin) or self._cfg.tun2socks_bin
        if not bin_path or not Path(bin_path).exists():
            raise FileNotFoundError(
                f"tun2socks binary not found: {self._cfg.tun2socks_bin}\n"
                f"Install: https://github.com/xjasonlyu/tun2socks/releases"
            )

        socks5_url = f"socks5://{self._socks5.bind_host}:{self._socks5.bind_port}"

        cmd = [
            bin_path,
            "-device", f"tun://{self._cfg.name}",
            "-proxy", socks5_url,
            "-loglevel", "warning",
        ]

        self._tun2socks_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("tun2socks started (pid=%d)", self._tun2socks_proc.pid)

        # Give it a moment to initialize
        await asyncio.sleep(0.5)
        if self._tun2socks_proc.returncode is not None:
            stderr = await self._tun2socks_proc.stderr.read()
            raise RuntimeError(f"tun2socks exited early: {stderr.decode()}")

    async def _stop_tun2socks(self) -> None:
        if self._tun2socks_proc and self._tun2socks_proc.returncode is None:
            self._tun2socks_proc.terminate()
            try:
                await asyncio.wait_for(self._tun2socks_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._tun2socks_proc.kill()

    # ── Utils ─────────────────────────────────────────────────────────────

    def _check_dependencies(self) -> None:
        missing = []
        for cmd in ["ip", "iptables"]:
            if not shutil.which(cmd):
                missing.append(cmd)
        if missing:
            raise RuntimeError(f"Missing required tools: {', '.join(missing)}")


# ── Subprocess helper ────────────────────────────────────────────────────────

async def _run(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Command {cmd} failed: {stderr.decode().strip()}")


def _mask_to_prefix(netmask: str) -> int:
    """Convert "255.255.255.0" to 24."""
    import socket
    import struct
    packed = socket.inet_aton(netmask)
    bits = struct.unpack(">I", packed)[0]
    return bin(bits).count("1")


# ── Android architecture note ────────────────────────────────────────────────

ANDROID_ARCHITECTURE_NOTE = """
Android VpnService Integration
================================
On Android, the OS provides VpnService which creates a TUN file descriptor
without root. The architecture mirrors the Linux setup:

  1. VpnService.Builder
       .addAddress("10.88.0.1", 24)
       .addRoute("0.0.0.0", 0)           // capture all traffic
       .addDnsServer("1.1.1.1")
       .establish()                       // returns ParcelFileDescriptor (TUN fd)

  2. tun2socks (Go library, compiled for Android via gomobile)
       - Reads raw packets from the TUN fd
       - Creates SOCKS5 connections to hamieh SOCKS5 server running in-process
       - The hamieh client forwards them through WSS/HTTPS to the relay

  3. NexusClient (Kotlin/Flutter bridge)
       - Starts the SOCKS5 server in-process (Python via Chaquopy, or Go)
       - Connects to the relay using WSS with configurable SNI
       - Exposes status via the mobile REST API

Flutter integration:
  - Use flutter_background_service for persistent VPN operation
  - flutter_local_notifications for connection status
  - Communicate with the Nexus mobile API on 127.0.0.1:8080
"""
