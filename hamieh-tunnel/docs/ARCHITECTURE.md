# Hamieh Tunnel — Architecture

## System Overview

Hamieh Tunnel is a modular SOCKS5 + TLS tunneling system designed for:
- Bypassing carrier-level DPI and traffic classification
- Full traffic capture via TUN interface
- Mobile (Android/Flutter) integration
- Production-scale relay operation

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CLIENT MACHINE                                    │
│                                                                             │
│  ┌──────────────┐    ┌─────────────────────────────────────────────────┐   │
│  │  Application │    │              NEXUS CLIENT                        │   │
│  │  (Browser,   │    │                                                  │   │
│  │   App, etc.) │    │  ┌───────────┐    ┌───────────┐   ┌──────────┐ │   │
│  └──────┬───────┘    │  │  SOCKS5   │    │  Router   │   │  Tunnel  │ │   │
│         │            │  │  Server   │───▶│  (rules)  │──▶│ Manager  │ │   │
│         │ SOCKS5     │  │  :1080    │    └───────────┘   └────┬─────┘ │   │
│         └───────────▶│  └─────▲─────┘                        │       │   │
│                       │        │ TCP                           │       │   │
│  ┌──────────────┐    │  ┌─────┴─────────────────────────────┐│       │   │
│  │  TUN Device  │    │  │           Transport Layer          ││       │   │
│  │  (nexus0)    │    │  │  ┌────────┐  ┌────────┐  ┌──────┐ ││       │   │
│  │  tun2socks   │───▶│  │  │  WSS   │  │ HTTPS  │  │Azure │ ││       │   │
│  └──────────────┘    │  │  │(+pool) │  │CONNECT │  │ Dev  │ ││       │   │
│                       │  │  └───┬────┘  └───┬────┘  └──┬───┘ ││       │   │
│  ┌──────────────┐    │  │      │obfuscation │         │     ││       │   │
│  │  Mobile API  │    │  └──────┼────────────┼─────────┼─────┘│       │   │
│  │  :8080       │    │         │            │         │       │       │   │
│  │  REST + WS   │    └─────────┼────────────┼─────────┼───────┘       │   │
│  └──────────────┘              │            │         │                    │
└────────────────────────────────┼────────────┼─────────┼────────────────────┘
                                 │            │         │
                    TLS + SNI spoof (teams.microsoft.com)
                                 │            │         │
         ┌───────────────────────▼────────────▼─────────▼───────────────────┐
         │                    CARRIER NETWORK                                │
         │                                                                   │
         │  ┌─────────────────────────────────────────────────────────────┐ │
         │  │                   DPI Engine                                │ │
         │  │  Sees TLS ClientHello with SNI=teams.microsoft.com         │ │
         │  │  ──▶ classifies as Teams traffic ──▶ free bucket           │ │
         │  └─────────────────────────────────────────────────────────────┘ │
         └───────────────────────────────────────────────────────────────────┘
                                 │
         ┌───────────────────────▼────────────────────────────────────────────┐
         │                    RELAY SERVER (VPS)                              │
         │                                                                    │
         │  ┌─────────────────────────────────────────────────────────────┐  │
         │  │              HamiehRelayServer                               │  │
         │  │                                                             │  │
         │  │  WSS :8443 ──▶ authenticate ──▶ parse OPEN frame          │  │
         │  │  HTTPS :8444 ──▶ CONNECT upgrade ──▶ parse destination    │  │
         │  │                         │                                   │  │
         │  │  ┌──────────────────────▼──────────────────────────────┐  │  │
         │  │  │              Rate Limiter                            │  │  │
         │  │  │  Token bucket (req/min) + Bandwidth + Ban list      │  │  │
         │  │  └──────────────────────┬──────────────────────────────┘  │  │
         │  │                         │                                   │  │
         │  │  ┌──────────────────────▼──────────────────────────────┐  │  │
         │  │  │           Bidirectional TCP Relay                    │  │  │
         │  │  │  client ◀──────────────────────────▶ destination    │  │  │
         │  │  └─────────────────────────────────────────────────────┘  │  │
         │  └─────────────────────────────────────────────────────────────┘  │
         │                                                                    │
         │  Metrics: :9100/metrics (Prometheus)                               │
         └────────────────────────────────────────────────────────────────────┘
                                 │
                 ┌───────────────▼────────────────────┐
                 │        REAL DESTINATION              │
                 │   (YouTube, Instagram, etc.)         │
                 └──────────────────────────────────────┘
```

## Module Map

```
hamieh-tunnel/
├── core/               Shared utilities
│   ├── config.py       YAML config loader + dataclasses
│   ├── logging_setup.py Structured logging (text/JSON)
│   ├── metrics.py      In-process metrics + Prometheus endpoint
│   └── crypto.py       TLS cert generation, JWT auth
│
├── tunnel/             Transport layer
│   ├── manager.py      Pool manager + load balancing + rotation
│   ├── obfuscation.py  Padding + timing jitter
│   └── transport/
│       ├── base.py     Abstract Transport interface
│       ├── protocol.py Nexus wire protocol (OPEN/STATUS frames)
│       ├── wss.py      WebSocket/TLS transport (primary)
│       ├── https_fallback.py HTTPS CONNECT transport
│       └── azure_dev.py Azure Dev Tunnel module
│
├── client/             Client-side components
│   ├── socks5.py       Full SOCKS5 server (TCP CONNECT + UDP ASSOCIATE)
│   ├── routing.py      Smart routing engine (rules-based)
│   └── tun_interface.py TUN device + tun2socks + iptables
│
├── server/             Relay server
│   ├── relay.py        HamiehRelayServer (WSS + HTTPS CONNECT)
│   └── rate_limiter.py Per-client rate limiting + ban list
│
├── mobile_api/         Mobile integration layer
│   └── api.py          REST + WebSocket API (:8080)
│
├── cli/                Command-line interface
│   └── main.py         `hamieh` command (start/stop/status/logs/server)
│
└── config/             Configuration files
    ├── default.yaml    Default client config
    ├── server.yaml     Server config
    ├── example-sni-bypass.yaml
    └── example-azure-dev.yaml
```

## Data Flow

### 1. Client → Relay (WSS path)

```
App TCP connection
    │
    ▼ SOCKS5 handshake (RFC 1928)
SOCKS5 Server (127.0.0.1:1080)
    │
    ▼ Router.decide(host, port) → "tunnel"
TunnelManager.open_stream(host, port)
    │
    ▼ WebSocketTransport._connect()
    │   - asyncio.open_connection(relay_host, relay_port)
    │   - TLS handshake with SNI = cfg.sni (e.g. teams.microsoft.com)
    │   - WebSocket upgrade (GET /tunnel)
    │   - Send OPEN frame: [0x01][host_len][host][port][token_len][token]
    │   - Read STATUS frame: [0x10][0x00] = OK
    │
    ▼ SOCKS5 sends success reply to application
    │
    ▼ Bidirectional relay begins
App ◀──── data ────▶ WS ◀──── data ────▶ Relay TCP ◀──── data ────▶ Destination
```

### 2. Relay: incoming connection handling

```
TLS ClientHello (SNI = teams.microsoft.com — carrier sees this)
    │
    ▼ WebSocket upgrade accepted
    │
    ▼ Read OPEN frame
    │   - Parse: dst_host, dst_port, token
    │
    ▼ RateLimiter.is_allowed(client_ip)?
    │   YES ──▶ verify_token(token, cfg.auth.token)
    │   NO  ──▶ close(1008, "Rate limit exceeded")
    │
    ▼ Auth OK
    │
    ▼ asyncio.open_connection(dst_host, dst_port)
    │
    ▼ Send STATUS OK [0x10][0x00]
    │
    ▼ asyncio.gather(ws→tcp, tcp→ws)  — bidirectional relay
```

### 3. TUN mode (full traffic capture)

```
OS sends IP packet for 8.8.8.8:443
    │
    ▼ Routing table: default via nexus0 (TUN device)
TUN device (nexus0 / 10.88.0.1)
    │
    ▼ tun2socks reads raw IP packet
    │   Wraps as SOCKS5 CONNECT to 8.8.8.8:443
    │
    ▼ SOCKS5 Server (127.0.0.1:1080)
    │   (same flow as above)
    │
    ▼ TunnelManager → WSS → Relay → 8.8.8.8:443
```

## Security Model

| Component | Protection |
|-----------|-----------|
| Relay auth | JWT token (HS256) or shared secret |
| Transport | TLS 1.2+ with configurable SNI |
| Rate limiting | Token bucket per client IP |
| Brute force | Ban after N auth failures |
| Open proxy | Prevented: auth required for every connection |
| Bandwidth abuse | Per-client byte/second limit |
| Certificate | Auto-generated RSA-2048, or bring your own |

## Performance Design

- **Async I/O**: 100% asyncio — no blocking calls in hot paths
- **Connection pool**: Pre-established WSS connections (configurable min/max)
- **Zero-copy relay**: Direct `reader.read() → writer.write()` without intermediate buffers
- **Concurrent connections**: Limited only by `relay.max_clients` and OS FD limits
- **Multi-relay**: Round-robin load balancing across N relay endpoints
- **Buffer size**: Configurable (default 64 KB per relay chunk)
