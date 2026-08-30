# SPEC — Shared Service Layer & Reliable Teardown

Manifold chains LLM proxy services into a pipeline: it patches each service's
upstream, spawns them as subprocesses, and reverse-proxies agent traffic to the
first service. This SPEC covers two changes: (a) a registry/lease layer that
lets multiple gateways **share** one set of running service processes instead
of duplicating the whole chain, and (b) a `manifold down` rewrite that makes
teardown work even when the gateway is dead or hung.

## 1. Root cause

Two design defects in the pre-registry Manifold:

1. **`--port` delta-offset duplicates the whole chain.** Every `manifold up
   --port 9001` (same config, different gateway port) offset every service port
   by the delta and spawned a second full copy of every service. The services
   are stateless per request — only the gateway port matters — so this is pure
   waste and does not scale to multiple agents/gateways.
2. **`manifold down` trusts the gateway's signal handler.** `down --port` only
   signalled the gateway pid and relied on its SIGTERM handler to kill
   children. If the gateway is dead (crash, SIGKILL, OOM) or hung, the service
   processes survive and their ports stay bound. There was no external,
   registry-based authority for who-owns-what, so nothing else could reap them.

## 2. Invariants I1–I7

- **I1 single writer**: only the entry's `owner_port` gateway may
  spawn/kill/restart/patch a service. Adopters are read-only: never spawn,
  never patch, never forward logs, never killpg, never restart.
- **I2 entry life**: entry written atomically immediately after spawn (before
  the health gate); removed only by owner-stop, by a reaper that verified
  owner-gateway-pid dead, or by a sweep that found the service pid dead.
- **I3 kill authority**: `owner_port`/`owner_pid` authoritative for kills; a
  lease identity list alone never authorizes killing a service another live
  lease still lists; reaps re-check surviving live leases at kill time.
- **I4 verified kill**: killpg only when
  `os.getpgid(entry["pid"]) == entry["pgid"]` (guards pid/pgid reuse); on
  mismatch skip the kill, remove entry only.
- **I5 platform**: single machine, POSIX killpg (win32 falls back to
  `os.kill(pid, SIGTERM)`, same limitation as today).
- **I6 sharing boundary**: adoption only on exact identity match.
- **I7 stop on last leave**.

## 3. Global constraints (non-negotiable)

1. **Single machine.** No network registry, no multi-host coordination — all
   state is local files under `~/.manifold/run/`.
2. **POSIX killpg** for teardown (`os.killpg(pgid, SIGTERM)`, SIGKILL after a
   grace window); win32 falls back to `os.kill(pid, SIGTERM)` — the same
   limitation Manifold already has on Windows (I5).
3. **Registry lives under `~/.manifold/run/`** — `services/<identity>.json`,
   `leases/gateway-<port>.json`, `locks/<identity>.lock` — derived **lazily at
   call time** from `paths.PID_DIR` so the existing test idiom
   `patch("manifold.paths.PID_DIR", tmp_path)` keeps working.
4. **Identity tuple is fixed**: `sha256(json.dumps([name, directory, port,
   upstream_url, resolve_command(svc, upstream_url)]))` where `upstream_url`
   includes `upstream_path` (already applied by `compute_upstreams`).
   Health/stats paths are deliberately excluded. Changing the tuple is a
   breaking change to sharing semantics (I6).
5. **`--port` semantics change is intentional**: in shared mode the gateway
   port override no longer delta-offsets service ports; a second gateway on the
   same config adopts the running services. Flagged in README as a deliberate
   behavior change; `--isolated` restores the old delta-offset behavior.
6. **`--isolated` escape hatch**: restores today's delta-offset full-duplicate
   behavior, still registry-tracked so teardown stays reliable.
7. **No git commits** during implementation; changes stay uncommitted for
   review. The final diff must be only in-scope files.

## 4. Work orders

Checklist, in implementation order (each item lands with its tests):

- [ ] **§1 `src/manifold/models.py`** — `ServiceState` gains
      `adopted: bool = False`, `pgid: int | None = None`,
      `identity: str | None = None`, `owner_port: int | None = None`.
- [ ] **§2 `src/manifold/registry.py` (NEW)** — module-attribute imports
      (`import manifold.process as process`, `import manifold.chain as chain`)
      so tests can patch helpers. Functions: `run_dir/services_dir/leases_dir/
      locks_dir`, `service_entry_path/lease_path/lock_path`, `pid_alive(pid)`
      (`os.kill(pid, 0)`; `ProcessLookupError`→False, `PermissionError`→True),
      `entry_is_live(entry)` (pid alive + `pid_alive(owner_pid)` on POSIX),
      `read/write/remove_service_entry` (write via existing
      `paths.atomic_write_text`; corrupt read → unlink + None),
      `list_service_entries`, `read/write/remove_lease`, `list_leases`,
      `live_other_leases(gw_port)`, `find_live_entry_by_port(port)`,
      `kill_entry_processes(entry, grace=2.0)` (SIGTERM → poll → SIGKILL per
      I4), `acquire_spawn_lock(identity)` (single non-blocking
      `O_CREAT|O_EXCL` attempt; `FileExistsError`→False),
      `release_spawn_lock`, `sweep_stale()` (1. remove dead entries; 2. kill+
      remove live entries whose owner pid is dead and lease-less; 3. remove
      leases with dead gateway pid).
- [ ] **§3 `src/manifold/process.py`** — `start_service`: after spawn, set
      `state.pgid = os.getpgid(proc.pid)` (guarded); reset
      `state.adopted = False` at entry. `stop_service`: new guard at top —
      `if state.adopted: status = STOPPED; pid = None; return` (never kill
      another gateway's process). Clear `pgid` after kill on the normal path.
      NEW `release_service(state)`: ownership handoff without kill — pop
      `_processes`/`_log_tasks` WITHOUT cancelling the forwarder tasks (keep
      pipes draining), status STOPPED, clear pid/pgid. `sync_kill_tracked_
      subprocesses` and atexit no longer see it.
- [ ] **§4 `src/manifold/health.py`** — `run_health_checks` and `health_loop`
      gain optional keyword
      `on_adopted_unhealthy: Callable[[ServiceState], Awaitable[None]] | None
      = None`; invoked once when an **adopted** service flips
      HEALTHY→UNHEALTHY (3 consecutive failures). Existing tests unaffected.
- [ ] **§5 `src/manifold/cli.py` — `up` path** — new options `up --isolated`;
      `_apply_port_override(cfg, port_override, isolated)` (shared mode →
      gateway port only; `isolated=True` → today's delta-offset);
      `_preflight_check(cfg, shared)` (shared-mode service-port-in-use OK only
      when a live entry with the matching identity exists; otherwise hard
      error naming the conflicting service/identity/owner; gateway port in use
      stays a hard error); `_plan_service(svc, upstream_url) -> ("adopt",
      entry) | ("promote", entry) | ("spawn", None) | ("error", msg)`;
      `_spawn_owned(state, upstream_url, identity, gw_port, gw_pid,
      reclaim_entry=None) -> bool` (retry loop ≤50 × 0.2s on
      `acquire_spawn_lock`; adopt-if-entry-appears while waiting; under lock:
      re-check, port-busy → `typer.Exit`, PROMOTE → kill + remove first,
      `patch_service_config` (owner duty, CONFIG_FILE only), `start_service`,
      write entry (owner_port/owner_pid); `finally: release_spawn_lock`);
      `_adopt_from_entry(state, entry, upstream_url)` (sets
      adopted/pid/pgid/owner_port/upstream_url, status STARTING, logs
      "Adopting running service 'X' (pid N, owned by gateway :P)");
      `_run_pipeline` restructure: (1) load + `_apply_port_override`;
      (2) `registry.sweep_stale()`; (3) preflight(shared); (4) `_handle_crash`
      gains `if state.adopted: return`; (5) `compute_upstreams` — **skip
      blanket `wire_pipeline` in shared mode** (adopters never patch);
      (6) plan loop (ADOPT/PROMOTE/SPAWN/ERROR→Exit(1)), collect identities;
      (7) write lease **before first spawn**, refresh after loop;
      (8) wrap `wait_for_services_ready` + uvicorn in try/finally → new
      `_shutdown_pipeline`; (9) `health_loop(..., on_adopted_unhealthy=
      _promote_adopted_wrapper)`, `watch_config(..., gw_port, gw_pid,
      isolated)`; (10) health/stats callbacks emit `adopted`/`owner_port`.
      Promotion rule `_promote_adopted(state, gw_port, gw_pid)`: entry missing
      → nothing; entry dead → remove; owner alive → warn "leaving recovery to
      owner", return; else reclaim via `_spawn_owned(reclaim_entry=entry)`.
      Concurrent promotions serialize on the spawn lock (loser adopts back).
      `_handle_crash._do_restart` fix: after restart's `start_service`,
      refresh `registry.write_service_entry` (new pid/pgid must replace the
      entry). `_shutdown_pipeline(pipeline, gw_port, gw_pid)` (graceful,
      ordered): snapshot `live_other_leases(gw_port)` → `still_needed`
      identity set; reverse-order per state: adopted → skip; owned+still_needed
      → transfer entry owner fields to surviving lease holder +
      `release_service` + log handoff; owned+not-needed → `stop_service` +
      `remove_service_entry`. Then `remove_lease(gw_port)` +
      `sync_kill_tracked_subprocesses()`.
- [ ] **§6 `src/manifold/cli.py` — `down` rewrite (`_down_one(port,
      config_path)`)** — (1) snapshot lease (identities + gw_pid from legacy
      pid file) BEFORE signalling; (2) gateway signal: SIGTERM if alive; poll
      up to 5.0s; SIGKILL + 0.5s if still alive (dead gateway → steps 3–5 are
      the whole teardown — the fix for defect 2); (3) direct reap by registry:
      for each snapshotted identity — surviving live lease → transfer entry
      owner fields; elif `entry["owner_port"] in (port, None)` →
      `kill_entry_processes` + remove entry; (4) owner_port sweep: kill+remove
      every entry with `owner_port == port` not in surviving identities
      (catches crash-mid-startup); (5) file cleanup: remove lease, pid file,
      port file; (6) legacy fallback (ONLY when no lease existed and nothing
      was reaped): lsof scan of config service ports, needs `--config`.
      `down --all`: `_down_one` over every discovered port (leases + legacy
      pid files), then final `sweep_stale()`. `status`: per-service annotation
      `registry: running (pid N, owner gateway :P)` when a live entry matches.
- [ ] **§7 `src/manifold/watcher.py`** — `watch_config`/`_apply_config_changes`
      gain optional `gw_port/gw_pid/isolated` kwargs (None → today's exact
      behavior). When set: removed adopted service → drop tracking only (no
      kill — `stop_service`'s guard is the safety net); new/enabled service →
      `_plan_service` + `_spawn_owned`; `needs_restart` on adopted →
      `release_service` + plan/spawn new identity; on owned →
      `restart_service` + entry refresh (remove old entry if identity
      changed); replace blanket `wire_pipeline` (watcher.py:93) with
      owner-only `patch_service_config` in shared mode; after any change
      rewrite the lease.
- [ ] **§8 `src/manifold/gateway.py` + `scripts/kill-ports.sh`** —
      `/_manifold/config`: add `adopted`/`owner_port` per service (same shape
      as the CLI health callback). `kill-ports.sh`: replace the blind lsof
      step with a wrapper around `manifold down --port <port> --config
      <config>` (which now includes the legacy lsof fallback); CLI contract
      unchanged; old scan kept as a fallback only when the manifold CLI is
      unavailable.
- [ ] **§9 Docs** — this SPEC, README (`--isolated`, `--all`, `--port`
      semantics change flagged), docs/ARCHITECTURE.md (registry section).
      Stale docs/SPEC.md left alone.

## 5. Test contract (existing idioms)

- **`tests/test_registry.py` (NEW)** — tmp_path PID_DIR patch: identity
  stability/difference per tuple field; entry/lease roundtrip; corrupt JSON →
  None + unlink; `pid_alive` via patched `os.kill`; `entry_is_live` both-pid
  rule; `kill_entry_processes` (SIGTERM→SIGKILL, getpgid mismatch → no
  killpg); `sweep_stale` (dead entry removed / orphan killed / leased
  untouched / dead lease removed); spawn lock exclusivity.
- **`tests/test_cli_registry.py` (NEW)** — CliRunner + patched PID_DIR + fake
  files under `tmp_path/run/`, `AsyncMock` for `manifold.process.start_
  service`: `_apply_port_override` shared vs isolated; shared preflight (same
  identity OK / different → Exit / gateway port → error); `_plan_service` all
  four outcomes; up-adopt (no `start_service` call, lease written); up-spawn
  (entry + lease written); up-promote (reclaim kill + respawn); lock
  contention → adopt; `_shutdown_pipeline` (a) owned+no-lease → stop+remove,
  (b) owned+surviving-lease → `release_service` + owner transfer, (c) adopted
  → untouched; `_promote_adopted` both branches; `down` hung-gateway reap +
  surviving-lease transfer + file cleanup; `down --all`; `down` legacy lsof
  branch.
- **`tests/test_process_shutdown.py` (extend)** — `start_service` sets pgid;
  `stop_service` adopted guard (no killpg); `release_service` pops without
  kill and keeps forwarder tasks.
- **`tests/test_health.py` (extend)** — callback fires once for adopted
  UNHEALTHY flip; not for owned; not on recovery.
- **`tests/test_watcher.py` (extend)** — shared-mode: adopted removal → no
  `stop_service`; new service → spawn flow; adopted restart on wiring change →
  release+spawn; lease rewritten. Legacy (no gw_port) tests untouched.
- **`tests/test_models.py` / `tests/test_gateway.py` (extend)** — new field
  defaults; `/_manifold/config` fields.
- The repo's existing test suite must still pass.

## 6. Validation procedure (manual, end-to-end)

1. `uv run pytest` green (coverage ≥55% branch gate) + `uv run ruff check .`
   and `uv run ruff format --check .`.
2. **Shared adoption**: `manifold up -c manifold.yaml` (:9000), then same
   config `--port 9001` → console shows "Adopting running service …" per
   service; `pgrep -f hivemind` count unchanged; both gateways proxy;
   `manifold status` shows owner annotations.
3. **Stop on last leave**: `down --port 9000` → services stay up (handoff
   log), :9001 still proxies; `down --port 9001` → all services stopped,
   `lsof -iTCP:<svc-port> -sTCP:LISTEN` empty, `~/.manifold/run/` empty.
4. **Hung/dead gateway**: `kill -9` a gateway, then `manifold down --port
   <it>` → services reaped by registry directly, ports freed.
5. **Racing ups**: two parallel `manifold up --port 9002` → one set of
   processes, one "Adopting", no port errors.
6. **Isolated**: `--port 9100 --isolated` → delta-offset ports, full
   duplicate; `down` cleans all.
7. **Hot-reload**: with a shared chain, disable/re-enable a service → no other
   gateway's processes killed; re-enable re-adopts.
8. **Legacy**: pid file only (no lease) → `down` still works via signal +
   lsof fallback.
9. **Live chains**: kimi/anthropic-new configs already use unique per-config
   service ports, so shared-mode `--port` is behavior-compatible for them.

## 7. Cutover (additive)

1. **Additive by design.** The registry adds new files under
   `~/.manifold/run/`; nothing existing is deleted or moved. Legacy pid files
   (`manifold-<port>.pid`/`.port`) and the lsof fallback keep working for
   pre-registry instances, so old gateways and old `down` invocations are
   unaffected.
2. **No live-chain restarts needed.** Running chains (e.g., the session's
   `:9000`) keep their processes and pid files; the registry only starts being
   written when a new-version `manifold up` runs.
3. **`--isolated` preserves old semantics** — delta-offset full duplication —
   so any workflow depending on the old `--port` behavior can pin it while
   migrating.
4. All changes land and validate while existing chains keep running.

## 8. Success metrics

- `manifold up --port 9001` on a live shared chain prints "Adopting running
  service …" for every service and spawns **zero** new service processes.
- Services survive `down --port 9000` while :9001 leases them; the last
  gateway's `down` frees every service port (`lsof` empty) and empties
  `~/.manifold/run/`.
- A SIGKILLed gateway's services are reaped by `down --port <it>` (registry
  reaping), no lsof stragglers.
- Two parallel `manifold up --port 9002` converge on one process set.
- Legacy pid-file-only instances still tear down via signal + lsof fallback.
- `uv run pytest` green (coverage ≥55% branch gate); `ruff check`/`format`
  clean; `git diff` touches only in-scope files.

## 9. Rollback

- Changes stay uncommitted until review: `git checkout -- .` per affected
  file. The registry is inert without new-version code, so reverting the code
  fully restores pre-registry behavior (old pid files + lsof teardown).
- Runtime rollback: `manifold down` (new version) cleans registry files; a
  leftover `~/.manifold/run/` tree is safe to delete by hand — it is derived
  state, and `sweep_stale()` tolerates a missing/empty tree.
- No live chains are restarted during the work, so no runtime rollback exists
  for running sessions.

## 10. Known limitations

1. **Pipe/EPIPE handoff note**: an exiting owner's forwarder tasks close pipe
   read-ends; a handed-off child's next write gets EPIPE/SIGPIPE. All current
   services are Python (`uv run`) and survive — logging swallows
   `BrokenPipeError`. Non-Python services may not; restart-based handoff is
   the fallback there.
2. **pid-reuse window**: between a process's death and `sweep_stale()`/reap,
   a new process may reuse the pid. Guarded by I4 (killpg only when
   `os.getpgid(pid) == entry["pgid"]`); the residual window is the same as
   pre-registry Manifold's pid-file handling.
3. **Single machine only** (I5): no multi-host sharing, no NFS-safe locking;
   the registry assumes one local filesystem.
4. **Registry as new failure domain**: mitigated by atomic writes, corrupt-
   tolerant reads, pid-alive verification before any kill, and the lsof legacy
   fallback in `manifold down`.
5. **`--port` semantics change**: breaking for delta-offset users; mitigated
   by `--isolated` + README documentation.
