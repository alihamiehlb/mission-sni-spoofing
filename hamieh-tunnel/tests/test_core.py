"""
Core component tests — run without a live relay server.
Tests config, crypto, routing, rate limiting, and protocol encoding.
"""

import asyncio
import struct
import sys
import tempfile
import os
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Config ────────────────────────────────────────────────────────────────────

def test_default_config():
    from core.config import HamiehConfig, load_config
    cfg = load_config(None)
    assert cfg.mode == "client"
    assert cfg.socks5.bind_port == 1080
    assert cfg.transport.sni == "teams.microsoft.com"
    assert len(cfg.auth.token) > 0  # auto-generated


def test_config_from_yaml(tmp_path):
    yaml_content = """
mode: server
transport:
  relay_host: "1.2.3.4"
  relay_port: 9443
  sni: "update.microsoft.com"
auth:
  token: "my-secret-token"
"""
    cfg_file = tmp_path / "test.yaml"
    cfg_file.write_text(yaml_content)

    from core.config import load_config
    cfg = load_config(str(cfg_file))
    assert cfg.mode == "server"
    assert cfg.transport.relay_host == "1.2.3.4"
    assert cfg.transport.relay_port == 9443
    assert cfg.transport.sni == "update.microsoft.com"
    assert cfg.auth.token == "my-secret-token"


def test_env_var_override(monkeypatch):
    monkeypatch.setenv("HAMIEH_TRANSPORT_SNI", "test.microsoft.com")
    monkeypatch.setenv("HAMIEH_AUTH_TOKEN", "envtoken")

    from core.config import load_config
    import importlib, core.config
    importlib.reload(core.config)
    from core.config import load_config as lc
    cfg = lc(None)
    # env vars are lowercase-matched
    # HAMIEH_AUTH → section=auth, key=token
    assert cfg.auth.token == "envtoken"


# ── Crypto ────────────────────────────────────────────────────────────────────

def test_cert_generation():
    from core.crypto import generate_self_signed_cert
    with tempfile.TemporaryDirectory() as d:
        cert = os.path.join(d, "cert.pem")
        key = os.path.join(d, "key.pem")
        generate_self_signed_cert(cert, key, cn="test", days=1)
        assert Path(cert).stat().st_size > 100
        assert Path(key).stat().st_size > 100
        assert b"CERTIFICATE" in Path(cert).read_bytes()
        assert b"PRIVATE KEY" in Path(key).read_bytes()


def test_jwt_round_trip():
    from core.crypto import generate_secret, generate_token, verify_token
    secret = generate_secret()
    token = generate_token(secret, ttl_seconds=60)
    assert verify_token(token, secret)
    assert not verify_token(token, "wrong-secret")
    assert not verify_token("invalid-token", secret)


def test_generate_secret_uniqueness():
    from core.crypto import generate_secret
    secrets = {generate_secret() for _ in range(10)}
    assert len(secrets) == 10  # all unique


# ── Routing ───────────────────────────────────────────────────────────────────

def test_routing_default_tunnel():
    from core.config import RoutingConfig
    from client.routing import Router
    router = Router(RoutingConfig(default_action="tunnel"))
    assert router.decide("8.8.8.8", 443) == "tunnel"


def test_routing_private_direct():
    from core.config import RoutingConfig, RoutingRule
    from client.routing import Router
    cfg = RoutingConfig(
        default_action="tunnel",
        rules=[RoutingRule(match="private", action="direct", priority=1)],
    )
    router = Router(cfg)
    assert router.decide("192.168.1.100", 80) == "direct"
    assert router.decide("10.0.0.1", 8080) == "direct"
    assert router.decide("172.16.0.1", 443) == "direct"
    assert router.decide("8.8.8.8", 443) == "tunnel"


def test_routing_domain_glob():
    from core.config import RoutingConfig, RoutingRule
    from client.routing import Router
    cfg = RoutingConfig(
        default_action="tunnel",
        rules=[RoutingRule(match="*.blocked.com", action="block", priority=1)],
    )
    router = Router(cfg)
    assert router.decide("ads.blocked.com", 80) == "block"
    assert router.decide("tracker.blocked.com", 443) == "block"
    assert router.decide("safe.com", 80) == "tunnel"


def test_routing_cidr():
    from core.config import RoutingConfig, RoutingRule
    from client.routing import Router
    cfg = RoutingConfig(
        default_action="tunnel",
        rules=[RoutingRule(match="203.0.113.0/24", action="direct", priority=1)],
    )
    router = Router(cfg)
    assert router.decide("203.0.113.42", 443) == "direct"
    assert router.decide("203.0.114.1", 443) == "tunnel"


def test_routing_priority_order():
    from core.config import RoutingConfig, RoutingRule
    from client.routing import Router
    cfg = RoutingConfig(
        default_action="tunnel",
        rules=[
            RoutingRule(match="all", action="direct", priority=100),
            RoutingRule(match="*.blocked.com", action="block", priority=1),
        ],
    )
    router = Router(cfg)
    # Higher priority (lower number) wins
    assert router.decide("ads.blocked.com", 80) == "block"
    assert router.decide("anything-else.com", 80) == "direct"


# ── Rate Limiter ──────────────────────────────────────────────────────────────

def test_rate_limiter_allows_up_to_limit():
    from core.config import RateLimitConfig
    from server.rate_limiter import RateLimiter
    rl = RateLimiter(RateLimitConfig(enabled=True, requests_per_minute=10))
    results = [rl.is_allowed("1.2.3.4") for _ in range(15)]
    assert results[:10] == [True] * 10
    assert results[10:] == [False] * 5


def test_rate_limiter_bans_on_auth_failures():
    from core.config import RateLimitConfig
    from server.rate_limiter import RateLimiter
    rl = RateLimiter(RateLimitConfig(enabled=True, ban_threshold=3))
    for _ in range(3):
        rl.record_auth_failure("9.9.9.9")
    assert not rl.is_allowed("9.9.9.9")


def test_rate_limiter_disabled():
    from core.config import RateLimitConfig
    from server.rate_limiter import RateLimiter
    rl = RateLimiter(RateLimitConfig(enabled=False))
    # When disabled, always allows
    for _ in range(1000):
        assert rl.is_allowed("1.2.3.4")


# ── Protocol ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_frame_encode_decode():
    from tunnel.transport.protocol import encode_open, read_open, FRAME_OPEN

    frame = encode_open("example.com", 443, "mytoken")
    assert frame[0] == FRAME_OPEN

    # Simulate server parsing
    reader = asyncio.StreamReader()
    reader.feed_data(frame)
    reader.feed_eof()

    host, port, token = await read_open(reader)
    assert host == "example.com"
    assert port == 443
    assert token == "mytoken"


@pytest.mark.asyncio
async def test_open_frame_no_token():
    from tunnel.transport.protocol import encode_open, read_open

    frame = encode_open("192.168.1.1", 8080, "")
    reader = asyncio.StreamReader()
    reader.feed_data(frame)
    reader.feed_eof()

    host, port, token = await read_open(reader)
    assert host == "192.168.1.1"
    assert port == 8080
    assert token == ""


# ── Metrics ───────────────────────────────────────────────────────────────────

def test_metrics_counters():
    from core.metrics import HamiehMetrics
    m = HamiehMetrics()
    m.conn_opened()
    m.conn_opened()
    m.conn_opened()
    m.conn_closed()
    assert m.connections.active == 2
    assert m.connections.total_opened == 3
    assert m.connections.total_closed == 1

    m.bandwidth.add_sent(1024 * 1024)
    m.bandwidth.add_recv(2 * 1024 * 1024)
    assert abs(m.bandwidth.mb_sent - 1.0) < 0.01
    assert abs(m.bandwidth.mb_recv - 2.0) < 0.01


def test_metrics_summary():
    from core.metrics import HamiehMetrics
    m = HamiehMetrics()
    s = m.summary()
    assert "connections" in s
    assert "bandwidth" in s
    assert "tunnel" in s
    assert s["connections"]["active"] == 0
