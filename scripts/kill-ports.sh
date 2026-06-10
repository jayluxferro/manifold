#!/usr/bin/env bash
# Kill a manifold pipeline — copy-paste your `manifold up` args and it works.
#
#   ./scripts/kill-ports.sh --config manifold-anthropic-new.yaml --port 12345
#   ./scripts/kill-ports.sh -c manifold.yaml
#   ./scripts/kill-ports.sh manifold-anthropic-new.yaml --port 9000
#
# Strategy (in order):
#   1. Find the gateway PID file in ~/.manifold/ and kill the whole process
#      group.  Since manifold uses killpg on Unix, one group-kill cleans up
#      the gateway AND every pipeline child process — even when manifold
#      reassigned ports at runtime.
#   2. Fall back to port-scanning from the YAML config for any stragglers.
set -euo pipefail

PID_DIR="$HOME/.manifold"
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
        -*) shift ;;
        *)
            if [ -f "$1" ]; then CONFIG="$1"; fi
            shift ;;
    esac
done

killed=0

# ── Step 1: kill via gateway PID file (covers ALL services in one shot) ──────

# Resolve the gateway port: CLI override > config file > assume default 9000
if [ -z "$GW_PORT" ] && [ -f "$CONFIG" ]; then
    GW_PORT=$(uv run python -c "
import yaml
with open('$CONFIG') as f:
    gw = (yaml.safe_load(f) or {}).get('gateway') or {}
print(gw.get('port', ''))
" 2>/dev/null || true)
fi
GW_PORT="${GW_PORT:-9000}"

PID_FILE="$PID_DIR/manifold-${GW_PORT}.pid"

if [ -f "$PID_FILE" ]; then
    GW_PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "$GW_PID" ] && kill -0 "$GW_PID" 2>/dev/null; then
        echo "Gateway PID: $GW_PID (port $GW_PORT) → killing process group"
        kill -TERM -- -"$GW_PID" 2>/dev/null || true
        sleep 0.5
        if kill -0 "$GW_PID" 2>/dev/null; then
            echo "Process group survived SIGTERM → force-kill"
            kill -KILL -- -"$GW_PID" 2>/dev/null || true
        fi
        killed=1
        rm -f "$PID_FILE"
        port_file="$PID_DIR/manifold-${GW_PORT}.port"
        rm -f "$port_file"
    else
        rm -f "$PID_FILE"
    fi
fi

# ── Step 2: fall back to YAML port scan for any stragglers ───────────────────

if [ ! -f "$CONFIG" ]; then
    if [ "$killed" -gt 0 ]; then
        echo "Done."
    else
        echo "No PID file at $PID_FILE and no config at $CONFIG"
        echo "Try: ./scripts/kill-ports.sh --config <your-config.yaml> --port <port>"
    fi
    exit 0
fi

ports=$(uv run python -c "
import yaml
with open('$CONFIG') as f:
    cfg = yaml.safe_load(f) or {}
result = []
for svc in cfg.get('pipeline') or []:
    p = svc.get('port')
    if p is not None:
        result.append(int(p))
print(' '.join(str(p) for p in result))
" 2>/dev/null || true)

stragglers=0
for port in $ports; do
    if [ "$port" = "$GW_PORT" ]; then continue; fi
    pids=$(lsof -ti TCP:"$port" -s TCP:LISTEN 2>/dev/null || true)
    for pid in $pids; do
        cmd=$(ps -p "$pid" -o comm= 2>/dev/null || echo "?")
        echo "Port $port: still alive (pid $pid, $cmd) → killing"
        kill "$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
        stragglers=$((stragglers + 1))
    done
done

if [ "$killed" -gt 0 ]; then
    echo "Done — pipeline killed via process group."
elif [ "$stragglers" -gt 0 ]; then
    echo "Done — $stragglers straggler(s) killed."
else
    echo "Nothing to kill."
fi
