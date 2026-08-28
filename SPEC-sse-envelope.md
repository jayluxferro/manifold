# SPEC — SSE Envelope Fix Across the Manifold Chain

Incident: Claude Code prints "Streaming response ended before any complete data was
received. Retrying without streaming." when any downstream leg of the chain fails.

## 1. Root cause (validated 2026-08-27/28)

**Trigger:** `api.deepseek.com/anthropic` instability — connection resets
(`peer closed connection without sending complete message body`), throttling.

**Bug class:** 8 of 9 chain components commit `HTTP 200 + Content-Type:
text/event-stream` to the client before (or regardless of) knowing whether the
upstream actually has an event stream. A downstream 4xx/5xx or a connection
reset therefore reaches the client as an "SSE stream" containing raw JSON or
zero bytes — zero complete SSE events — which is exactly the reported symptom.

**Live validation (bad-key streaming request per port):** only hivemind :8765
returned `401 application/json`. Every other port returned
`200 text/event-stream` with the raw JSON error as body. Palisade is the first
converter for error-status failures; hivemind itself converts connection-reset
failures (empty 200 SSE via `StreamingResult.status_code` default 200).

## 2. The Envelope Invariant

A layer may transform request/response **bodies** per its purpose (redact,
score, compress, encode, throttle). It must never lie about the **envelope**:
status code, content-type, stream framing.

Concretely, every streaming path must implement two gates:

### Gate 1 — never commit to SSE before the upstream proves it has one

```
upstream = await client.send(req, stream=True)        # AWAIT upstream headers first
if upstream.status_code >= 400 or "text/event-stream" not in upstream.headers["content-type"]:
    body = await upstream.aread(); await upstream.aclose()
    return Response(body, status=upstream.status_code,   # plain response, faithful envelope
                    content_type=upstream.headers.get("content-type"),
                    headers=passthrough_minus_hop_by_hop)  # keep retry-after!
# only now:
return StreamingResponse(gen(), status=upstream.status_code, media_type="text/event-stream")
```

### Gate 2 — never let a committed stream end with zero complete frames

Once headers are committed the status can't change. On ANY mid-stream failure
(upstream reset, timeout, parse error), emit exactly one well-formed terminal
frame, then close:

- Anthropic `/v1/messages`: `event: error\ndata: {"error":{...}}\n\n`
- OpenAI `/v1/chat/completions`: `data: {"error":{...}}\n\n` then `data: [DONE]\n\n`

Zero-frame EOF is always a bug. (Anthropic's own API emits `event: error`
mid-stream on failures; clients surface it as a proper API error instead of
the "no complete data" retry loop.)

### Timeouts

Pre-commit wait: connect ≤ 10s. Streaming read: keep each layer's existing
generous per-read timeout (300–600s). No new total-time deadlines on streams.

## 3. Global constraints (non-negotiable)

1. **Never restart, kill, or signal any process on ports 77xx, 8765, or 9000.**
   This Claude Code session's own API traffic flows through the live chain.
   Code edits are safe (running processes hold loaded modules); process
   management is not.
2. **No git commits.** Work in-tree, leave changes uncommitted for review.
   Repos start clean; the final diff must be only your change.
3. **Happy-path behavior is frozen.** If a layer streams today, it must still
   stream incrementally (no new buffering). If it buffers by design (veritas),
   it keeps buffering. Intended transforms (redaction, compression, encoding,
   watermarking, scheduling) are byte-identical on the 2xx path.
4. Match each repo's existing code style and test conventions.
5. Each layer keeps its own ports/CLI/config surface unchanged.

## 4. Per-layer work orders

All repos resolve under both `/Users/jay/dev/ml/mcp/` and
`/Volumes/Lux/dev/ml/mcp/` (same inodes). Paths below use `/Users/jay/...`
except entropy-gate.

### 4.1 llm-redactor — `/Users/jay/dev/ml/mcp/llm-redactor`
- `src/llm_redactor/transport/cloud.py:99,196,226` — `raise_for_status()`
  inside streaming generators throws after headers committed.
- `src/llm_redactor/transport/http_proxy.py:266-319` (OpenAI path: no
  exception handling at all), `:484-505` (signed Anthropic path catches only
  `TimeoutException` — an `HTTPStatusError` escapes; log traceback observed).
- Fix: restructure all three streaming paths to open-stream-then-decide
  (Gate 1); wrap generators with Gate 2 terminal frames (OpenAI flavor for
  `/v1/chat/completions`, Anthropic flavor for `/v1/messages`).
- Preserve: redaction, raw surgery, `SSETextRestorer` restoration on the 2xx
  path byte-for-byte.

### 4.2 veritas — `/Users/jay/dev/ml/mcp/veritas`
- `src/veritas/server.py:375-381` + `:41-44` — httpx auto-decodes gzip but
  `_HOP_BY_HOP` doesn't include `content-encoding`, so a decoded body is
  forwarded with the original `Content-Encoding: gzip` header (downstream
  tries to gunzip plaintext → zero parseable events). Strip it.
- Status fidelity and the honest 502-on-error are correct — keep.
- Preserve: full-buffer architecture and fidelity scoring (SSE enforcement
  skip is deliberate).

### 4.3 lattice — `/Users/jay/dev/ml/mcp/lattice`
- `src/lattice/server.py:541` — forces `text/event-stream` when upstream
  omits content-type. Only label SSE when the upstream content-type confirms
  it; otherwise forward the actual content-type (or `application/octet-stream`).
- Status fidelity is good (has a test) — keep.
- Also: response bodies gained a `_watermark` field between local-splitter
  and lattice (observed live). Confirm watermarking never touches SSE event
  payloads on the streaming path; if it does, gate it off for SSE. Document
  the finding in the test file or a comment.

### 4.4 local-splitter — `/Users/jay/dev/ml/mcp/local-splitter`
- `src/local_splitter/transport/http_proxy.py:417-481` — tool-bearing
  requests (= all Claude Code traffic) use `_transparent_proxy` raw
  pass-through; `stream_and_close` (`:469-475`) has no error handling.
- Fix: Gate 1 in the transparent-proxy stream branch; Gate 2 terminal frame
  in `stream_and_close`.
- Preserve: T1 routing, fail-open local answering, cache, and the local
  path's SSE synthesis (already valid).

### 4.5 entropy-gate — `/Volumes/Lux/dev/ml/mcp/entropy-gate`
- `src/entropy_gate/proxy.py:321-361` — `_proxy_streaming` drops upstream
  status (`StreamingResponse` default 200); empty upstream error body →
  empty 200 SSE (`:346-350`).
- Fix: Gate 1. Its existing synthetic error event for mid-stream
  `httpx.HTTPError` (`:353-355`) already matches Gate 2 — keep, align format.
- Preserve: profiling/`--memory` bypass semantics on the streaming path.

### 4.6 strata — `/Users/jay/dev/ml/mcp/strata`
- `src/strata/server.py:54-59,77-81` — `_forward_stream` never inspects
  upstream status; `StreamingResponse` hardcodes 200 + SSE.
- Fix: open the upstream stream in the handler, Gate 1 check, then wrap;
  forward upstream status; keep `X-Strata-*` headers.
- Preserve: compression, graduation, ledger, breakpoints, controller —
  request-side processing untouched.

### 4.7 palisade — `/Users/jay/dev/ml/mcp/palisade` (poison point #1)
- `src/palisade/server.py:344-380` — `_stream_passthrough` hardcodes 200+SSE
  and relays upstream error bodies as SSE (`:366-368`, `:373-374`).
- `src/palisade/server.py:221-231` — for `stream:true` with response-side
  strategies it silently downgrades to `stream:false` and returns a
  `JSONResponse` to a streaming client (layers above then mislabel it SSE).
- Fix:
  (a) Gate 1 + Gate 2 in `_stream_passthrough`.
  (b) For `stream:true` with response-side strategies: forward with
      `stream:false`, buffer, decode/scrub as today, then **synthesize a
      valid Anthropic SSE lifecycle** from the final text:
      `message_start` → `content_block_start` → one `content_block_delta`
      (full decoded text) → `content_block_stop` → `message_delta`
      (`stop_reason: "end_turn"`) → `message_stop`. Follow the Anthropic
      Messages SSE schema (cf. local-splitter's `_anthropic_sse_generator`,
      `http_proxy.py:952-1016`). A streaming client then receives a complete,
      well-formed stream whose content is the decoded answer.
- Preserve: all request-side strategies, the `<200`-char passthrough rule,
  and non-streaming behavior.

### 4.8 hivemind — `/Users/jay/dev/ml/mcp/hivemind` (poison point #2)
- `src/hivemind/proxy/models.py:24` — `StreamingResult.status_code` defaults
  to 200; `src/hivemind/proxy/interceptor.py:213,266-268,309-314` — exception
  paths leave it 200; `src/hivemind/proxy/server.py:282-328` — first-chunk
  prefetch then wraps the empty failure in SSE.
- Fix: exception before first committed byte → set a real status
  (502 transport, 504 timeout) and return a plain JSON error response
  (reuse the existing `>=400` plain-Response path).
- Enhancement (in scope): allow `--max-retries` for streaming requests
  **only before the first byte is committed to the client** — i.e. retry on
  connect error / timeout-before-headers / upstream non-2xx. After commit:
  Gate 2 terminal frame, no retry.
- Preserve: AIMD, admission control, TPM/RPM limiting, budgets, circuit
  breaker, non-streaming behavior.

## 5. Test contract (every repo, in `tests/`, pytest)

- **A. Error status:** mock upstream returns 401 JSON; `stream:true` request
  → assert status 401, `application/json`, upstream body; assert never
  200+SSE.
- **B. Mid-stream reset:** mock sends 200 SSE headers + partial/garbage
  bytes, then closes → assert client receives ≥1 complete frame and the last
  frame is a terminal error frame; zero-frame EOF = test failure.
- **C. Happy path:** mock sends a real multi-event SSE stream with small
  delays → assert incremental delivery (first chunk arrives before upstream
  finishes) and content fidelity modulo the layer's intended transform.
- **D. Mislabel guard:** upstream returns 200 `application/json` for a
  `stream:true` request → layer must not label it `text/event-stream`.
  Exceptions by design: palisade (synthesizes valid SSE per §4.7b) and
  local-splitter's local-answer path (already synthesizes valid SSE).
- The repo's existing test suite must still pass.

## 6. Integration validation (post-fix, reviewer-run)

Hand-chain fixed services on scratch ports — **do not** run a second manifold
instance (state/log collision with the live one):

```
18889 llm-redactor → 18892 veritas → 18890 lattice → 18888 local-splitter
→ 18887 entropy-gate → 18893 strata → 18891 palisade → 18765 hivemind → deepseek
```

Scratch instances may briefly share service-local state (SQLite/ledgers)
with live ones — acceptable for a short read-mostly window; override via env
vars where a service supports it.

1. Bad-key loop against every scratch port → expect `401 application/json`
   everywhere.
2. Happy-path streaming request at :18889 → incremental SSE end-to-end.
3. Fault injection: kill a mid-chain scratch service mid-stream → client
   gets one terminal error frame, never zero-frame EOF.

## 7. Cutover (session-safe)

1. All fixes land and validate while the live chain keeps running untouched.
2. Start fixed chain + a second gateway on `:19000`; point NEW agent sessions
   at `:19000`.
3. This session stays on `:9000` until it ends; only then retire the old
   chain and remap ports. `manifold down` never runs while this session lives.

## 8. Success metrics

- 8/8 scratch ports + scratch gateway return 401 on the bad-key streaming test.
- 0 zero-frame EOFs under fault injection.
- Happy-path incremental streaming unchanged (per-layer architecture preserved).
- All repos: existing tests + new regression tests green; `git diff` touches
  only in-scope files.

## 9. Rollback

Changes stay uncommitted until review: `git checkout -- .` per repo. The live
chain is never restarted during the work, so no runtime rollback exists.
