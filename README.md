# Agent-Legacy — Cost-Aware Multi-Agent Deep Research Framework

An autonomous AI agent **team** that plans, researches the web, reasons, builds
deliverable files on disk, and self-reviews — routing each job to the cheapest
capable model. Built on [`deepagents`](https://pypi.org/project/deepagents/)
(compiles to a [LangGraph](https://github.com/langchain-ai/langgraph) graph),
OpenAI-compatible routing via **TokenRouter** (`qwen/qwen3.8-max-free`), and a verbose
streaming event protocol designed to make every step of the agent's work
observable.

## What it does

- **Classifies task complexity** (simple / medium / complex) and routes to an
  appropriately sized model — cheap models for easy tasks, the strongest model
  only when needed.
- **Delegates to specialist sub-agents**: a universal 5-agent team —
  *research* (web search + deep crawl), *executor* (builds files), *decision*
  (strong reasoning), and *qa* (pass/fail review + score), coordinated by a
  *planner* (CEO).
- **Crawls the real web**: search (TinyFish), fetch pages, extract links, and
  recursive same-domain site crawling.
- **Writes deliverable files** to a workspace via a filesystem backend.
- **Remembers** past tasks in file-backed long-term memory and recalls them.
- **Recovers from failures**: narrates tool errors and retries from a different
  angle instead of stopping.
- **Escalates hard reasoning** mid-run to the strongest LLM without spawning a
  full sub-agent (a cost optimization).
- **Tracks approximate token usage and USD cost** per run, per model.
- **Streams structured events** (routed / handoff / visit / tool_call /
  collected / token / cost / complete) to a **live terminal UI** (`run.bat`).

## Architecture

```
User input
   |
   v
router.classify_task()          cost-aware tier picking
heuristic -> (if MEDIUM) -> LLM   SIMPLE / MEDIUM / COMPLEX
   |  picks ModelSpec
   v
agent_factory.build_agent()     deepagents.create_deep_agent()
|- Planner (main model)         compiles to a LangGraph graph
|- Sub-agent: research          web search + deep crawl
|- Sub-agent: executor          builds deliverable files
|- Sub-agent: decision (COMPLEX) strong reasoning on hard choices
'- Sub-agent: qa (COMPLEX)      PASS/FAIL review + score/10
+ FilesystemBackend (write_file)
   |  streamed events
   v
services/runner.run_agent()     async generator yielding structured events
   |
   v
agent/tui.py                    Rich live dashboard (run.bat)
```

### The team
- **Planner (CEO)** — breaks goals down with `write_todos`, coordinates the
  team, uses memory, recovers from failures, writes deliverables, and narrates
  each step in plain, natural language.
- **Research** — uses `web_search` + `fetch_url` / `crawl_website` to gather
  cited evidence.
- **Executor** — does the physical work: fetches data and writes files.
- **Decision** — runs on the strongest model to reason about the hardest choices.
- **QA** — independently reviews deliverables, returns PASS/FAIL + score/10.

## Project structure

```
components/
|- agent/
|  |- config.py            # env-driven settings
|  |- router.py            # multi-LLM tier router + classifier
|  |- cost.py              # token + USD cost estimation
|  |- core/agent_factory.py# builds the deepagents graph
|  |- prompts/universal_ops.py  # Agent-Legacy system prompts
|  |- services/runner.py   # async streaming event runner
|  |- memory/              # long-term memory + memory tools
|  |- tools/               # crawl, web_search, registry, route_llm
|  |- tui.py               # Rich live dashboard
|  |- cli.py               # plain streaming log view
|  '- data/memory/         # persisted memory JSON
|- tests/test_build.py      # smoke test (compiles graph, no API call)
|- run.bat                  # launch the terminal UI
|- requirements.txt
|- pyproject.toml
'- .env.example
```

## Setup

Requires Python 3.11+.

```bash
# 1. Create / activate a venv (one is included at ./venv on Windows)
python -m venv venv
./venv/Scripts/Activate.ps1      # Windows PowerShell
# source venv/bin/activate        # macOS/Linux

# 2. Install dependencies + the project (editable, so `agent` is importable
#    everywhere without setting PYTHONPATH)
pip install -e ".[dev]"

# 3. Configure secrets
cp .env.example .env
#   then edit .env and set API_KEY (TokenRouter — same base URL + key found in
#   your opencode.json tokenrouter provider block) + TINYFISH_API_KEY
```

### Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `BASE_URL` / `API_KEY` / `MODEL` / `TEMPERATURE` | Global default LLM (TokenRouter) |
| `SIMPLE_*` / `MEDIUM_*` / `COMPLEX_*` | Per-tier model/base_url/key; blank -> falls back to global default |
| `MODEL_ROUTING` | `auto` (heuristic first, LLM only if ambiguous) / `llm` / `heuristic` |
| `TINYFISH_API_KEY` / `TINYFISH_ENDPOINT` | Web search provider |
| `RECURSION_LIMIT` | LangGraph step budget per stretch (default `200`); when exhausted the run resumes from its saved checkpoint |
| `MAX_CONTINUATIONS` | How many times a run may resume after hitting the step budget (default `5`); total work cap ≈ `(1 + MAX_CONTINUATIONS) × RECURSION_LIMIT` steps |

A single TokenRouter key is enough — leave the per-tier keys blank and they fall
back to the global default. The default setup runs every tier on
`qwen/qwen3.8-max-free` (free tier, so the cost tracker reports $0.00).

## How routing works (the clever part)

The deterministic **heuristic** is free, so it always runs first. An LLM call is
**only** spent when the heuristic is unsure (lands on `MEDIUM`, genuinely
ambiguous). Confident `SIMPLE`/`COMPLEX` results skip the LLM entirely, saving
tokens and cost. Set `MODEL_ROUTING=heuristic` to never call the classifier LLM,
or `MODEL_ROUTING=llm` to always ask it.

## Tools (registered for every session)

| Tool | What it does |
|---|---|
| `web_search` | TinyFish search -> title/url/snippet list |
| `fetch_url` | fetch one page -> readable markdown |
| `extract_links` | links on a page (optionally same-domain) |
| `crawl_website` | recursive same-domain BFS crawl |
| `recall_memory` | search long-term memory |
| `remember` | persist a learning/fact |
| `route_to_strong_llm` | hand ONE hard reasoning subtask to the strongest model |

Plus filesystem tools (`write_file`, `list_files`, ...) from the `deepagents`
`FilesystemBackend`.

## Cost tracking

Token usage comes from the provider's **real `usage_metadata`** (streamed back
by the model) whenever available, attributed per actual model — so sub-agent
calls on the strong model are no longer billed to the routed model. This is
done by a `UsageTracker` middleware wrapped around every model call. When the
provider reports nothing, it falls back to the `chars // 4` estimate. USD cost
is matched against per-model rates per 1M tokens (prefix-matched:
gpt-4o-mini, deepseek, qwen, gemini, claude...). The report is clearly labelled
as real provider usage or an estimate. Run-level guardrails
(`ModelCallLimitMiddleware` / `ToolCallLimitMiddleware`) cap runaway loops.

## Memory

File-backed JSON per session. Retrieval ranks entries by a weighted score:

```
score = 0.7 * keyword_overlap + 0.2 * recency + 0.1 * (importance / 10)
```

After each run, a short (TASK / RESULT / FILES) episodic summary is written
back. Exact-duplicate entries are skipped to keep the store clean.

## House modes (Tab to switch)

Four switchable operating modes named after the Hogwarts houses — each changes
actual agent behaviour, not just the theme:

| House | Trait | Mechanical advantage |
|---|---|---|
| 🦁 Gryffindor | brave | **Speed** — parallel research batches, shorter recovery waits |
| 🦡 Hufflepuff | loyal | **Reliability** — every deliverable forced through a QA review, +1 run retry |
| 🦅 Ravenclaw | wise | **Economy** — forces LLM-based routing + concise outputs |
| 🐍 Slytherin | cunning | **Ambition** — mandatory delegation to specialists, bigger cited deliverables |

- **Tab** cycles the modes live (header repaints instantly with the house sigil).
- `/mode` opens the house picker overlay; `/mode gryffindor` sets directly.
- The active house is injected into the planner prompt and shown in the routed
  event (`house=gryffindor (speed)`).

## Running the terminal UI

```bash
run.bat                       # Windows: interactive live dashboard
run.bat research AI agents and write a report   # or with a prompt
run.bat --plain "..."         # simple log view instead of the TUI
```

The Agent-Legacy TUI is a mission-control dashboard (Textual): a branded top
strip, a status-pill bar (state, routed tier/model, step, elapsed clock), a
live livestream log with per-agent glyphs, a Team/Browsing/Collected side panel,
and a token/cost footer. `--rich` runs the original hand-rolled Rich dashboard.

## Running the smoke test

```bash
python tests/test_build.py
# or
pytest
```

Expected output (no API call is made — it only verifies the graph compiles):

```
[OK] 7 tools registered: fetch_url, extract_links, crawl_website,
     web_search, recall_memory, remember, route_to_strong_llm
[OK] memory store has 1 entries
[OK] memory retrieval returned 1 result(s)
[OK] deep agent graph compiled (no API call made)
```

## Voice output (live playback)

The agent narrates each step in plain English. Instead of saving synthesized
audio to disk, `agent/services/voice.py` plays each narration line immediately
as its audio is received, using the Windows standard-library `winsound` module
(WAV from memory — zero extra dependencies). TTS is requested in `wav` format
for playback.

- `synthesize_speech(text, response_format="wav")` -> audio bytes (OpenRouter)
- `speak_line(text)`     -> synthesize + play one line right away
- `speak_agent_run(...)` -> run the agent and speak narration as it arrives
  (a passthrough over `run_agent()` that re-yields every event unchanged)

Configure via `TTS_MODEL` / `STT_MODEL` (defaults: `fish-audio/s2.1-pro-free:free`
TTS, `fish-audio/transcribe-1` STT). Use `response_format="wav"` for live
playback, `"mp3"` for saved clips.

## Roadmap / notes

- Token/cost figures are approximations for observability, not billing.
- Long research runs never die on LangGraph's step budget: run state is
  checkpointed to `agent/data/checkpoints.sqlite` under a per-task `thread_id`,
  and when `RECURSION_LIMIT` steps are exhausted the run **resumes** from the
  saved checkpoint (up to `MAX_CONTINUATIONS` times) instead of losing the work.
  If it still can't finish, it ships what's done so far with a clear notice.
- Next planned piece: a FastAPI + WebSocket relay so a web dashboard can
  consume the same streaming event protocol (dependencies already in
  `requirements.txt`).
