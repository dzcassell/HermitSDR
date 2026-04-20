#!/usr/bin/env bash
set -euo pipefail

# ─── Colors ───
if [ -t 1 ]; then
    BOLD=$'\033[1m'; GREEN=$'\033[0;32m'; BLUE=$'\033[0;34m'
    YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; DIM=$'\033[2m'; NC=$'\033[0m'
else
    BOLD=''; GREEN=''; BLUE=''; YELLOW=''; RED=''; DIM=''; NC=''
fi

echo "${BOLD}=== HermitSDR Install ===${NC}"
echo ""

# ─── Prereq checks ───
if ! command -v docker >/dev/null 2>&1; then
    echo "${RED}ERROR: docker not found. Install Docker Engine first.${NC}" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "${RED}ERROR: docker compose v2 not found.${NC}" >&2
    exit 1
fi

# ─── Build + start ───
echo "${BLUE}Building Docker image...${NC}"
docker compose build

echo ""
echo "${BLUE}Starting HermitSDR...${NC}"
docker compose up -d

# ─── Determine listening port ───
# Match HERMITSDR_PORT from docker-compose.yml, fall back to 5000
PORT=$(grep -E '^\s*-?\s*HERMITSDR_PORT' docker-compose.yml 2>/dev/null | \
       head -n 1 | sed -E 's/.*=\s*([0-9]+).*/\1/' || true)
PORT=${PORT:-5000}

# ─── Determine bind IPs (host network mode → all host IPs apply) ───
HOST_IPS=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' || true)
PRIMARY_IP=$(echo "$HOST_IPS" | head -n 1)
if [ -z "${PRIMARY_IP:-}" ]; then
    PRIMARY_IP="<host-ip>"
fi

# ─── Wait for health ───
echo ""
echo "${DIM}Waiting for HermitSDR to respond...${NC}"
HEALTHY=0
for i in $(seq 1 30); do
    if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/version" >/dev/null 2>&1; then
        HEALTHY=1
        break
    fi
    sleep 1
done

VERSION=""
if [ "$HEALTHY" = "1" ]; then
    VERSION=$(curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/version" 2>/dev/null | \
              sed -E 's/.*"version"\s*:\s*"([^"]+)".*/\1/' || echo "")
fi

# ─── Post-install summary ───
echo ""
echo "${BOLD}============================================================${NC}"
if [ "$HEALTHY" = "1" ]; then
    echo "${BOLD}${GREEN}  HermitSDR is RUNNING${NC}${BOLD} ${VERSION:+(v${VERSION}) }${NC}"
else
    echo "${BOLD}${YELLOW}  HermitSDR is starting (API not yet responsive)${NC}"
fi
echo "${BOLD}============================================================${NC}"
echo ""
echo "  ${BOLD}Listening on:${NC}  0.0.0.0:${PORT}  (host network mode)"
echo ""
echo "  ${BOLD}Web UI:${NC}"
echo "    ${GREEN}→ http://${PRIMARY_IP}:${PORT}${NC}"
if [ "$(echo "$HOST_IPS" | wc -l)" -gt 1 ]; then
    echo "${DIM}    Alternative addresses on this host:${NC}"
    echo "$HOST_IPS" | tail -n +2 | while read -r ip; do
        echo "${DIM}      http://${ip}:${PORT}${NC}"
    done
fi
echo "${DIM}      http://localhost:${PORT}   (on this machine)${NC}"
echo ""
echo "  ${BOLD}HL2 discovery:${NC}  UDP :1024 (broadcast + directed)"
echo ""
echo "  ${BOLD}Common commands:${NC}"
echo "    docker compose logs -f hermitsdr      # follow logs"
echo "    docker compose restart hermitsdr      # restart"
echo "    docker compose down                   # stop & remove"
echo "    docker compose ps                     # status"
echo ""

if [ "$HEALTHY" != "1" ]; then
    echo "${YELLOW}  Heads-up:${NC} the container started but /api/version didn't"
    echo "  respond within 30 seconds. Check logs:"
    echo "    docker compose logs --tail=50 hermitsdr"
    echo ""
    exit 0
fi
