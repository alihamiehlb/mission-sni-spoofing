"""
Configuration loader and validator for Hamieh Tunnel.

Supports YAML config files with environment variable overrides.
All values have sane defaults so the system works out of the box.
"""

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Sub-config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TLSConfig:
    cert_file: str = "certs/relay_cert.pem"
    key_file: str = "certs/relay_key.pem"
    ca_file: str = ""                    # optional CA for mTLS
    verify_client: bool = False          # enable mTLS on server
    min_version: str = "TLSv1.2"
    sni_override: str = ""              # spoof SNI (e.g. "teams.microsoft.com")


@dataclass
class AuthConfig:
    token: str = ""                      # shared bearer token (auto-generated if empty)
    token_ttl_seconds: int = 3600        # JWT token lifetime
    enable_mtls: bool = False


@dataclass
class PoolConfig:
    min_connections: int = 2             # minimum idle connections per relay
    max_connections: int = 20            # maximum concurrent connections per relay
    connect_timeout: float = 10.0
    idle_timeout: float = 120.0          # close idle connections after N seconds
    max_retries: int = 3


@dataclass
class RelayConfig:
    host: str = "0.0.0.0"
    port: int = 8443
    max_clients: int = 500
    buffer_size: int = 65536
    connect_timeout: float = 15.0


@dataclass
class TransportConfig:
    """Controls which transport the client uses to reach the relay."""
    type: str = "wss"                    # "wss" | "https" | "azure_dev"
    relay_host: str = ""
    relay_port: int = 8443
    path: str = "/tunnel"                # WebSocket path (wss transport)
    # SNI to use in TLS ClientHello — the key carrier-spoofing knob
    sni: str = "teams.microsoft.com"
    verify_relay_cert: bool = False      # False = accept self-signed relay cert
    tls: TLSConfig = field(default_factory=TLSConfig)
    pool: PoolConfig = field(default_factory=PoolConfig)
    # Azure Dev Tunnel (optional module)
    azure_tunnel_url: str = ""


@dataclass
class Socks5Config:
    bind_host: str = "127.0.0.1"
    bind_port: int = 1080
    enable_udp: bool = True
    auth_user: str = ""                  # SOCKS5 username auth (optional)
    auth_pass: str = ""


@dataclass
class RoutingRule:
    """A single routing rule: match traffic and decide to tunnel or bypass."""
    match: str = ""                       # CIDR, domain glob, or "all"
    action: str = "tunnel"               # "tunnel" | "direct" | "block"
    priority: int = 100


@dataclass
class RoutingConfig:
    default_action: str = "tunnel"       # what to do with unmatched traffic
    rules: list[RoutingRule] = field(default_factory=list)


@dataclass
class TunConfig:
    """Linux TUN device config for full traffic capture without SOCKS5."""
    enabled: bool = False
    name: str = "nexus0"
    address: str = "10.88.0.1"
    netmask: str = "255.255.255.0"
    mtu: int = 1500
    tun2socks_bin: str = "tun2socks"     # path to tun2socks binary


@dataclass
class ObfuscationConfig:
    enabled: bool = False
    padding_min: int = 16                # min random bytes added per packet
    padding_max: int = 256               # max random bytes added per packet
    timing_jitter_ms: int = 20          # max random delay added per write


@dataclass
class RotationConfig:
    enabled: bool = False
    interval_seconds: int = 300          # rotate every N seconds
    max_bytes: int = 100 * 1024 * 1024  # or after N bytes
    relay_pool: list[str] = field(default_factory=list)  # extra relay addresses


@dataclass
class RateLimitConfig:
    enabled: bool = True
    requests_per_minute: int = 600       # per-client
    bytes_per_second: int = 10 * 1024 * 1024  # 10 MB/s per client
    ban_threshold: int = 10              # auth failures before ban


@dataclass
class LogConfig:
    level: str = "INFO"
    file: str = ""                       # empty = stdout only
    json_format: bool = False
    access_log: bool = True


@dataclass
class MetricsConfig:
    enabled: bool = True
    bind_host: str = "127.0.0.1"
    bind_port: int = 9100               # Prometheus metrics endpoint


@dataclass
class HamiehConfig:
    """Root configuration object."""
    # Mode: "client" runs SOCKS5+transport, "server" runs relay, "both" runs both
    mode: str = "client"

    transport: TransportConfig = field(default_factory=TransportConfig)
    socks5: Socks5Config = field(default_factory=Socks5Config)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    tun: TunConfig = field(default_factory=TunConfig)
    relay: RelayConfig = field(default_factory=RelayConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    obfuscation: ObfuscationConfig = field(default_factory=ObfuscationConfig)
    rotation: RotationConfig = field(default_factory=RotationConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    tls: TLSConfig = field(default_factory=TLSConfig)
    log: LogConfig = field(default_factory=LogConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _from_dict(cls: type, data: dict) -> Any:
    """Recursively instantiate dataclasses from a dict."""
    import dataclasses

    if not dataclasses.is_dataclass(cls):
        return data

    field_types = {f.name: f.type for f in dataclasses.fields(cls)}
    kwargs: dict[str, Any] = {}

    for f in dataclasses.fields(cls):
        val = data.get(f.name)
        if val is None:
            continue

        ftype = f.type
        # Resolve string annotations
        if isinstance(ftype, str):
            import typing
            ftype = eval(ftype, {**vars(typing), **globals()})  # noqa: S307

        # Nested dataclass
        if dataclasses.is_dataclass(ftype) and isinstance(val, dict):
            kwargs[f.name] = _from_dict(ftype, val)
        # list[RoutingRule]
        elif (
            hasattr(ftype, "__origin__")
            and ftype.__origin__ is list
            and val
            and isinstance(val, list)
        ):
            item_type = ftype.__args__[0]
            if dataclasses.is_dataclass(item_type):
                kwargs[f.name] = [_from_dict(item_type, i) for i in val]
            else:
                kwargs[f.name] = val
        else:
            kwargs[f.name] = val

    return cls(**kwargs)


def load_config(path: str | Path | None = None) -> HamiehConfig:
    """
    Load configuration from a YAML file, then apply environment variable overrides.

    Environment variable format: HAMIEH_<SECTION>_<KEY> (all caps, underscores).
    Example: HAMIEH_TRANSPORT_SNI=teams.microsoft.com
    """
    raw: dict = {}

    if path:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}

    # Environment variable overrides (flat dotted keys)
    for env_key, env_val in os.environ.items():
        if not env_key.startswith("HAMIEH_"):
            continue
        parts = env_key[7:].lower().split("_", 1)
        if len(parts) == 2:
            section, key = parts
            if section not in raw:
                raw[section] = {}
            raw[section][key] = env_val

    cfg = _from_dict(HamiehConfig, raw)

    # Auto-generate auth token if not provided
    if not cfg.auth.token:
        cfg.auth.token = secrets.token_urlsafe(32)

    return cfg


def default_config() -> HamiehConfig:
    """Return a config with all defaults."""
    return HamiehConfig()
