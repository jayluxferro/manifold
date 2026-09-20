"""Tests for the mid-chain TCP shim (manifold.shim).

Everything here runs real asyncio servers on loopback — the shim's whole job
is byte fidelity on live sockets, so mocking the transports would only test
the mocks.  Ports are allocated by binding to 0 and reading the assigned port,
so the suite never races a real service for a fixed port.
"""

from __future__ import annotations

import asyncio

import pytest

import manifold.shim as shim
from manifold.shim import all_shims, get_shim, start_shim, stop_all_shims, stop_shim


async def _tcp_server(port, handler) -> asyncio.Server:
    """Start a plain asyncio server running *handler* per connection."""
    return await asyncio.start_server(handler, "127.0.0.1", port)


async def _echo_handler(reader, writer) -> None:
    """Echo every byte back until the client's side closes."""
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def _free_port() -> int:
    """Ask the kernel for an unused loopback port (bind 0, read, close)."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


async def _roundtrip(
    shim_port: int, payload: bytes, *, half_close: bool = True
) -> bytes:
    """One client connection through the shim: write *payload*, read it all back.

    Half-closes after the payload by default (the request-then-response shape
    of real chain traffic) so the peer's EOF-driven handlers terminate.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", shim_port)
    writer.write(payload)
    await writer.drain()
    if half_close:
        writer.write_eof()
    chunks: list[bytes] = []
    while True:
        data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        if not data:
            break
        chunks.append(data)
    writer.close()
    return b"".join(chunks)


@pytest.fixture(autouse=True)
def _clean_shim_table():
    shim._shims.clear()
    yield
    shim._shims.clear()


@pytest.mark.asyncio
async def test_bidirectional_roundtrip_through_shim():
    target_port = await _free_port()
    target = await _tcp_server(target_port, _echo_handler)
    handle = await start_shim(await _free_port(), "127.0.0.1", target_port)
    try:
        result = await _roundtrip(handle.listen_port, b"hello through the shim")
        assert result == b"hello through the shim"
    finally:
        await stop_shim(handle)
        target.close()


@pytest.mark.asyncio
async def test_streaming_partial_writes_arrive_incrementally():
    """SSE-shaped flow: the target writes chunks with a gap between them.

    The assertion is timing, not content: the first chunk must arrive *before*
    the target's 0.5s pre-chunk-two sleep ends, so a read timeout shorter than
    that gap fails for any shim that buffers until upstream EOF.
    """

    async def _chunky_handler(reader, writer):
        writer.write(b"data: one\n\n")
        await writer.drain()
        await asyncio.sleep(0.5)
        writer.write(b"data: two\n\n")
        await writer.drain()
        writer.close()

    target_port = await _free_port()
    target = await _tcp_server(target_port, _chunky_handler)
    handle = await start_shim(await _free_port(), "127.0.0.1", target_port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", handle.listen_port)
        # 0.3s < the handler's 0.5s gap: only an incrementally-forwarding shim
        # can satisfy this read.
        chunk = await asyncio.wait_for(reader.read(4096), timeout=0.3)
        assert chunk == b"data: one\n\n"
        rest = await asyncio.wait_for(reader.read(4096), timeout=2.0)
        assert rest == b"data: two\n\n"
        writer.close()
    finally:
        await stop_shim(handle)
        target.close()


@pytest.mark.asyncio
async def test_concurrent_connections():
    target_port = await _free_port()
    target = await _tcp_server(target_port, _echo_handler)
    handle = await start_shim(await _free_port(), "127.0.0.1", target_port)
    try:
        payloads = [bytes([i]) * (i + 1) * 100 for i in range(8)]
        results = await asyncio.gather(
            *[_roundtrip(handle.listen_port, p) for p in payloads]
        )
        assert list(results) == payloads
    finally:
        await stop_shim(handle)
        target.close()


@pytest.mark.asyncio
async def test_half_close_propagation_keeps_response_flowing():
    """A client that finishes its request with FIN must still get the answer.

    This is the HTTP keep-alive shape: request body ends (EOF from the client
    side), the server responds afterwards.  A shim that closed the whole
    connection on first EOF would truncate exactly these responses.
    """

    async def _read_to_eof_then_reply(reader, writer):
        while True:
            data = await reader.read(4096)
            if not data:
                break
        writer.write(b"response after eof")
        await writer.drain()
        writer.close()

    target_port = await _free_port()
    target = await _tcp_server(target_port, _read_to_eof_then_reply)
    handle = await start_shim(await _free_port(), "127.0.0.1", target_port)
    try:
        result = await _roundtrip(handle.listen_port, b"request", half_close=True)
        assert result == b"response after eof"
    finally:
        await stop_shim(handle)
        target.close()


@pytest.mark.asyncio
async def test_target_unreachable_closes_client_side():
    """Dead target: the client's connection is closed (today's corpse contract)."""
    dead_port = await _free_port()  # nothing listening there
    handle = await start_shim(await _free_port(), "127.0.0.1", dead_port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", handle.listen_port)
        writer.write(b"going nowhere")
        await writer.drain()
        # The shim's connect fails → our side is closed.  Closing with our
        # still-unread bytes in flight sends RST, so accept either error form
        # a dead corpse port produces today: reset or clean EOF.
        with pytest.raises((ConnectionResetError, ConnectionError)):
            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            assert data == b""
        writer.close()
    finally:
        await stop_shim(handle)


@pytest.mark.asyncio
async def test_stop_shim_releases_the_port():
    port = await _free_port()
    target_port = await _free_port()
    target = await _tcp_server(target_port, _echo_handler)
    handle = await start_shim(port, "127.0.0.1", target_port)
    assert get_shim(port) is handle
    await stop_shim(handle)
    assert get_shim(port) is None
    assert all_shims() == {}
    # The port must be rebindable immediately — a real service retakes it here.
    reborn = await _tcp_server(port, _echo_handler)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"real service")
        assert await reader.read(4096) == b"real service"
        writer.close()
    finally:
        reborn.close()
        target.close()


@pytest.mark.asyncio
async def test_start_shim_rejects_conflicting_shim():
    target_port = await _free_port()
    port = await _free_port()
    handle = await start_shim(port, "127.0.0.1", target_port)
    try:
        with pytest.raises(RuntimeError):
            await start_shim(port, "127.0.0.1", target_port)
    finally:
        await stop_shim(handle)


@pytest.mark.asyncio
async def test_start_shim_port_in_use_raises_oserror():
    """A non-shim listener on the port surfaces as OSError (EADDRINUSE).

    Callers distinguish "already shimmed here" (RuntimeError) from "can't
    bind — something else owns the port" (OSError); the latter is the common
    hung-service case and must degrade to today's no-shim behavior.
    """
    listener = await _tcp_server(await _free_port(), _echo_handler)
    busy_port = listener.sockets[0].getsockname()[1]
    try:
        with pytest.raises(OSError):
            await start_shim(busy_port, "127.0.0.1", await _free_port())
        assert get_shim(busy_port) is None
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_stop_shim_drains_then_cancels_in_flight_connections():
    """stop_shim returns within the grace even with a connection held open."""
    gate = asyncio.Event()

    async def _holding_handler(reader, writer):
        # Never reads, never writes, just holds the socket until told.
        await gate.wait()
        writer.close()

    target_port = await _free_port()
    target = await _tcp_server(target_port, _holding_handler)
    handle = await start_shim(await _free_port(), "127.0.0.1", target_port)
    client_reader, client_writer = await asyncio.open_connection(
        "127.0.0.1", handle.listen_port
    )
    await asyncio.sleep(0.1)  # let the relay reach the target
    assert handle.active_connections == 1
    stop_task = asyncio.create_task(stop_shim(handle, grace=0.2))
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(stop_task, timeout=2.0)
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 1.5  # grace (0.2s) honoured, not the handler's endless read
    # The cancelled relay must have closed the accepted side.
    assert await asyncio.wait_for(client_reader.read(4096), timeout=2.0) == b""
    client_writer.close()
    gate.set()  # release the target handler so its task can exit
    target.close()


@pytest.mark.asyncio
async def test_stop_shim_for_port_and_stop_all():
    target_port = await _free_port()
    target = await _tcp_server(target_port, _echo_handler)
    h1 = await start_shim(await _free_port(), "127.0.0.1", target_port)
    h2 = await start_shim(await _free_port(), "127.0.0.1", target_port)
    assert await shim.stop_shim_for_port(h1.listen_port) is True
    assert await shim.stop_shim_for_port(h1.listen_port) is False  # already gone
    assert get_shim(h1.listen_port) is None
    assert get_shim(h2.listen_port) is h2
    await stop_all_shims()
    assert all_shims() == {}
    target.close()


@pytest.mark.asyncio
async def test_shim_target_for_observability():
    target_port = await _free_port()
    port = await _free_port()
    assert shim.shim_target(port) is None
    handle = await start_shim(port, "127.0.0.1", target_port)
    assert shim.shim_target(port) == f"127.0.0.1:{target_port}"
    await stop_shim(handle)
    assert shim.shim_target(port) is None
