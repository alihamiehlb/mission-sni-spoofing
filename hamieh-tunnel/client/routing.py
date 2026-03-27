"""
Smart routing engine.

Decides whether a given (host, port) should be:
  - "tunnel"  — forwarded through the tunnel
  - "direct"  — connected directly (bypass tunnel)
  - "block"   — rejected

Rules are evaluated in priority order (lowest number = highest priority).
The first matching rule wins. Default action applies if no rule matches.

Rule match formats:
  - CIDR notation:   "192.168.0.0/16", "10.0.0.0/8"
  - Domain glob:     "*.google.com", "example.com"
  - Port range:      "port:22", "port:1-1023"
  - "all"            — catch-all wildcard
  - "private"        — shorthand for all RFC1918 + loopback ranges
  - "local"          — loopback only

Example config:
  routing:
    default_action: tunnel
    rules:
      - match: private
        action: direct
        priority: 10
      - match: "*.microsoft.com"
        action: direct
        priority: 20
      - match: "192.168.1.0/24"
        action: direct
        priority: 5
"""

import fnmatch
import ipaddress
import logging
from dataclasses import dataclass
from typing import Optional

from core.config import RoutingConfig, RoutingRule

logger = logging.getLogger(__name__)


_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

_LOCAL_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]


def _ip_in_networks(ip_str: str, networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in networks)
    except ValueError:
        return False


def _matches_rule(rule: RoutingRule, host: str, port: int) -> bool:
    """Check if a host:port matches a routing rule."""
    m = rule.match.strip().lower()

    if m == "all":
        return True

    if m == "private":
        return _ip_in_networks(host, _PRIVATE_NETWORKS)

    if m == "local":
        return _ip_in_networks(host, _LOCAL_NETWORKS)

    if m.startswith("port:"):
        spec = m[5:]
        if "-" in spec:
            lo, hi = spec.split("-", 1)
            return int(lo) <= port <= int(hi)
        return port == int(spec)

    # CIDR
    if "/" in m:
        try:
            net = ipaddress.ip_network(m, strict=False)
            try:
                return ipaddress.ip_address(host) in net
            except ValueError:
                return False
        except ValueError:
            pass

    # Domain glob
    if fnmatch.fnmatch(host.lower(), m):
        return True

    # Exact IP match
    if host.lower() == m:
        return True

    return False


class Router:
    """Evaluates routing rules to decide how to handle each connection."""

    def __init__(self, cfg: RoutingConfig) -> None:
        # Sort rules by priority (ascending = highest priority first)
        self._rules = sorted(cfg.rules, key=lambda r: r.priority)
        self._default = cfg.default_action
        logger.info(
            "Router loaded %d rules, default=%s", len(self._rules), self._default
        )

    def decide(self, host: str, port: int) -> str:
        """
        Returns "tunnel", "direct", or "block".
        """
        for rule in self._rules:
            if _matches_rule(rule, host, port):
                logger.debug(
                    "Route %s:%d → %s (rule: %s)", host, port, rule.action, rule.match
                )
                return rule.action

        logger.debug("Route %s:%d → %s (default)", host, port, self._default)
        return self._default
