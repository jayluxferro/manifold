#!/usr/bin/env bash
# Kill any process bound to a port listed in manifold.yaml.
# Defaults to ./manifold.yaml; override with MANIFOLD_CONFIG env var or first arg.
set -euo pipefail

CONFIG="${1:-${MANIFOLD_CONFIG:-manifold.yaml}}"

if [ ! -f "$CONFIG" ]; then
    echo "Config not found: $CONFIG" >&2
    exit 1
fi

# Extract all port numbers (gateway + pipeline) using Python (pyyaml is
# already in the project's uv environment).
ports=$(uv run python -c "
import yaml, sys
with open('$CONFIG') as f:
    cfg = yaml.safe_load(f) or {}
gw = (cfg.get('gateway') or {}).get('port')
ports = []
if gw:
    ports.append(int(gw))
for svc in cfg.get('pipeline') or []:
    p = svc.get('port')
    if p is not None:
        ports.append(int(p))
print(' '.join(str(p) for p in ports))
")

if [ -z "$ports" ]; then
    echo "No ports found in $CONFIG" >&2
    exit 0
fi

echo "Ports from $CONFIG: $ports"
echo

killed=0
for port in $ports; do
    pids=$(lsof -ti TCP:"$port" -s TCP:LISTEN 2>/dev/null || true)
    if [ -z "$pids" ]; then
        echo "Port $port: nothing listening"
        continue
    fi
    for pid in $pids; do
        cmd=$(ps -p "$pid" -o comm= 2>/dev/null || echo "unknown")
        echo "Port $port: killing pid $pid ($cmd)"
        kill "$pid" 2>/dev/null || true
        killed=$((killed + 1))
    done
done

# Give processes a moment to die, then force-kill survivors
if [ "$killed" -gt 0 ]; then
    sleep 0.5
    for port in $ports; do
        pids=$(lsof -ti TCP:"$port" -s TCP:LISTEN 2>/dev/null || true)
        for pid in $pids; do
            echo "Port $port: force-killing pid $pid"
            kill -9 "$pid" 2>/dev/null || true
        done
    done
    echo
    echo "Done — $killed process(es) terminated."
else
    echo "Nothing to kill."
fi
