"""
Cryptographic utilities: TLS certificate generation, JWT token auth,
self-signed cert creation, and key derivation helpers.
"""

import datetime
import ipaddress
import secrets
import ssl
import subprocess
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


# ---------------------------------------------------------------------------
# Certificate generation
# ---------------------------------------------------------------------------

def generate_self_signed_cert(
    cert_path: str | Path,
    key_path: str | Path,
    cn: str = "hamieh-relay",
    days: int = 365,
    san_ips: list[str] | None = None,
    san_dns: list[str] | None = None,
) -> None:
    """
    Generate a self-signed RSA-2048 certificate and private key.
    Uses the `cryptography` library so there's no openssl subprocess dependency.
    """
    cert_path = Path(cert_path)
    key_path = Path(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    # Generate RSA-2048 private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Build subject / issuer
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Hamieh Tunnel"),
    ])

    # Subject Alternative Names
    san_entries: list[x509.GeneralName] = []
    for ip in (san_ips or []):
        san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
    for dns in (san_dns or []):
        san_entries.append(x509.DNSName(dns))
    if not san_entries:
        san_entries.append(x509.DNSName(cn))

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .sign(private_key, hashes.SHA256())
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def ensure_cert(cert_path: str | Path, key_path: str | Path, **kwargs) -> None:
    """Generate cert+key only if they don't already exist."""
    if not Path(cert_path).exists() or not Path(key_path).exists():
        generate_self_signed_cert(cert_path, key_path, **kwargs)


# ---------------------------------------------------------------------------
# SSL context factories
# ---------------------------------------------------------------------------

def client_ssl_context(
    sni_override: str = "",
    verify: bool = False,
    ca_file: str = "",
    client_cert: str = "",
    client_key: str = "",
) -> ssl.SSLContext:
    """
    Create an SSL context for the tunnel client.

    sni_override is sent in the TLS ClientHello — this is the carrier-spoofing knob.
    When verify=False the client accepts any server cert (self-signed relay).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    if verify and ca_file:
        ctx.load_verify_locations(ca_file)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = True
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    # mTLS client certificate
    if client_cert and client_key:
        ctx.load_cert_chain(client_cert, client_key)

    return ctx


def server_ssl_context(
    cert_file: str,
    key_file: str,
    ca_file: str = "",
    require_client_cert: bool = False,
) -> ssl.SSLContext:
    """Create an SSL context for the relay server."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    if require_client_cert and ca_file:
        ctx.load_verify_locations(ca_file)
        ctx.verify_mode = ssl.CERT_REQUIRED

    return ctx


# ---------------------------------------------------------------------------
# JWT token authentication
# ---------------------------------------------------------------------------

def generate_token(secret: str, ttl_seconds: int = 3600) -> str:
    """
    Generate a signed JWT token for relay authentication.
    Uses HS256 — no external key management required.
    """
    import time
    try:
        import jwt
        now = int(time.time())
        payload = {"iat": now, "exp": now + ttl_seconds, "sub": "hamieh-client"}
        return jwt.encode(payload, secret, algorithm="HS256")
    except ImportError:
        # Fallback: just use the raw secret as token
        return secret


def verify_token(token: str, secret: str) -> bool:
    """Verify a JWT token. Returns True if valid."""
    try:
        import jwt
        jwt.decode(token, secret, algorithms=["HS256"])
        return True
    except Exception:
        # Constant-time comparison fallback
        return secrets.compare_digest(token, secret)


def generate_secret(length: int = 32) -> str:
    """Generate a cryptographically secure random secret."""
    return secrets.token_urlsafe(length)
