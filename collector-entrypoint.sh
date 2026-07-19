#!/bin/bash
set -uo pipefail

mkdir -p /app/logs
log_file="/app/logs/collect-$(date +%Y%m%dT%H%M%S).log"
echo "Logging to ${log_file}"

tg-scraper "$@" 2>&1 | tee -a "$log_file"
exit "${PIPESTATUS[0]}"