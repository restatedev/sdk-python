# Embedded relay receiver (Python) — implementation plan

**Status:** **landed** (loopback-over-Hypercorn). **Date:** 2026-08-12.
**Scope:** the Python SDK gains an embedded relay-tunnel *receiver*, reusing the
same Rust engine as the Java (FFM) and TypeScript (napi) SDKs
(`restate-sdk-shared-core`, branch `relay`, `tunnel` feature —
`relay::{Config, Engine, Handle}`). Because the Python SDK is already a
**PyO3/maturin** native extension (`restate._internal`), the binding is a small
`#[pyclass]`; there is no new packaging machinery.

## What shipped (v1: loopback over Hypercorn)

Mirrors Java/TS: the native engine dials the relay on its own tokio runtime and
bridges each forwarded request over a **loopback socket** into a local HTTP/2
(h2c) server hosting the SDK's ASGI app. The PyO3 boundary carries only control
(start / status / stop); no per-request data crosses it, so dispatch — and thus
**both the promise API and the codegen SDK** — is untouched.

- **Rust** (`src/lib.rs`): `#[pyclass] RelayTunnel` — `start(config_json) -> RelayTunnel`,
  `status() -> str`, `stop()` — over `relay::{Config, Engine, Handle}`. Registered
  in the `_internal` module. `stop()` releases the GIL while the runtime drains
  (the engine never calls back into Python, so no deadlock). `Cargo.toml` repoints
  `restate-sdk-shared-core` to the `relay` branch with the `tunnel` feature added
  (this also moves the VM core 7.0.1 → 7.0.2, same as the Java branch).
- **Python** (`python/restate/tunnel.py`): `serve_tunnel(services, *, relay, identity_keys=None, protocol=None)`
  builds the ASGI app via `restate.app(...)`, boots Hypercorn (h2c) on
  `127.0.0.1:0` in a background thread (mirrors `harness.py`), waits until it is
  listening, then starts `RelayTunnel` pointed at that port. Returns a
  `TunnelHandle` (`status()`, `stop()`, context manager). New `tunnel = ["hypercorn"]`
  extra.

**Why Hypercorn h2c is low-risk here:** prior-knowledge h2c is exactly how Restate
talks to the Python SDK in production (the examples bind plain `0.0.0.0:9080` and
Restate connects h2c). The relay engine's loopback dial is the same shape.

### Verification

- `cargo build` (relay branch + tunnel feature) compiles; the existing VM code
  is unaffected by 7.0.1 → 7.0.2. `cargo fmt`/`clippy` clean; `ruff` clean.
- `maturin develop --extras tunnel` builds + installs the extension.
- Lifecycle smoke test: `serve_tunnel` boots Hypercorn on the loopback; the
  tunnel starts (status `running=True`; the receiver harmlessly retries a dead
  relay); `GET /health` on the loopback returns `200 {"status":"ok"}` — proving
  Hypercorn serves the SDK ASGI app on the loopback; `stop()` tears it down.
- The full forwarded round-trip through the engine is proven cross-language in
  shared-core's `tests/relay_loopback.rs` (identical engine) and the TS
  round-trip. A live run against a real relay is the remaining operator-time step.

## Follow-up: the tunnel *is* the ASGI server (no Hypercorn, no socket)

The native next step. ASGI is just `async def app(scope, receive, send)` — a
conventional ASGI *server* (Hypercorn) exists only to translate HTTP into that
call. But we don't have HTTP; we have the tunnel. So the tunnel can **be** the
ASGI server: for each forwarded invocation, build a `scope` and drive
`app(scope, receive, send)` directly, feeding `receive`/`send` from the relay
stream. This removes Hypercorn, the loopback socket, and the port.

Grounded design notes:

- Swap the shared-core `LoopbackHandler` for a Python-specific `InvokeHandler`
  (in the PyO3 crate) that, per `Invocation`, drives the Python ASGI app instead
  of dialing a socket. Keep using shared-core's `receiver`/`bridge`/`protocol`
  (dial-in, role-flip, `/whoami`, redial, multi-homing) — only the handler
  changes. `relay::receiver::{Receiver, ReceiverConfig, InvokeHandler, Invocation}`
  are public, so no shared-core change is needed; replicate the small
  runtime-build wiring from `relay::loopback` with the ASGI handler.
- **Scope:** `{"type":"http", "http_version":"2", "method":..., "path": tail,
  "headers":..., ...}`. Setting `http_version="2"` makes the SDK negotiate **bidi**
  mode (`server.py send_discovery`: `!= "1.1"` → bidi) — correct, since the relay
  connection is HTTP/2. `scope["path"]` is parsed tail-only (`server.py parse_path`,
  `rsplit("/",4)`), so the forwarded tail works.
- **receive/send bridge:** `receive()` pulls chunks off the relay request stream
  (`Invocation.body`, an h2 `RecvStream`) → `{"type":"http.request","body":...,"more_body":...}`;
  `send()` writes `http.response.start` / `http.response.body` to the relay
  response (`Invocation.respond`). This crosses tokio ↔ asyncio, so it needs
  **`pyo3-async-runtimes`** (drive the Python coroutine + expose the h2 reads/writes
  as Python awaitables). Where the ASGI *message shaping* lives (Rust vs a thin
  pure-Python adapter the crate calls) is an implementation choice; a Python
  adapter keeps the fiddly dict-shaping Pythonic.
- **Full-duplex caveat (must honour):** the SDK closes its response side only
  after the client closes its request side (`server_context.py:616`, "some asgi
  servers remove the stream as soon as they see `more_body=False`"). The bridge
  must stay full-duplex and must not half-close early, or long-lived bidi
  invocations hang/truncate. (This is why v1 uses real HTTP/2 h2c; the direct
  driver must reproduce the same full-duplex semantics.)

This is the higher-risk build (cross-runtime streaming), which is why v1 ships
the proven loopback path first.

## Deferred

- The ASGI-direct driver above.
- Restate Cloud `/_/start-tunnel` mode (the engine is `/whoami`-only, same as
  Java/TS).
- Swap the shared-core git dependency for a crates.io release once the `tunnel`
  feature ships.
- Native-wheel CI already builds via `maturin-action` (manylinux/musllinux/macos);
  the `tunnel` feature adds tokio/h2/rustls(ring), which must cross-compile in
  those images — validate on the release matrix before publishing.
