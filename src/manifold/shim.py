"""Minimal asyncio TCP shim — mid-chain traffic bypass for a dead service.

When a mid-chain service dies, the services *before* it keep POSTing to its
port (upstreams are baked at spawn), so their requests fail until the
auto-restart succeeds — 2-60s of backoff, forever if the crash loop never
recovers.  The entry hop doesn't have this problem (the gateway re-resolves
``get_entry_url`` per request), so this module exists for the mid-chain case
only.

A shim is a pure byte forwarder bound on the dead service's port: connections
arriving there are relayed to the next LIVE service in the chain.  There is
deliberately NO HTTP parsing — chain traffic is SSE and chunked bodies, and
any parsing would be a new way to corrupt it.  The dead layer's function is
lost while the shim is up (a shimmed redactor scrubs nothing, a shimmed rate
limiter limits nothing); the shim only keeps the chain *connected*.

Registry discipline (see SPEC-shared-pipeline.md): a shim is NOT a registry
service — no entry, no lease, no separate process.  It is ephemeral state of
the gateway process that created it (an asyncio server + tasks die with that
process), tracked in the module-level ``_shims`` table so shutdown, health
checks, and observability can find it.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time

log = logging.getLogger(__name__)

# In-flight connections get this long to finish on shutdown before they are
# cancelled — the same ~2s grace convention as service teardown.
DRAIN_GRACE = 2.0
# Bound on the target connect; a dead target must fail fast, not hang the
# accepted client for the OS-level connect timeout.
CONNECT_TIMEOUT = 5.0
_CHUNK = 64 * 1024


@dataclasses.dataclass
class ShimHandle:
    """One running shim: listener on *listen_port* → target host:port.

    ``pid_at_start`` records the dead service's process pid (when known) so
    the gateway can detect that someone respawned the real service and release
    the port back to it — see cli._shim_reconcile.
    """

    listen_port: int
    target_host: str
    target_port: int
    server: asyncio.Server | None = None
    pid_at_start: int | None = None
    # Monotonic birth time — the reconcile loop's TTL backstop releases any
    # shim that outlives every registry-based release signal.
    started_at: float = dataclasses.field(default_factory=time.monotonic)
    # One relay task per accepted connection (the task that runs _relay).
    _relays: set[asyncio.Task] = dataclasses.field(default_factory=set, repr=False)

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def active_connections(self) -> int:
        return sum(1 for t in self._relays if not t.done())


# Shimmed service port -> handle.  Gateway-local: never written to ~/.manifold,
# never shared across gateways (two gateways shimming the same port is
# impossible anyway — the second bind fails with EADDRINUSE).
_shims: dict[int, ShimHandle] = {}


def get_shim(port: int) -> ShimHandle | None:
    """The shim currently bound on *port*, if any."""
    return _shims.get(port)


def all_shims() -> dict[int, ShimHandle]:
    """Snapshot of the active shim table (port -> handle)."""
    return dict(_shims)


def shim_target(port: int) -> str | None:
    """``host:port`` the shim on *port* forwards to, for observability."""
    handle = _shims.get(port)
    if handle is None:
        return None
    return f"{handle.target_host}:{handle.target_port}"


async def start_shim(
    port: int,
    target_host: str,
    target_port: int,
    *,
    host: str = "127.0.0.1",
    pid_at_start: int | None = None,
) -> ShimHandle:
    """Bind *port* and forward every connection to target_host:target_port.

    Raises OSError when the port cannot be bound (in use, permissions) —
    callers treat that as "no shim, today's behavior stands" — and RuntimeError
    when a shim already manages *port* in this process.
    """
    if port in _shims:
        raise RuntimeError(f"A shim is already listening on port {port}")

    handle = ShimHandle(
        listen_port=port,
        target_host=target_host,
        target_port=target_port,
        pid_at_start=pid_at_start,
    )

    async def _on_connection(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        handle._relays.add(task)
        try:
            await _relay(handle, reader, writer)
        finally:
            handle._relays.discard(task)

    server = await asyncio.start_server(_on_connection, host, port)
    handle.server = server
    _shims[port] = handle
    log.info(
        "Shim listening on %s:%d → %s:%d (pure TCP forward, no HTTP parsing)",
        host,
        port,
        target_host,
        target_port,
    )
    return handle


async def _relay(
    handle: ShimHandle,
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    """Connect to the target and pump bytes both ways until both sides end."""
    try:
        try:
            target_reader, target_writer = await asyncio.wait_for(
                asyncio.open_connection(handle.target_host, handle.target_port),
                timeout=CONNECT_TIMEOUT,
            )
        except (OSError, TimeoutError) as exc:
            # Same client-visible contract as the corpse port today: the
            # request fails.  (One honest difference: with a shim the TCP
            # connect itself succeeds, so a client sees a close/EOF instead of
            # a connect refusal.)
            log.warning(
                "Shim on port %d: target %s:%d unreachable (%s) — closing client",
                handle.listen_port,
                handle.target_host,
                handle.target_port,
                type(exc).__name__,
            )
            with contextlib.suppress(Exception):
                client_writer.close()
            return

        to_target = asyncio.create_task(_pump(client_reader, target_writer))
        to_client = asyncio.create_task(_pump(target_reader, client_writer))
        try:
            await asyncio.gather(to_target, to_client, return_exceptions=True)
        finally:
            # Both directions ended (or the task was cancelled): full close.
            for w in (client_writer, target_writer):
                with contextlib.suppress(Exception):
                    w.close()
    finally:
        with contextlib.suppress(Exception):
            client_writer.close()


async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    """Copy src → dst until EOF or a transport error.

    EOF is propagated as a half-close (``write_eof``), NOT a full close: a
    keep-alive client that finished its request body must still receive the
    response, and an SSE response flows long after the request bytes end.
    """
    try:
        while True:
            data = await src.read(_CHUNK)
            if not data:
                with contextlib.suppress(OSError):
                    dst.write_eof()
                return
            dst.write(data)
            await dst.drain()
    except (ConnectionError, TimeoutError, OSError):
        # This direction's transport died; _relay tears down both sides once
        # both pump tasks finish.
        return


async def stop_shim(handle: ShimHandle, grace: float = DRAIN_GRACE) -> None:
    """Stop accepting, drain in-flight connections up to *grace*, deregister.

    Idempotent: closing an already-closed server and draining an empty relay
    set are no-ops.
    """
    if handle.server is not None:
        handle.server.close()
    relays = [t for t in handle._relays if not t.done()]
    if relays:
        _, pending = await asyncio.wait(relays, timeout=grace)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    if _shims.get(handle.listen_port) is handle:
        _shims.pop(handle.listen_port, None)
    log.info(
        "Shim on port %d stopped (was forwarding to %s:%d)",
        handle.listen_port,
        handle.target_host,
        handle.target_port,
    )


async def stop_shim_for_port(port: int, grace: float = DRAIN_GRACE) -> bool:
    """Stop the shim on *port* if this process runs one. True if it did."""
    handle = _shims.get(port)
    if handle is None:
        return False
    await stop_shim(handle, grace=grace)
    return True


async def stop_all_shims(grace: float = DRAIN_GRACE) -> None:
    """Stop every shim this gateway owns (called from pipeline teardown)."""
    for handle in list(_shims.values()):
        await stop_shim(handle, grace=grace)
