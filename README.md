# Autonomous CI/CD PR Review Swarm

An event-driven service that reviews GitHub pull requests with five specialised
LLM reviewers running concurrently, retrieves relevant existing code from a
vector index of the repository, and gates the merge behind a unanimous verdict
computed in Python.

Deployed on Render free tier, where the filesystem is ephemeral — so the vector
memory rebuilds itself from source on a cold boot.

---

## How it works

```
PR opened / pushed
   │
   ▼
POST /webhook ──── HMAC-verify signature ──── reject 401 if unsigned
   │
   ├── claim (repo, head_sha) ──── duplicate delivery? return 200, do nothing
   │
   ├── BackgroundTasks.add_task(...)
   └── return 200  ◄── inside GitHub's ~10s delivery window
                          │
        ┌─────────────────┘  (Starlette runs the task after the response is sent)
        ▼
   commit status → pending
        │
   ensure_memory()  ── 0 vectors? ── git clone --depth 1 → AST chunk → embed → Qdrant
        │                            (warm process: one count(), returns immediately)
        ▼
   GET /pulls/{n} with Accept: vnd.github.v3.diff
        │
   search_codebase(diff)  ── split diff per file → embed each → top-3 per file
        │                     → merge by best cosine score → 8 unique blocks
        ▼
   ┌──────────────── LangGraph: one superstep, five nodes ────────────────┐
   │  Security    Performance    Style    QA / Tests    Product          │
   └──────────────────────────┬───────────────────────────────────────────┘
                              ▼
                    consensus gate (pure Python)
                              │
              5/5 ──► status: success ──┐
              < 5 ──► status: failure ──┴──► post review comment
```

### 1. The asynchronous gateway

GitHub allows roughly 10 seconds to acknowledge a webhook delivery. A five-reviewer
swarm takes far longer than that, and a cold boot longer still. So `/webhook` does
only the cheap work — verify, filter, de-duplicate — and hands the review to a
`BackgroundTask`, which Starlette runs *after* the 200 has been written to the socket.

Deliveries are de-duplicated on `(repo, head_sha)`, because webhooks are
at-least-once and `synchronize` fires on every push to the branch.

### 2. Self-healing vector memory

Render wipes the disk on every deploy and every wake from spin-down, so the
service regularly starts with no index at all. `ensure_memory()` detects an empty
collection, shallow-clones the target repository, chunks it, embeds it, and
deletes the clone. On an already-warm process it costs one `count()`.

Chunking uses Python's `ast` module rather than a character-count text splitter,
so a retrieved block is always a complete function or class instead of one cut in
half mid-body. Only the top level of each module is walked: descending into
classes would emit every method twice — once inside its class and once alone —
which doubles the embedding bill and lets one large class crowd everything else
out of the top-k.

### 3. The consensus gate

The model does not decide whether code merges. Each reviewer returns a
constrained `Verdict` object (`APPROVE` or `REQUEST_CHANGES`, plus findings), and
a plain Python function counts them. Anything short of unanimous blocks the merge,
and a reviewer whose call failed counts as a rejection rather than an approval —
treating a timeout as consent is how a gate stops being a gate.

The result is written to two places: a Markdown comment on the PR, and a **commit
status** on the head SHA. The comment is advisory; the commit status is the actual
gate, because naming it a required check in branch protection makes GitHub itself
refuse the merge.

---

## The five reviewers

| | Reviewer | Looks for |
|---|---|---|
| 🛡️ | Security | Hardcoded credentials, injection, missing authz, secrets in logs |
| 🚀 | Performance | Complexity regressions, N+1 queries, unbounded growth, blocking I/O |
| 💅 | Style | Naming, dead code, missing hints, inconsistency with retrieved context |
| 🧪 | QA / Tests | Missing tests, unhandled edge cases and boundaries |
| 👔 | Product | Scope creep, unrelated refactoring, speculative abstraction |

Adding a sixth is one entry in `REVIEWERS` — the runner and the gate size
themselves off that list.

---

## Layout

| File | Responsibility |
|---|---|
| `main.py` | Webhook entrypoint, signature verification, GitHub API calls, background worker |
| `reviewer.py` | LangGraph graph, the five reviewer specs, the consensus gate |
| `ingest.py` | AST chunking, repository indexing, self-healing cold boot |
| `retrieve.py` | Per-file diff retrieval and result merging |
| `embeddings.py` | Shared embedding client — one model, one dimensionality, both pipelines |
| `qdrant_store.py` | Sole owner of the Qdrant connection and its concurrency lock |
| `database.py` | SQLAlchemy engine, session factory, connection pooling |
| `models.py` | Five normalised tables: repositories, pull_requests, reviews, findings, review_consensus |
| `db_writer.py` | Transactional persistence of completed reviews to PostgreSQL |
| `analytics.py` | FastAPI router with six SQL-powered analytics endpoints |
| `rate_limiter.py` | Per-repository sliding-window rate limiter backed by PostgreSQL |
| `test_review_swarm.py` | 31 offline tests over the deterministic logic |

`embeddings.py` exists so ingest and retrieval cannot drift onto different models
or dimensionalities — their vectors would land in different spaces and cosine
similarity between them would be meaningless.

`qdrant_store.py` exists because Qdrant runs here in embedded mode, which takes an
exclusive file lock on its directory. Two PRs arriving together execute in the
same process, so access is serialised behind a lock; without it the second one
dies. Moving to a Qdrant server is a change to that one file.

---

## Tech stack

- **API** — FastAPI, Starlette `BackgroundTasks`
- **Orchestration** — LangGraph (fan-out / fan-in over a shared `TypedDict` state)
- **Vector store** — Qdrant, embedded mode, cosine distance
- **Relational store** — PostgreSQL via SQLAlchemy (review audit trail + analytics)
- **Migrations** — Alembic
- **Models** — `gemini-2.5-flash` for review, `gemini-embedding-001` at 768 dims
- **Parsing** — Python `ast`

---

## Setup

```bash
git clone https://github.com/Rishi8603/pr-reviewer.git
cd pr-reviewer

python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Linux / macOS

pip install -r requirements.txt
cp .env.example .env           # then fill it in
```

### Database (optional)

PostgreSQL is optional — the service works without it, but analytics endpoints
return 503 and reviews are not persisted.

```bash
# Start PostgreSQL (Docker or local install)
docker run -d --name pr-reviewer-db -p 5432:5432 \
  -e POSTGRES_DB=pr_reviewer \
  -e POSTGRES_USER=user \
  -e POSTGRES_PASSWORD=password \
  postgres:16

# Set DATABASE_URL in .env
# DATABASE_URL=postgresql://user:password@localhost:5432/pr_reviewer

# Run migrations
alembic upgrade head
```

### Run

```bash
uvicorn main:app --reload
```

Then check that memory is populated:

```bash
curl http://localhost:8000/health
# {"status":"ok","vectors":42,"memory":"warm","signature_verification":true,"database":"connected"}
```

There is no separate indexing step to remember — the first review builds the index
if it is empty. To index a directory ahead of time anyway:

```bash
python ingest.py            # index the current directory
python ingest.py ../other   # index somewhere else
```

### Wire up GitHub

1. Repo → **Settings → Webhooks → Add webhook**
   - Payload URL: `https://<your-host>/webhook`
   - Content type: `application/json`
   - Secret: the same value as `GITHUB_WEBHOOK_SECRET`
   - Events: **Pull requests**
2. To make the verdict actually block a merge, repo → **Settings → Branches →
   Branch protection rule** → require the status check named **`pr-review-swarm`**.

Without step 2 the review is a comment. With it, a 4/5 verdict disables the merge
button.

### Tests

```bash
python -m unittest discover -v
```

31 tests, fully offline — no Gemini call, no Qdrant, no GitHub. They cover the
logic that decides whether a PR merges, which is the part that should be
verifiable without a network.

---

## Analytics endpoints

When `DATABASE_URL` is set, every review is persisted to PostgreSQL and the
following endpoints become available:

| Endpoint | Returns |
|---|---|
| `GET /analytics/overview?days=30` | Per-repo approval rates, avg/p95 review latency |
| `GET /analytics/reviewer-agreement?days=30` | Pairwise agreement rates between reviewers (CTE + self-join) |
| `GET /analytics/hotspots?days=30&limit=20` | Files with the most findings, ranked by DENSE_RANK |
| `GET /analytics/trends?weeks=12` | Weekly review volume and outcome time-series |
| `GET /analytics/reviewer/{type}/findings?days=30` | A reviewer's recent findings with window functions |
| `GET /analytics/summary` | Quick dashboard counts |

The queries use CTEs, window functions (`ROW_NUMBER`, `DENSE_RANK`,
`PERCENTILE_CONT`), `DATE_TRUNC`, and conditional aggregation.

### Rate limiting

When `DATABASE_URL` is set, a per-repository sliding-window rate limiter
prevents webhook flooding. Default: 10 reviews per hour per repository.
Configurable via `RATE_LIMIT_MAX_REVIEWS` and `RATE_LIMIT_WINDOW_SECONDS`.

---

## Measurements

| | |
|---|---|
| Five reviewers, wall clock | **0.31 s** vs 1.50 s if run sequentially (0.3 s stub per reviewer) — a 4.8× overlap, asserted by a test |
| Render free-tier cold start | ~63 s before the first request is served |
| LLM calls per review | 5 (one per reviewer) — the report is rendered in Python, not by a sixth call |

The concurrency figure holds because fanning out from `START` puts all five nodes
in one LangGraph superstep, LangGraph dispatches sync node callables to a
threadpool, and the work inside each is a network call that releases the GIL while
it waits.

---

## Known limitations

Honest list of what this does not do yet.

- **Single process only.** Embedded Qdrant locks its directory and the delivery
  de-duplication set lives in process memory, so `--workers 2` breaks both. Both
  want the same fix: a Qdrant server and Redis.
- **No durable queue.** `BackgroundTasks` runs in-process, so a restart mid-review
  loses that review with no retry. A real deployment wants Celery or SQS.
- **Cold boot is on the critical path.** The first PR after a spin-down waits
  through a clone and a full index. Warming on startup instead would hide it.
- **Python only.** `ast` is Python-specific; other languages need tree-sitter.
- **Unanimity is strict.** Five independent reviewers each with a small
  false-positive rate rarely all approve, so in practice most PRs get a
  `REQUEST_CHANGES`. Weighting reviewers, or making only Security blocking, would
  trade strictness for a bot people keep listening to.
- **Whole-repo index, not incremental.** A cold boot re-indexes everything rather
  than only what changed since the last commit it saw.
- **Review comments are not line-anchored.** The report is one comment; GitHub's
  review API could attach findings to specific lines.
