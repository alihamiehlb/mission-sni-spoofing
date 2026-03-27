#!/bin/bash
# Hamieh Tunnel Relay — One-command VPS deployment
#
# Usage:
#   1. Copy the entire server/ folder to your VPS
#   2. Set your auth token: export HAMIEH_AUTH_TOKEN="your-secret-password"
#   3. Run: bash deploy.sh
#
# Requirements: Docker + Docker Compose
# Recommended VPS: any $4-6/mo VPS (1 CPU, 1GB RAM handles ~500 users)

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}║     Hamieh Tunnel Relay Deploy       ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
echo ""

if [ -z "${HAMIEH_AUTH_TOKEN:-}" ]; then
    echo -e "${RED}Error: HAMIEH_AUTH_TOKEN is not set${NC}"
    echo ""
    echo "Set it before running this script:"
    echo "  export HAMIEH_AUTH_TOKEN=\"your-secret-password\""
    echo ""
    echo "This password is what your phone app uses to authenticate."
    exit 1
fi

if ! command -v docker &>/dev/null; then
    echo -e "${CYAN}Installing Docker...${NC}"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
fi

if ! docker compose version &>/dev/null; then
    echo -e "${RED}Docker Compose not found. Install it: https://docs.docker.com/compose/install/${NC}"
    exit 1
fi

echo -e "${GREEN}Building and starting relay...${NC}"
docker compose up -d --build

echo ""
echo -e "${GREEN}═══════════════════════════════════════${NC}"
echo -e "${GREEN}  Hamieh Relay is running!${NC}"
echo -e "${GREEN}═══════════════════════════════════════${NC}"
echo ""

SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

echo -e "  Server IP:    ${CYAN}${SERVER_IP}${NC}"
echo -e "  Raw TLS port: ${CYAN}8445${NC}  (phone clients)"
echo -e "  WSS port:     ${CYAN}8443${NC}  (desktop clients)"
echo -e "  Auth token:   ${CYAN}${HAMIEH_AUTH_TOKEN}${NC}"
echo ""
echo -e "  ${CYAN}In the Hamieh Tunnel app:${NC}"
echo -e "    Relay Host:  ${SERVER_IP}"
echo -e "    Relay Port:  8445"
echo -e "    Token:       ${HAMIEH_AUTH_TOKEN}"
echo ""
echo -e "  Check health:  curl -k https://localhost:8443/health"
echo -e "  View stats:    curl http://localhost:9100/status"
echo -e "  View logs:     docker compose logs -f"
echo -e "  Stop:          docker compose down"
echo ""
