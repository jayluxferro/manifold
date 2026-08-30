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
#   2. Lease-aware teardown via `manifold down`: the CLI reaps services
#      through the registry (~/.manifold/run/), signals the gateway, and
#      falls back to a lsof port scan itself for pre-registry instances.
#      The raw port scan below only runs if the manifold CLI is unavailable
#      (no uv, or the repo isn't synced).
set -euo pipefail

# Resolve the repo root from the script location so `uv run manifold` works
# regardless of the caller's working directory (scripts/../).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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

# ── Step 2: lease-aware teardown via `manifold down` ─────────────────────────
# `manifold down --port <port> --config <config>` is now the authoritative
# teardown path: it reaps service processes through the registry, transfers
# ownership to surviving gateways that still lease them (shared mode), and
# includes the legacy lsof scan for pre-registry instances.  We only fall
# back to the raw port scan here when the manifold CLI itself is unavailable.

scan_stragglers() {
    # Legacy fallback: blind lsof scan of the config's service ports.
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
}

manifold_ok=0
if command -v uv >/dev/null 2>&1; then
    if (cd "$REPO_ROOT" && uv run manifold down --help >/dev/null 2>&1); then
        manifold_ok=1
    fi
fi

if [ "$manifold_ok" -eq 1 ]; then
    down_args=(--port "$GW_PORT")
    # --config feeds `manifold down`'s internal legacy lsof fallback for
    # pre-registry instances; only pass it when the file actually exists.
    # Resolve it to an absolute path: the `manifold down` call runs from
    # $REPO_ROOT, so a relative path must keep meaning the caller's cwd.
    if [ -f "$CONFIG" ]; then
        CONFIG_ABS="$(cd "$(dirname "$CONFIG")" && pwd)/$(basename "$CONFIG")"
        down_args+=(--config "$CONFIG_ABS")
    fi
    echo "Running lease-aware teardown: manifold down ${down_args[*]}"
    if (cd "$REPO_ROOT" && uv run manifold down "${down_args[@]}"); then
        :  # down handled teardown (registry reaping + its own lsof fallback)
    else
        rc=$?
        echo "manifold down exited $rc — scanning ports for stragglers"
        scan_stragglers
    fi
else
    echo "manifold CLI unavailable — falling back to port scan"
    scan_stragglers
fi
