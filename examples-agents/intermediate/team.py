"""
A team of agents on different frameworks: an editor (OpenAI Agents SDK) hands
research to a researcher (LangGraph) and drafting to a writer (Pydantic AI),
and puts the piece together.

Each member is an agent of its own, keyed by the editor's key and the task
(``researcher/alice.lisbon``), with its own conversation. The editor can come
back to a task, and a crash anywhere resumes where it stopped::

    OPENAI_API_KEY=... uv run --extra openai --extra langgraph --extra pydantic_ai --extra serve \\
        --with langchain-openai --with 'pydantic-ai-slim[openai]' examples-agents/intermediate/team.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/editor/alice/chat -d 'Write a short guide to Lisbon for a first visit.'
    curl localhost:8080/writer/alice.guide/messages --json '{}'   # what the editor asked the writer (the task's name may differ)
"""

import asyncio

import restate
from restate import sessions
from restate.agents import langgraph, openai, pydantic_ai, subagent

researcher = langgraph.agent(
    "researcher",
    description="Researches a topic, and answers with the facts",
    system="You research a topic and answer with the facts that matter, as a short list.",
)

writer = pydantic_ai.agent(
    "writer",
    description="Drafts prose from notes",
    system="You write clear, warm prose from the notes you are given. Keep it short.",
)

editor = openai.agent(
    "editor",
    system="You edit a small team: research with the researcher, draft with the writer, then answer with the piece.",
    tools=[subagent(researcher), subagent(writer)],
)


if __name__ == "__main__":
    asyncio.run(restate.serve(sessions.app(editor, researcher, writer)))
