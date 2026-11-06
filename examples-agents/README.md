# Session objects and agents: examples

Examples of [`restate.sessions`](../python/restate/sessions/__init__.py),
which gives each key of a virtual object its own SQL database, and of
[`restate.agents`](../python/restate/agents/__init__.py), AI agents built on
top of it. Each example is a single file that runs on its own.

## Run an example

The examples serve themselves with `restate.serve(restate.app([...]))`, on
Python 3.11 or later.

Install the SDK with the `serve` extra, which adds the HTTP/2 server that
`restate.serve` runs on:

```sh
pip install "restate-sdk[serve]"
```

Or, to run the examples against the SDK from this repository, sync it from
the repository's root once. This builds the SDK's native extension, so it
needs a Rust toolchain:

```sh
uv sync --all-extras --all-packages
```

Then run an example, start Restate, and tell Restate where the example is:

```sh
python examples-agents/tutorial/01_object.py                                    # the example, on port 9080 (or: uv run ...)
npx @restatedev/restate-server                                                  # Restate, in another terminal
npx @restatedev/restate -y deployments register --force http://localhost:9080   # tell Restate where it is
curl localhost:8080/wallet/alice/hello
```

Session objects need Restate 1.8 or later. Every example serves on port 9080
(or `PORT`), so stop one before you start the next, and register again. The
comment at the top of each file lists the commands to try it.

The agent examples also need their framework's extra and the package of their
model's provider; the comment at the top of each agent's file says which. For
example: `pip install "restate-sdk[serve,langgraph]" langchain-openai`, or
`uv run --extra langgraph --with langchain-openai ...` from source.

To talk with an agent from a terminal, as it works, run the agents' command
line (it needs the `client` extra: `pip install "restate-sdk[client]"`):

```sh
python -m restate.agents chat assistant/alice    # what you type goes to the agent; /help for the commands
```

It shows the conversation so far, then each message, tool call and wait as it
comes; `/signal approval approved` answers a wait, and Ctrl-D leaves.

Most handlers take and return JSON, so their requests use `curl --json`. An
agent's `chat` handler is the exception: it takes the message as plain text
(`curl -d 'Hi'`; a JSON string such as `"Hi"` works too), so that chatting
from a terminal needs no quoting. It answers with a JSON string.

## Tutorial: a wallet, from a database to agents

One application, built one idea per file: a wallet for every user. It starts
as a Restate virtual object, gets its own SQL database, and learns to move
money safely between wallets. Keep Restate running from one lesson to the
next: the wallets you filled in one lesson are still there in the next, as a
real application keeps its data across releases.

### Part 1 · A database per key

| Lesson                                       | You learn                                              |
| -------------------------------------------- | ------------------------------------------------------ |
| [`01_object.py`](tutorial/01_object.py)      | A virtual object: handlers that Restate calls by key   |
| [`02_database.py`](tutorial/02_database.py)  | `ctx.db`: every wallet has its own SQL database        |
| [`03_views.py`](tutorial/03_views.py)        | Views: reading the database while a handler is running |
| [`04_migrations.py`](tutorial/04_migrations.py)      | Changing the tables of databases that already exist    |

### Part 2 · Changes that happen once

| Lesson                                             | You learn                                                     |
| -------------------------------------------------- | ------------------------------------------------------------- |
| [`05_transaction.py`](tutorial/05_transaction.py)  | Transactions: spending fails as a whole, leaving no trace     |
| [`06_retries.py`](tutorial/06_retries.py)          | Retries run a handler again: an id makes a deposit count once |
| [`07_calls.py`](tutorial/07_calls.py)              | Calling another object, with an idempotency key               |
| [`08_sends.py`](tutorial/08_sends.py)              | Transfers: a send that is delivered only if the debit commits |

A third part, agents in the wallet, is not written yet. Until then, the
examples under [Basic](#basic) show each framework's agent, and those under
[Intermediate](#intermediate) put agents to work with Restate.

## Basic

| File                          | What                                            |
| ----------------------------- | ----------------------------------------------- |
| [`notes.py`](basic/notes.py)  | SQL in a virtual object, with `ctx.db`, and no agent |
| [`langgraph_agent.py`](basic/langgraph_agent.py) | A LangGraph agent: its conversation in the key's database |
| [`pydantic_ai_agent.py`](basic/pydantic_ai_agent.py) | A Pydantic AI agent: its conversation in the key's database |
| [`openai_agent.py`](basic/openai_agent.py) | An OpenAI Agents SDK agent: its conversation in the key's database |
| [`adk_agent.py`](basic/adk_agent.py) | A Google ADK agent: its session in the key's database |

## Intermediate

One of Restate's patterns at a time: agents whose tools are Restate itself,
a person in the loop, agents in workflows, and teams of agents on different
frameworks. A tool such as `call`, `send`, `workflow`, `subagent` or
`wait_for_signal` works with every framework.

| File                                            | What                                                                                                    |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| [`support.py`](intermediate/support.py)         | A support agent whose tools are Restate itself: a service call, a manager's approval (a signal) and a refund workflow |
| [`followup.py`](intermediate/followup.py)       | An agent that schedules a check-in for a day later: `send` with a delay                                 |
| [`team.py`](intermediate/team.py)               | A team on three frameworks: an editor (OpenAI) hands research to a researcher (LangGraph) and drafting to a writer (Pydantic AI), with `subagent` |
| [`router.py`](intermediate/router.py)           | A front desk of your own: a handler asks a model which team handles a message (`prompt`), then calls that team's agent |
| [`triage.py`](intermediate/triage.py)           | Agents as steps of a workflow: classify a ticket, draft a reply, and wait for a person's review         |
| [`refund_graph.py`](intermediate/refund_graph.py) | A refund desk you chat with, as a LangGraph graph of your own (`langgraph.agent(graph=...)`) whose steps are Restate's: ask a clerk and a policy agent, wait for a manager's approval, run the payout workflow (`langgraph.nodes`) |
| [`loop.py`](intermediate/loop.py)               | An agent loop of your own, with no framework: ask the model, run its tool calls, repeat; durable on the agents' conversation |

## Tips

- Restate's UI, at <http://localhost:9070>, shows every request and what it
  did, including the retries of lesson 06.
- To start over with empty wallets, stop Restate and delete the
  `restate-data` directory it created.
- `tests/examples_agents.py` runs each example's commands against a real
  Restate server.
