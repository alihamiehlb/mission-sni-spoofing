# Hamieh Tunnel — Setup Guide

## Prerequisites

- Python 3.11+
- A VPS or cloud VM (relay server)
- Optional: tun2socks binary for full traffic capture

## Quick Start

### 1. Install

```bash
# Clone
git clone https://github.com/yourorg/hamieh-tunnel.git
cd hamieh-tunnel

# Install (recommended: uv)
pip install uv
uv sync

# Or with pip:
pip install -e .
```

### 2. Generate credentials

```bash
# Generate auth token (copy this — you need it on both client and server)
hamieh keygen

# Generate TLS certificate for the relay
hamieh cert --cert certs/relay_cert.pem --key certs/relay_key.pem
```

### 3. Configure

```bash
cp config/default.yaml config/my-config.yaml
# Edit:
#   transport.relay_host → your relay server IP
#   auth.token → your generated token
#   transport.sni → carrier-spoofed domain (if applicable)
nano config/my-config.yaml
```

---

## Running on a VPS (Relay Server)

### Install on VPS

```bash
# SSH into your VPS
ssh user@your-vps-ip

# Install Python 3.11+
sudo apt update && sudo apt install python3 python3-pip -y

# Install hamieh
pip install hamieh-tunnel
# or from source:
git clone ... && cd hamieh-tunnel && pip install -e .

# Copy server config
cp config/server.yaml /etc/hamieh/server.yaml

# Edit token (must match client)
nano /etc/hamieh/server.yaml
```

### Generate cert on VPS

```bash
hamieh cert \
  --cert /etc/hamieh/certs/relay_cert.pem \
  --key  /etc/hamieh/certs/relay_key.pem \
  --cn   hamieh-relay \
  --ip   $(curl -s ifconfig.me)
```

### Start relay server

```bash
# Foreground (test):
hamieh server --config /etc/hamieh/server.yaml

# Systemd service (production):
sudo nano /etc/systemd/system/hamieh-relay.service
```

**Systemd unit:**
```ini
[Unit]
Description=Nexus Relay Server
After=network.target

[Service]
Type=simple
User=hamieh
ExecStart=/usr/local/bin/hamieh server --config /etc/hamieh/server.yaml
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hamieh-relay
sudo systemctl status hamieh-relay
```

### Open firewall port

```bash
# UFW
sudo ufw allow 8443/tcp
sudo ufw allow 8444/tcp

# iptables
sudo iptables -A INPUT -p tcp --dport 8443 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8444 -j ACCEPT

# AWS Security Group / Azure NSG: open TCP 8443, 8444 inbound
```

---

## Running Locally (Client)

### Basic (SOCKS5 proxy mode)

```bash
hamieh start --config config/my-config.yaml
```

This starts the SOCKS5 proxy on `127.0.0.1:1080`.

**Configure your OS/app to use it:**

```bash
# Linux (terminal)
export ALL_PROXY=socks5://127.0.0.1:1080

# Linux (GNOME Settings → Network → Proxy → SOCKS5)
# Set host: 127.0.0.1, port: 1080

# macOS (System Settings → Network → Proxies → SOCKS5)
# Set host: 127.0.0.1, port: 1080

# Windows (Settings → Network → Proxy → Use a proxy server)
# Set: 127.0.0.1:1080 (type: SOCKS5)

# Firefox: Settings → Network → Manual proxy → SOCKS5 Host: 127.0.0.1, Port: 1080

# curl:
curl --proxy socks5h://127.0.0.1:1080 https://ifconfig.me
```

### Full traffic capture (TUN mode, requires root)

```bash
# Install tun2socks
wget https://github.com/xjasonlyu/tun2socks/releases/latest/download/tun2socks-linux-amd64.zip
unzip tun2socks-linux-amd64.zip
sudo mv tun2socks /usr/local/bin/

# Enable TUN in config
# tun:
#   enabled: true
#   name: nexus0

sudo hamieh start --config config/my-config.yaml --tun
```

### Check it's working

```bash
# Check exit IP (should show relay server's IP)
curl --proxy socks5h://127.0.0.1:1080 https://ifconfig.me

# Check tunnel status
hamieh status

# View logs
hamieh logs
hamieh logs --follow
```

---

## Connecting from a Phone (USB tethering / Wi-Fi hotspot)

### Method 1: Phone connects through laptop (USB tethering)

1. Connect phone to laptop via USB
2. Enable USB tethering on phone
3. Laptop runs hamieh with bind_host: "0.0.0.0" or the USB interface IP
4. Set SOCKS5 proxy on phone to laptop's USB IP, port 1080

```yaml
# config/my-config.yaml
socks5:
  bind_host: "0.0.0.0"   # Listen on all interfaces
  bind_port: 1080
```

Then on Android:
- Settings → Wi-Fi → Long-press your network → Modify → Advanced → Proxy → Manual
- Host: 192.168.42.1 (typical USB tethering IP), Port: 1080

### Method 2: Flutter App (see docs/MOBILE.md)

The Flutter app connects to the mobile API on `127.0.0.1:8080` and controls
the tunnel directly from inside the Android process.

---

## Stopping

```bash
# Graceful stop (Ctrl+C in the terminal where hamieh is running)
^C

# Or from another terminal:
hamieh stop
```

---

## Troubleshooting

### Relay not reachable

```bash
# Test TLS connection to relay
openssl s_client -connect your-relay:8443 -servername teams.microsoft.com

# Check relay is running on server
systemctl status hamieh-relay
journalctl -u hamieh-relay -n 50
```

### SOCKS5 not routing traffic

```bash
# Verify proxy is listening
ss -tlnp | grep 1080
# or
netstat -tlnp | grep 1080

# Test proxy
curl --proxy socks5h://127.0.0.1:1080 https://ifconfig.me
```

### Auth failures

```bash
# Check tokens match between client and server
hamieh logs | grep auth
# Server logs:
journalctl -u hamieh-relay | grep auth
```

### Rate limit errors

Edit `config/server.yaml`:
```yaml
rate_limit:
  requests_per_minute: 1200
  bytes_per_second: 20971520   # 20 MB/s
```

---

## Environment Variable Overrides

All config values can be overridden with environment variables:

```
HAMIEH_TRANSPORT_RELAY_HOST=1.2.3.4
HAMIEH_TRANSPORT_SNI=teams.microsoft.com
HAMIEH_AUTH_TOKEN=mysecrettoken
HAMIEH_RELAY_PORT=9443
HAMIEH_LOG_LEVEL=DEBUG
```
