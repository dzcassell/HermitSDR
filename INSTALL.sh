#!/usr/bin/env bash
set -euo pipefail

echo "=== HermitSDR Install ==="
echo "Building Docker image..."
docker compose build

echo ""
echo "Starting HermitSDR..."
docker compose up -d

echo ""
echo "=== HermitSDR is running ==="
echo "UI: http://$(hostname -I | awk '{print $1}'):5000"
echo "Logs: docker compose logs -f hermitsdr"
echo ""
