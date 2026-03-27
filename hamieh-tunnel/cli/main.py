"""
Hamieh Tunnel CLI

Commands:
  hamieh start     [--config FILE] [--tun] [--api]
  hamieh stop
  hamieh status
  hamieh logs      [--follow]
  hamieh server    [--config FILE]
  hamieh keygen    Generate a new auth token
  hamieh cert      Generate TLS certificates

All commands support --config to specify the YAML config file.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

DEFAULT_CONFIG = "config/default.yaml"
API_BASE = "http://127.0.0.1:8080"


# ── Helpers ─────────────────────────────────────────────────────────────────

def _load_config(config_path: Optional[str]) -> "HamiehConfig":  # noqa: F821
    from core.config import load_config
    return load_config(config_path or (DEFAULT_CONFIG if Path(DEFAULT_CONFIG).exists() else None))


def _api_request(method: str, path: str, token: str = "", **kwargs):
    """Simple synchronous HTTP call to the local mobile API."""
    import urllib.request
    import urllib.error

    url = f"{API_BASE}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = None
    if kwargs.get("json"):
        body = json.dumps(kwargs["json"]).encode()

    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.reason, "code": e.code}
    except ConnectionRefusedError:
        return {"error": "hamieh not running (API not reachable)", "code": 503}


# ── Commands ─────────────────────────────────────────────────────────────────

@click.group()
@click.version_option("1.0.0", prog_name="hamieh")
def cli():
    """Hamieh Tunnel — SOCKS5 + TLS tunneling system."""
    pass


@cli.command()
@click.option("--config", "-c", default=None, help="Config YAML file")
@click.option("--tun", is_flag=True, default=False, help="Enable TUN interface (requires root)")
@click.option("--api", is_flag=True, default=True, help="Start mobile API server")
@click.option("--api-port", default=8080, help="Mobile API port")
@click.option("--daemon", "-d", is_flag=True, default=False, help="Run in background")
def start(config, tun, api, api_port, daemon):
    """Start the Nexus tunnel client."""
    if daemon:
        console.print("[yellow]Starting Nexus in background...[/yellow]")
        os.execv(sys.executable, [sys.executable, "-m", "cli.main", "start",
                                   "--config", config or DEFAULT_CONFIG])
        return

    cfg = _load_config(config)
    if tun:
        cfg.tun.enabled = True

    console.print(
        Panel(
            f"[bold cyan]Hamieh Tunnel v1.0.0[/bold cyan]\n"
            f"Transport: [green]{cfg.transport.type.upper()}[/green]  "
            f"Relay: [green]{cfg.transport.relay_host}:{cfg.transport.relay_port}[/green]\n"
            f"SNI: [yellow]{cfg.transport.sni}[/yellow]  "
            f"SOCKS5: [green]{cfg.socks5.bind_host}:{cfg.socks5.bind_port}[/green]\n"
            f"TUN: {'[green]enabled[/green]' if cfg.tun.enabled else '[dim]disabled[/dim]'}  "
            f"API: {'[green]enabled[/green]' if api else '[dim]disabled[/dim]'}",
            title="[bold]Starting[/bold]",
            border_style="cyan",
        )
    )

    asyncio.run(_run_client(cfg, api, api_port))


@cli.command()
def stop():
    """Stop the running Nexus tunnel."""
    result = _api_request("POST", "/api/tunnel/stop")
    if "error" in result:
        console.print(f"[red]Error: {result['error']}[/red]")
    else:
        console.print(f"[green]Tunnel stopped.[/green]")


@cli.command()
def status():
    """Show current tunnel status and metrics."""
    result = _api_request("GET", "/api/tunnel/status")
    if "error" in result:
        console.print(f"[red]{result['error']}[/red]")
        return

    running = result.get("running", False)
    m = result.get("metrics", {})
    bw = m.get("bandwidth", {})
    conn = m.get("connections", {})
    tunnel = m.get("tunnel", {})

    table = Table(title="Hamieh Tunnel Status", border_style="cyan")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Status", "[green]RUNNING[/green]" if running else "[red]STOPPED[/red]")
    table.add_row("Uptime", f"{result.get('uptime_seconds', 0):.0f}s")
    table.add_row("Transport", result.get("transport", "—"))
    table.add_row("Relay", result.get("relay", "—"))
    table.add_row("SNI", result.get("sni", "—"))
    table.add_row("SOCKS5", f"{result.get('socks5', {}).get('host')}:{result.get('socks5', {}).get('port')}")
    table.add_row("Active Connections", str(conn.get("active", 0)))
    table.add_row("Total Connections", str(conn.get("total", 0)))
    table.add_row("Sent", f"{bw.get('sent_mb', 0):.2f} MB")
    table.add_row("Received", f"{bw.get('recv_mb', 0):.2f} MB")
    table.add_row("Tunnel Rotations", str(tunnel.get("rotations", 0)))

    console.print(table)


@cli.command()
@click.option("--follow", "-f", is_flag=True, help="Follow log output in real time")
@click.option("--limit", default=50, help="Number of lines to show")
def logs(follow, limit):
    """View tunnel logs."""
    if follow:
        _follow_logs()
        return

    result = _api_request("GET", f"/api/tunnel/logs?limit={limit}")
    if "error" in result:
        console.print(f"[red]{result['error']}[/red]")
        return

    for entry in result.get("logs", []):
        level = entry.get("level", "INFO")
        color = {"ERROR": "red", "WARNING": "yellow", "DEBUG": "dim"}.get(level, "white")
        console.print(
            f"[dim]{entry.get('ts', '')}[/dim] "
            f"[{color}]{level:<8}[/{color}] "
            f"[blue]{entry.get('logger', '')}[/blue]: "
            f"{entry.get('msg', '')}"
        )


def _follow_logs():
    """Stream logs via WebSocket."""
    try:
        import websocket  # websocket-client

        def on_message(ws, message):
            entry = json.loads(message)
            level = entry.get("level", "INFO")
            console.print(
                f"[dim]{entry.get('ts', '')}[/dim] [{level}] "
                f"{entry.get('logger', '')}: {entry.get('msg', '')}"
            )

        ws = websocket.WebSocketApp(
            f"ws://127.0.0.1:8080/ws/logs",
            on_message=on_message,
        )
        ws.run_forever()
    except ImportError:
        console.print("[yellow]Install websocket-client for --follow: pip install websocket-client[/yellow]")
        sys.exit(1)


@cli.command()
@click.option("--config", "-c", default=None, help="Config YAML file")
@click.option("--port", default=None, type=int, help="Override relay port")
@click.option("--token", default=None, help="Override auth token")
def server(config, port, token):
    """Start the Hamieh relay server."""
    if port:
        os.environ["HAMIEH_RELAY_PORT"] = str(port)
    if token:
        os.environ["HAMIEH_AUTH_TOKEN"] = token

    console.print(
        Panel("[bold cyan]Nexus Relay Server[/bold cyan]", border_style="cyan")
    )

    from server.relay import _run_server
    try:
        asyncio.run(_run_server(config))
    except KeyboardInterrupt:
        console.print("\n[yellow]Server stopped.[/yellow]")


@cli.command()
@click.option("--length", default=32, help="Token length in bytes")
def keygen(length):
    """Generate a new authentication token."""
    from core.crypto import generate_secret
    token = generate_secret(length)
    console.print(f"[green]Generated token:[/green]\n{token}")
    console.print("\n[dim]Add to config.yaml:[/dim]")
    console.print(f"[dim]  auth:\n    token: {token}[/dim]")


@cli.command()
@click.option("--cert", default="certs/relay_cert.pem", help="Certificate output path")
@click.option("--key", default="certs/relay_key.pem", help="Private key output path")
@click.option("--cn", default="hamieh-relay", help="Common name")
@click.option("--days", default=365, help="Validity period in days")
@click.option("--ip", multiple=True, help="IP SANs (repeatable)")
def cert(cert, key, cn, days, ip):
    """Generate a self-signed TLS certificate."""
    from core.crypto import generate_self_signed_cert
    generate_self_signed_cert(cert, key, cn=cn, days=days, san_ips=list(ip) or None)
    console.print(f"[green]Certificate:[/green] {cert}")
    console.print(f"[green]Private key:[/green] {key}")
    console.print(f"[dim]Valid for {days} days, CN={cn}[/dim]")


# ── Async runner ─────────────────────────────────────────────────────────────

async def _run_client(cfg: "HamiehConfig", api_enabled: bool, api_port: int) -> None:  # noqa: F821
    from core.logging_setup import setup_logging
    from core.metrics import serve_metrics
    from tunnel.manager import TunnelManager
    from client.socks5 import Socks5Server
    from client.routing import Router
    from mobile_api.api import serve_mobile_api, get_controller

    setup_logging(cfg.log)

    # Build components
    router = Router(cfg.routing)
    mgr = TunnelManager(cfg)
    socks5 = Socks5Server(cfg, mgr, router)

    # TUN interface (optional)
    tun_manager = None
    if cfg.tun.enabled:
        from client.tun_interface import TunManager
        tun_manager = TunManager(cfg.tun, cfg.socks5)

    # Attach to mobile API controller
    ctrl = get_controller()
    ctrl.attach(cfg, mgr, socks5, tun_manager)

    # Start everything
    await mgr.start()
    await socks5.start()

    if cfg.tun.enabled and tun_manager:
        await tun_manager.start()

    if api_enabled:
        await serve_mobile_api(cfg, bind_port=api_port)

    if cfg.metrics.enabled:
        await serve_metrics(cfg.metrics)

    console.print(
        f"\n[bold green]✓ Nexus tunnel active[/bold green]\n"
        f"  SOCKS5 proxy: [cyan]{cfg.socks5.bind_host}:{cfg.socks5.bind_port}[/cyan]\n"
        f"  Configure your OS/app to use this as a SOCKS5 proxy.\n\n"
        f"  [dim]Linux: export ALL_PROXY=socks5://{cfg.socks5.bind_host}:{cfg.socks5.bind_port}[/dim]\n"
        f"  [dim]Press Ctrl+C to stop.[/dim]\n"
    )

    # Run until Ctrl+C
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()

    console.print("\n[yellow]Shutting down...[/yellow]")
    if cfg.tun.enabled and tun_manager:
        await tun_manager.stop()
    await socks5.stop()
    await mgr.stop()

    m = get_metrics()
    console.print(
        f"[dim]Session: {m.bandwidth.mb_sent:.2f} MB sent, "
        f"{m.bandwidth.mb_recv:.2f} MB received, "
        f"{m.connections.total_opened} connections[/dim]"
    )


if __name__ == "__main__":
    cli()
