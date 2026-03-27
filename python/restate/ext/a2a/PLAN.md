# A2A Integration for Restate Python SDK — v3

## Context

Adding an A2A integration that combines the best of both approaches:
- **Inside Restate**: A `TaskObject` (VirtualObject) manages task state, durability, and agent invocation
- **Outside Restate**: A thin FastAPI gateway handles A2A protocol (JSON-RPC, agent card catalog) and discovers agents via the Restate admin API

The gateway translates A2A JSON-RPC into Restate ingress HTTP calls. No `AgentExecutor`, no `DefaultRequestHandler`, no A2A server SDK needed on the gateway — it's a direct protocol translation.

## Architecture

```
A2A Client
  │
  ▼
FastAPI Gateway (outside Restate)
  │
  ├─ GET /.well-known/agent-card
  │     → queries Restate admin API for services with a2a metadata
  │     → builds and returns agent card catalog
  │
  ├─ POST /{agent}/a2a (JSON-RPC: message/send)
  │     → POST http://ingress/{agent}-task/{task_id}/handle_send_message_request
  │
  ├─ POST /{agent}/a2a (JSON-RPC: tasks/get)
  │     → POST http://ingress/{agent}-task/{task_id}/get_task
  │
  └─ POST /{agent}/a2a (JSON-RPC: tasks/cancel)
        → POST http://ingress/{agent}-task/{task_id}/get_invocation_id
        → PATCH http://admin/invocations/{id}/cancel
        → POST http://ingress/{agent}-task/{task_id}/get_task (poll for result)

Inside Restate:
  TaskObject (VirtualObject, keyed by task_id)
    ├─ handle_send_message_request (exclusive) — runs agent, manages task state
    ├─ get_task (shared) — returns task from K/V store
    ├─ get_invocation_id (shared) — returns in-flight invocation ID
    └─ cancel_task (exclusive) — marks task as canceled
```

## Agent Function Signature

```python
async def my_agent(query: str, context_id: str) -> AgentInvokeResult:
    ctx = restate_context()  # available via current_context()
    result = await ctx.run_typed("call_llm", llm.call, query)
    return AgentInvokeResult(parts=[TextPart(text=result)])
```

Runs within TaskObject's exclusive handler context — full access to `ctx.run_typed()`, service calls, etc.

## Agent Discovery via Metadata

Each `TaskObject` stores its agent card in service metadata:

```python
metadata={"a2a.agent_card": agent_card.model_dump_json()}
```

Service `description` is used as the agent description.

The gateway queries `GET http://admin:9070/services`, filters for services with `a2a.agent_card` metadata, and deserializes the agent cards.

## User-Facing API

### Restate side (agent definition):

```python
from restate.ext.a2a import A2ATaskObject, AgentInvokeResult
from a2a.types import AgentCard, AgentSkill, TextPart

async def weather_agent(query: str, context_id: str) -> AgentInvokeResult:
    ctx = restate_context()
    forecast = await ctx.run_typed("get_forecast", fetch_forecast, query)
    return AgentInvokeResult(parts=[TextPart(text=forecast)])

weather = A2ATaskObject(
    "weather",
    invoke_function=weather_agent,
    agent_card=AgentCard(
        name="Weather Agent",
        description="Provides weather forecasts",
        url="http://gateway:8000/weather/a2a",  # gateway URL
        version="1.0",
        skills=[AgentSkill(id="forecast", name="Forecast", description="...")],
        default_input_modes=["text"],
        default_output_modes=["text"],
    ),
)

# Standard restate app — TaskObject is just a VirtualObject
app = restate.app(services=[weather])
```

### Gateway side (separate process):

```python
from restate.ext.a2a import A2AGateway

gateway = A2AGateway(
    restate_admin_url="http://localhost:9070",
    restate_ingress_url="http://localhost:8080",
)
app = gateway.build()  # FastAPI app

# Run: uvicorn gateway:app --port 8000
```

The gateway auto-discovers all `A2ATaskObject` services from the admin API.

## Files to Create/Modify

### 1. `python/restate/ext/a2a/__init__.py` (new)

Exports:
- `A2ATaskObject` — VirtualObject with built-in task management
- `AgentInvokeResult` — result type for invoke_function
- `A2AGateway` — FastAPI gateway
- `restate_context()` / `restate_object_context()` — context helpers

### 2. `python/restate/ext/a2a/_models.py` (new)

```python
@dataclass
class AgentInvokeResult:
    parts: list[Part]
    require_user_input: bool = False

InvokeAgentType = Callable[[str, str], Awaitable[AgentInvokeResult]]
```

### 3. `python/restate/ext/a2a/_task.py` (new, based on reference)

**`TaskObject`** — VirtualObject keyed by `task_id`. Copied from reference with adjustments:

- `handle_send_message_request(ctx, request: SendMessageRequest) -> SendMessageResponse`
  - Generates context_id if missing
  - Stores invocation ID for cancellation
  - Upserts task in K/V store
  - Calls `invoke_function(query, context_id)`
  - Updates task to completed/input_required/failed/canceled
- `get_task(ctx) -> Task | None` (shared)
- `get_invocation_id(ctx) -> str | None` (shared)
- `cancel_task(ctx, request) -> CancelTaskResponse` (exclusive)
- `update_store(ctx, state, ...) -> Task` (exclusive)
- `upsert_task(ctx, params) -> Task` (exclusive)

**`A2ATaskObject`** — wrapper that creates a `TaskObject` with agent card stored in metadata:

```python
class A2ATaskObject:
    def __init__(self, name, invoke_function, agent_card):
        self._task_object = TaskObject(
            f"{name}",
            invoke_function,
        )
        # Store agent card in metadata for discovery
        self._task_object.metadata = {"a2a.agent_card": agent_card.model_dump_json()}
        self._task_object.description = agent_card.description
```

Exposes the same interface as VirtualObject so it can be passed to `restate.app()`.

### 4. `python/restate/ext/a2a/_gateway.py` (new)

**`A2AGateway`** — FastAPI app builder:

- Constructor: `(restate_admin_url, restate_ingress_url)`
- `build()` → FastAPI app with:
  - `GET /.well-known/agent-card` — returns agent card(s) from admin API discovery
  - `POST /{agent_name}/a2a` — JSON-RPC dispatch endpoint per agent
- Agent discovery: queries `GET {admin_url}/services`, filters by `a2a.agent_card` metadata
- JSON-RPC dispatch:
  - `message/send` → `POST {ingress_url}/{agent_name}/{task_id}/handle_send_message_request`
  - `tasks/get` → `POST {ingress_url}/{agent_name}/{task_id}/get_task`
  - `tasks/cancel`:
    1. `POST {ingress_url}/{agent_name}/{task_id}/get_invocation_id`
    2. `PATCH {admin_url}/invocations/{id}/cancel`
    3. Poll `get_task` for final state
  - Other methods → return appropriate JSON-RPC error responses

Uses `httpx.AsyncClient` for all HTTP calls. Lifespan manages the client.

### 5. `pyproject.toml` (modify)
Add: `a2a = ["a2a-sdk", "fastapi", "httpx[http2]"]`

## Key Design Details

### Ingress URL patterns (VirtualObject)
- `POST http://ingress/{service}/{key}/{handler}` — blocking call
- `POST http://ingress/{service}/{key}/{handler}/send` — returns invocation ID immediately
- Request body: JSON-serialized handler input
- Response: JSON-serialized handler output
- Header `x-restate-id`: invocation ID

### Admin API patterns
- `GET http://admin/services` — list all services with metadata
- `PATCH http://admin/invocations/{id}/cancel` — cancel in-flight invocation

### Serialization
A2A types are Pydantic models → Restate's PydanticJsonSerde handles them correctly. The gateway serializes/deserializes using the same Pydantic models.

### Agent Card in Metadata
```python
metadata = {"a2a.agent_card": agent_card.model_dump_json()}
```
Gateway deserializes: `AgentCard.model_validate_json(metadata["a2a.agent_card"])`

### Task ID
- If the A2A client provides a `task_id` in the message → use it as VirtualObject key
- If not → gateway generates a UUID and uses it as the key

### Multi-agent Support
- Each agent is a separate `A2ATaskObject` (VirtualObject)
- Gateway discovers all of them via admin API
- Each agent gets its own endpoint: `POST /{agent_name}/a2a`
- Agent cards include their specific URL

## Cancellation Flow (detailed)

1. A2A client sends `tasks/cancel` JSON-RPC request
2. Gateway receives it, extracts `task_id` and `agent_name`
3. Gateway calls `POST {ingress}/{agent_name}/{task_id}/get_invocation_id`
4. If invocation_id exists:
   a. `PATCH {admin}/invocations/{invocation_id}/cancel`
   b. The running handler catches TerminalError(409, "cancelled"), updates task to canceled
   c. Gateway calls `POST {ingress}/{agent_name}/{task_id}/get_task` to get final state
5. If no invocation_id (task already completed):
   a. Gateway calls `POST {ingress}/{agent_name}/{task_id}/cancel_task`

## v1 Limitations
- No streaming support
- No push notifications
- No resubscribe support
- No authenticated extended card
- Gateway discovery is one-shot on startup (could add polling later)

## Verification
1. Create a simple echo agent with `A2ATaskObject`
2. Run restate app: `restate.app(services=[echo_agent])`
3. Run gateway: `uvicorn gateway:app`
4. Send JSON-RPC `message/send` via curl
5. Send `tasks/get` to retrieve task
6. Test multi-turn with same task_id
7. Test cancellation
8. Verify agent card discovery at `/.well-known/agent-card`
