#!/bin/bash
# Launch a collector task detached, so it survives closing this session.
set -euo pipefail

name="tg-scraper-collector-$(date +%Y%m%dT%H%M%S)"
cid=$(docker compose run -d --rm --name "$name" collector "$@")

echo "Started '${name}' (container ${cid:0:12}) — safe to close this session now."
echo "Follow live output:  docker logs -f ${name}"
echo "Log file (persists after the container exits): ./logs/collect-*.log"