# Session objects and agents: examples

Examples of [`restate.sessions`](../python/restate/sessions/__init__.py),
virtual objects with a SQL database per key, and
[`restate.agents`](../python/restate/agents/__init__.py), AI agents in them.
They are the Python counterparts of
[sdk-sessions](https://github.com/restatedev/sdk-sessions)' tutorial and
examples. Each is a file that runs on its own.

## Run an example

From the repository's root, once (it builds the SDK, with its native
extension, and installs the `serve` extra the examples run on: uvicorn's
HTTP/2, on Python 3.11 or later):

```sh
uv sync --all-extras --all-packages
```

Then run an example, start Restate, and tell Restate where the example is:

```sh
uv run examples-agents/tutorial/01_object.py                                    # the example, on port 9080
npx @restatedev/restate-server                                                  # Restate, in another terminal
npx @restatedev/restate -y deployments register --force http://localhost:9080   # tell Restate where it is
curl localhost:8080/wallet/alice/hello
```

Session objects need Restate 1.8 or later. An agent's framework comes with its extra, and its model's provider with
its package: the comment at the top of each agent's file says which (`uv run
--extra langgraph --with langchain-openai ...`, say). Every example serves on port 9080 (or `PORT`): stop one before you start the
next, and register again. The comment at the top of each file lists the
commands to try it.

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

Part 3, agents in the wallet (a prompt, an assistant, its tools, a person in
the loop, a subagent), comes with the agents' framework integrations.

## Basic

| File                          | What                                            |
| ----------------------------- | ----------------------------------------------- |
| [`notes.py`](basic/notes.py)  | SQL in a virtual object, with `ctx.db`, and no agent |
| [`langgraph_agent.py`](basic/langgraph_agent.py) | A LangGraph agent: its checkpoints in the key's database |
| [`pydantic_ai_agent.py`](basic/pydantic_ai_agent.py) | A Pydantic AI agent: its conversation in the key's database |

## Tips

- Restate's UI, at <http://localhost:9070>, shows every request and what it
  did, including the retries of lesson 06.
- To start over with empty wallets, stop Restate and delete the
  `restate-data` directory it created.
- `tests/examples_agents.py` runs each example's commands against a real
  Restate server.
