# Access patterns: what a conversation asks of Restate

What the conversation of turns ([The conversation](conversation.md)) costs in
Restate state, compared with the single table it replaced (before #296):
what a turn reads and writes, warm and cold, what a client following the
conversation reads, which statements make up the cost, and what to change.
Measured with [`scripts/access_patterns.py`](../../scripts/access_patterns.py)
on `19fe21c` (before), `434bb6f` (after), and `869b5b8` (after, with the fix
of `last_of` that this measurement led to).

## How the database meets Restate

A key's database is a SQLite file in the object's state, stored by
sdk-shared-core's state layer
([its architecture](https://github.com/restatedev/sdk-shared-core/blob/main/src/state/docs/ARCHITECTURE.md)):

- **A commit is a flush:** every statement outside a transaction, and every
  transaction, puts a tail entry (its pages' changes) and the head, then
  syncs: two state writes and a run in the invocation's journal, and a round
  trip.
- **Now and then a flush checkpoints** (every 128 flushes, or sooner for a
  small database): a pack of the frames changed and a manifest, in 64 KiB
  parts, and deletes of the tail entries before it.
- **A warm open** (the process holds the state as of the head it reads) is
  one get: the head.
- **A cold open** (a new process, or another process flushed since) gets the
  head, then the manifest and *each tail entry, one key each*, in one round.
- **A cold page read** gets the 64 KiB part of a pack that holds its frame:
  about 60 KB per get, whichever page of it the SQL wanted. A b-tree descent
  is a round trip per level not yet read.
- **Every get is journaled** (protocol v7; the store's `get` says so in
  `restate/sessions/_session.py`), so what an invocation reads is written
  to its journal too.

So what a turn costs in Restate is decided by two things: how many commits
it makes (writes, syncs, journal entries), and, when it runs cold, which
pages its SQL touches and how they are spread over pack parts.

## The method

The script answers prompts through `answer_once` with the tests' made-up
framework (`tests/agents_conversation.py`) over the tests' fake context
(`tests/sessions_object.py`), and meters every `get`, `set`, `clear` and
`sync` of the key's database, and each SQL statement with the rows it
returned and the gets made while it ran (the turns here run one statement
at a time, so each get is charged to the right one).

- **A turn** is a prompt (150 characters), a reply with tool calls, their
  results, and an answer (600 characters). The text is words of English
  letter frequencies picked by Zipf's law: a block of 1,500 characters
  compresses (zlib) about 2.0×, where a block of these docs' prose does
  1.9×. Invocation ids, and so prompt ids, are random, as Restate's are.
- **Chat:** 2 tool calls of 1,500 characters a turn, 1,000 turns.
- **Heavy tools:** 8 tool calls of 4,000 characters a turn, 300 turns.
- **Warm:** all turns in one process, as a key answered by the same
  container turn after turn.
- **Cold:** the turn after turns 10, 50, 100, 200, 500 and 1,000, in a new
  process whose page cache is empty, as a key's first invocation in a new
  container. A client reading turn *n*, the latest, from the cursor before
  it (the `messages` view) is measured the same way, in its own process.
- **Probes:** single statements, each alone in a new process.
- The agent's defaults: `window=1`, `budget=32_000`, no summaries.

Not covered: the companion (`signals`), the call records' writes (no tool
here makes Restate calls), snapshots and `compact`, and a real server (see
[Open questions](#open-questions)).

## Results

### Writes: the same commits, slightly fewer bytes

Per turn, warm, averaged over the run:

| | Before | After |
| --- | --- | --- |
| Commits (flushes, syncs) | 6 | 6 |
| State writes (`set`) | 12.1 | 12.1 |
| Bytes written, chat | 8,761 | 7,844 (−10%) |
| of which tail entries | 4,474 | 3,605 (−19%) |
| of which packs, manifests, head | 4,287 | 4,239 |
| Bytes written, heavy tools | 36,915 | 35,383 (−4%) |
| Stored after 1,000 chat turns | 2.93 MB, 243 keys | 2.87 MB, 271 keys |

A turn commits its prompt, each step, and its end, as before: one flush
each. The new layout writes a little less per step: the single table had a
`UNIQUE` index on `prompt`, which took an entry (a `NULL`) for every step
too, where the steps' table has only its `seq` index. The turn's end is
still one `UPDATE`, of the turn's row now. Checkpoints cost the same.

### What a turn reads, warm: bounded

| Turn | Rows read, before | Rows read, after | Bytes of rows, before | after | Model input, before | after |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 11 | 50 | 16 | 49 KB | 13 KB | 51 items | 24 items |
| 101 | 500 | 106 | 492 KB | 90 KB | 501 | 204 |
| 201 | 1,000 | 198 | 984 KB | 168 KB | 1,001 | 344 |
| 501 | 2,500 | 198 | 2.46 MB | 168 KB | 2,501 | 344 |
| 1,000 | 4,995 | 198 | 4.92 MB | 169 KB | 4,996 | 344 |

Before, every turn read the whole conversation (`SELECT … ORDER BY n`) and
gave all of it to the model. After, a turn reads the transcript the budget
holds and the window's steps: flat from about turn 200 on. Warm, those
reads are memory, so in Restate a warm turn is one get, the head, in both
versions. Statements per turn went from 9 to 13–15 (the transcript is read
a page of 64 rows at a time).

### What a turn reads, cold: no better, until one statement is fixed

The turn after turn *n*, in a new process (reads in bytes, then round
trips):

| Turn | Before | After | After, with [the fix](#the-fix-last_of-from-the-ends-of-the-tables) |
| ---: | ---: | ---: | ---: |
| 11 | 26 KB, 5 | 28 KB, 4 | 28 KB, 4 |
| 51 | 145 KB, 8 | 113 KB, 7 | 113 KB, 7 |
| 101 | 234 KB, 10 | 235 KB, 9 | 235 KB, 9 |
| 201 | 501 KB, 14 | 456 KB, 13 | 441 KB, 12 |
| 501 | 1.29 MB, 29 | 1.24 MB, 27 | 576 KB, 12 |
| 1,001 | 2.83 MB, 63 | 2.77 MB, 55 | 900 KB, 19 |
| 301, heavy tools | 4.55 MB, 85 | 4.08 MB, 55 | 2.84 MB, 48 |

Before, the full load read the whole database. After, the bounded load
should read a small part of it, yet a cold turn still reads it all: one
statement does. Attributed by statement, the cold turn after turn 1,000
reads:

| Statement | Gets | Bytes |
| --- | ---: | ---: |
| `SELECT MAX(n), MAX(seq), MAX(answered_seq) FROM restate_agent_turns` (`last_of`) | 52 | 2.48 MB |
| the open (head, manifest, 79 tail entries) | 81 | 170 KB |
| `SELECT answer FROM restate_agent_turns WHERE prompt = ?` | 1 | 66 KB |
| `SELECT n, seq, summary, summarized FROM restate_agent_conversation_start` | 1 | 59 KB |

### The statements, each alone, cold

After turn 500, each in a new process, reads beyond the open:

| Statement | Gets | Bytes |
| --- | ---: | ---: |
| **After** | | |
| `SELECT MAX(n), MAX(seq), MAX(answered_seq) FROM turns` | 23 | 1.10 MB |
| the same as three scalar subqueries, `SELECT (SELECT MAX(n) FROM turns), …` | 2 | 116 KB |
| `SELECT MAX(n), MAX(seq) FROM items` | 4 | 221 KB |
| the same as two scalar subqueries | 1 | 59 KB |
| `SELECT COUNT(*) FROM items` (a scan of its smallest index, `seq`, for comparison) | 4 | 221 KB |
| the transcript's first page: `… FROM turns WHERE n >= ? AND n < ? ORDER BY n DESC LIMIT 64` | 2 | 113 KB |
| the window's steps: `… FROM items WHERE n > ? ORDER BY n` | 1 | 59 KB |
| `SELECT answer FROM turns WHERE prompt = ?` | 1 | 55 KB |
| `SELECT n, seq, summary, summarized FROM start` | 1 | 59 KB |
| the view, from a cursor: `SELECT MIN(n) FROM turns WHERE seq > ?` | 1 | 57 KB |
| the view, from a cursor: `SELECT MIN(turn) FROM items WHERE seq > ?` | 1 | 59 KB |
| **Before** | | |
| the full load, `SELECT … FROM conversation ORDER BY n` | 24 | 1.08 MB |
| `SELECT MAX(n), MAX(seq) FROM conversation` | 6 | 336 KB |
| the view, from a cursor: `SELECT MIN(n) FROM conversation WHERE seq > ?` | 24 | 1.08 MB |

**A `SELECT` of several `MAX`es is a full scan.** SQLite reads a single `MIN`
or `MAX` of an indexed column from the end of its b-tree, but only when it
is the query's one aggregate; turso, the engine here, measures the same. A
`SELECT` with more than one scans the table. `last_of` runs one such
`SELECT` per table at every turn: warm, it is CPU over the whole
conversation; cold, it reads every page. One scalar subquery per maximum
reads only the end of each b-tree. The `messages` view's latest page (no
cursor) calls `last_of` too.

### A client following the conversation

The `messages` view reading turn *n*, the latest, from the cursor before
it, alone in a new process:

| Turn | Before | After |
| ---: | ---: | ---: |
| 100 | 234 KB, 10 round trips | 127 KB, 5 |
| 500 | 1.29 MB, 29 | 250 KB, 6 |
| 1,000 | 2.83 MB, 64 | 426 KB, 8 |
| 300, heavy tools | 4.55 MB, 86 | 484 KB, 7 |

Before, `SELECT MIN(n) FROM conversation WHERE seq > ?` scanned the whole
table: the engine walks the rows in `n` order until one has a later `seq`,
which for a client that is nearly up to date is near the last row. After,
the view looks in the turns and in the items by their `seq` indexes, and
reads only the latest turn's rows. What remains is mostly the open.

Warm, the view costs the same in both versions, about 63 gets a call:
readers never use the process's state of the namespace (`src/state/mod.rs`
in sdk-shared-core skips the cache for them), so every view call gets the
head, the manifest and each tail entry again.

## Why the bounded reads still touch much of the file

Pages are allocated as rows are written, so the file holds the turns' rows
and their steps interleaved, in the order they were written:

```
db file, oldest first, 16 KiB frames in 64 KiB pack parts
| turn 1 | steps of 1 ... | turn 2 | steps of 2 ......... | turn 3 | ...
   ^                          ^                              ^
   the transcript's rows: one or two per turn, spread over every part
```

A turn row is about 1 KB; the steps written after it are 4 KB (chat) to
33 KB (heavy tools). The transcript that the budget holds, about 200 turn
rows, is spread over the last 200 turns of the file: the cold turn after
300 heavy turns, with the fix, still reads 2.84 MB, of which the
transcript's pages are 2.23 MB (38 gets), parts fetched for a few turn rows
each (360 KB in chat, after 1,000 turns). The window's steps, the latest
turn's rows and the view's reads are at the end of the file, together, and
cost a part or two.

## What got better, what didn't, and what got worse

**Better:**

- A turn's SQL work and the model's input are bounded (198 rows, 169 KB
  here), where the single table had a turn read, and the model get, the
  whole conversation: 4.9 MB of rows at turn 1,000.
- A client following the conversation cold reads 6.6× less at turn 1,000
  (9.4× with heavy tools), and the gap grows with the conversation.
- About 10% fewer bytes written per chat turn (19% fewer in tail entries).

**The same:** commits per turn (6), so state writes, syncs and journal entries
per turn; stored bytes; checkpoint cadence; a warm turn's Restate reads (the
head).

**Not realized until the fix:**

- **`last_of` scanned both tables** at every turn (several `MAX`es in one
  `SELECT`), so a cold turn read the whole database, as the full load did.
  Fixed in `869b5b8`: −67% bytes and −65% round trips for the cold turn
  after turn 1,000.

**A cost of the new layout:**

- **The transcript is spread over the file** among the steps, so the
  budget's transcript read cold costs a part per turn or two, growing with
  the steps per turn up to the budget.

Small, for now: the prompt's answer is looked up by the `UNIQUE` index on
random prompt ids, and the start row is one row on an early page. Each costs
one part, cold. The prompt index grows by about 40 bytes a turn, so its
random inserts touch few pages at these sizes.

## The fix: `last_of` from the ends of the tables

`869b5b8` asks for each maximum in a scalar subquery of its own, in one
statement:

```python
async def last_of(db: DatabaseReader) -> tuple[int | None, int]:
    # One subquery per maximum: each reads the end of its b-tree, where one SELECT of several scans the table.
    [[turn, turn_seq, answered_seq, step, step_seq]] = await db.values(
        f"SELECT (SELECT MAX(n) FROM {TURNS}), (SELECT MAX(seq) FROM {TURNS}), (SELECT MAX(answered_seq) FROM {TURNS}), "
        f"(SELECT MAX(n) FROM {STEPS}), (SELECT MAX(seq) FROM {STEPS})"
    )
```

Its test (`test_the_last_position_and_seq_are_read_from_the_ends_of_the_tables_without_a_scan`
in `tests/agents_conversation.py`) fills a key with 600 turns, reads it under
another key that no process has read (pack ranges are cached per
namespace), and counts the pack parts fetched: 3 for `last_of`, where the
`SELECT`s of several maxima fetched 11 and a scan of the rows 25. The
mutant `last-of-scans` in `scripts/resurrect.py` brings the scan back. The
sdk-sessions port should never write a `SELECT` of several aggregates
either.

## What to change next

In order of value for effort:

1. **Keep the transcript dense.** Options, to measure: the steps in a
   database of their own beside the key's (sessions name such databases,
   `database_name`), so that the turn rows fill their own pages; or a
   smaller budget for agents with heavy tools. Either makes the budget's
   transcript a few parts instead of one per turn.
2. **In sdk-shared-core (not this SDK's code, but they set every open's
   floor):**
   - a cold open gets each tail entry as a key of its own: up to about 120
     gets per open here, and the view pays it on every call, as readers
     never reuse the process's state;
   - a cold page read fetches 64 KiB and journals it (until protocol v8's
     ephemeral gets, which the store already plans to use).

## Open questions

- **Eager state.** Restate can send an object's state with the invocation
  (`StartMessage.state_map`). If it sends this object's whole state, every
  invocation receives the whole database (2.9 MB at turn 1,000 here)
  however little it reads, and the cold numbers above become a question of
  what the server sends rather than what the SQL reads. To measure against
  a real server.
- **The companion and the call records**, not exercised by this harness:
  their writes per turn, and the `restate_agent_calls` read at every turn.
- **Steering** adds no request to a turn that nobody steers: the companion
  answers the publishes a turn makes anyway (its `start`, and one after each
  write of steps that commits: a reply with tool calls, the tools' results)
  with the messages waiting for it (one state read more, of its inbox, which
  is empty then), and a turn takes them at a step with no call of its own;
  the `taken` that tells the companion is sent from the storing
  transaction's outbox. A steered message costs the `steer` view, one call
  to the companion (an inbox entry, a publish, and the placeholder sent to
  `steered`), the transaction that stores it (an item and a row of
  `restate_agent_steers`), and the placeholder's short invocation (open the
  database, one read by id). A later turn reads the steered messages of the
  transcript it loads as one range of that table per page; the summary
  view, one range. Measured on a real server with
  `scripts/steering_traffic.py` (a turn of one tool call): a turn nobody
  steers, 40 journal entries and 4 companion calls (`start`, which
  publishes; a `publish` after the reply with the call, and one after its
  result; `close`); a turn that takes a steered message, 50 entries and 5
  calls (the outbox's `taken`); the message itself, the `steer` view (5
  entries), the companion's `steer` (12), and the placeholder that finds it
  taken (3).
- **Snapshots and `compact`:** a snapshot replaces the previous one (one
  `DELETE` and one `INSERT` of the whole compacted history), and `compact`
  reads the transcript from the summary's start. Both are occasional, but
  their size grows with what the framework keeps.

## Reproducing

Every table above, from checkouts of the three commits (each needs the built
extension, `python/restate/_internal*.so`, copied into it):

```sh
git worktree add ../before 19fe21c && cp python/restate/_internal*.so ../before/python/restate/
git worktree add ../after 434bb6f && cp python/restate/_internal*.so ../after/python/restate/
run() { python scripts/access_patterns.py "$@"; }
CHAT="--turns 1000 --follow --cold-at 10,50,100,200,500,1000"
HEAVY="--turns 300 --calls 8 --result 4000 --follow --cold-at 100,300"
run --tree ../before $CHAT > before.jsonl && run --tree ../before $HEAVY > before-heavy.jsonl
run --tree ../after $CHAT > after.jsonl && run --tree ../after $HEAVY > after-heavy.jsonl
run --tree . $CHAT > fix.jsonl && run --tree . $HEAVY > fix-heavy.jsonl

# The statements, each alone in a new process after turn 500.
T=restate_agent_turns I=restate_agent_items C=restate_agent_conversation
run --tree ../after --turns 500 --cold-at 500 \
  --probe "SELECT MAX(n), MAX(seq), MAX(answered_seq) FROM $T" \
  --probe "SELECT (SELECT MAX(n) FROM $T), (SELECT MAX(seq) FROM $T), (SELECT MAX(answered_seq) FROM $T)" \
  --probe "SELECT MAX(n), MAX(seq) FROM $I" \
  --probe "SELECT (SELECT MAX(n) FROM $I), (SELECT MAX(seq) FROM $I)" \
  --probe "SELECT COUNT(*) FROM $I" \
  --probe "SELECT n, prompt, item, status, answer, agent, carried, answered_at, answered_seq, asked_at FROM $T WHERE n >= 0 AND n < 9000000000000000000 ORDER BY n DESC LIMIT 64" \
  --probe "SELECT n, item, reply, agent FROM $I WHERE n > (SELECT MAX(n) FROM $T) - 6 ORDER BY n" \
  --probe "SELECT answer FROM $T WHERE prompt = 'none'" \
  --probe "SELECT n, seq, summary, summarized FROM restate_agent_conversation_start" \
  --probe "SELECT MIN(n) FROM $T WHERE seq > (SELECT MAX(seq) FROM $T) - 3" \
  --probe "SELECT MIN(turn) FROM $I WHERE seq > (SELECT MAX(seq) FROM $I) - 3" > after-probes.jsonl
run --tree ../before --turns 500 --cold-at 500 \
  --probe "SELECT n, seq, item, prompt, answer, reply, agent FROM $C ORDER BY n" \
  --probe "SELECT MAX(n), MAX(seq) FROM $C" \
  --probe "SELECT MIN(n) FROM $C WHERE seq > (SELECT MAX(seq) FROM $C) - 3" > before-probes.jsonl

run --report before.jsonl after.jsonl fix.jsonl before-heavy.jsonl after-heavy.jsonl fix-heavy.jsonl \
  after-probes.jsonl before-probes.jsonl
```

The writes per turn are the averages of the warm rows (`set_bytes`, and
`by_kind` for the tail entries); the statements behind a cold turn are its
row's `by_statement`. All of it runs in about three minutes. A rerun gives
the same rows, statements and gets; bytes within a fraction of a percent
(the names of packs are random) and round trips within two (which reads the
native side issues together depends on its scheduling): run as written, the
commands above gave every number in these tables but a few round trips
(53 for the 55 of the cold turn after turn 1,000, say).
