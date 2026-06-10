#!/usr/bin/env bash
# Kill any process bound to ports used by a manifold pipeline.
#
# Copy-paste the args you passed to `manifold up` and it works:
#   ./scripts/kill-ports.sh --config manifold-anthropic-new.yaml --port 12345
#   ./scripts/kill-ports.sh -c manifold.yaml
#   ./scripts/kill-ports.sh manifold-anthropic-new.yaml --port 9000
set -euo pipefail

CONFIG="manifold.yaml"
GW_PORT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -c|--config)
            CONFIG="$2"; shift 2 ;;
        --config=*)
            CONFIG="${1#*=}"; shift ;;
        -p|--port)
            GW_PORT="$2"; shift 2 ;;
        --port=*)
            GW_PORT="${1#*=}"; shift ;;
        -*) shift ;;  # skip other flags we don't care about
        *)
            # positional arg → config file (if it exists)
            if [ -f "$1" ]; then
                CONFIG="$1"
            fi
            shift ;;
    esac
done

if [ ! -f "$CONFIG" ]; then
    echo "Config not found: $CONFIG" >&2
    exit 1
fi

# Extract pipeline service ports from config; apply --port override for gateway.
ports=$(uv run python -c "
import yaml, sys
with open('$CONFIG') as f:
    cfg = yaml.safe_load(f) or {}

gw_port = ${GW_PORT:-None}
if gw_port is None:
    gw = (cfg.get('gateway') or {})
    gw_port = gw.get('port')

result = []
if gw_port is not None:
    result.append(int(gw_port))
for svc in cfg.get('pipeline') or []:
    p = svc.get('port')
    if p is not None:
        result.append(int(p))
print(' '.join(str(p) for p in result))
")

if [ -z "$ports" ]; then
    echo "No ports found in $CONFIG" >&2
    exit 0
fi

echo "Config:  $CONFIG"
if [ -n "$GW_PORT" ]; then
    echo "Gateway:  port $GW_PORT (CLI override)"
fi
echo "Ports:   $ports"
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

# Grace period, then force-kill survivors
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
