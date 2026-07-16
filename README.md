# 🚀 Autonomous CI/CD PR Review Swarm

An event-driven, stateful microservice that intercepts GitHub webhooks and orchestrates a highly concurrent 5-agent AI swarm to review Pull Requests.

Unlike naive LLM wrappers, this system uses a **self-healing vector memory** to retain project-level context and a **deterministic Python state machine** to enforce strict 5/5 consensus before allowing code to merge. This reduces hallucinations and improves reliability for production code review workflows.

---

## 🏗️ Core Architecture

### 1. The Asynchronous Gateway

GitHub webhooks enforce a strict timeout window. This FastAPI service uses `BackgroundTasks` to immediately return a `200 OK` response to GitHub, while offloading heavy AI computation and repository ingestion to background workers.

### 2. Self-Healing Codebase Memory

Designed for ephemeral cloud environments such as Render. If the system starts with an empty state, it automatically clones the target repository, parses the codebase using Python Abstract Syntax Trees (`ast`), and stores function-level embeddings in **Qdrant**.

### 3. The Strict Consensus Engine

The AI does not make the final merge decision. Five LangGraph agents run in parallel, and a pure Python state machine tallies their results. If the PR does not receive a perfect 5/5 approval, the merge is blocked.

---

## ⚙️ Multi-Agent Swarm

When a Pull Request is opened, the diff is analyzed and semantically matched against the vector memory. Relevant context is fetched from Qdrant using cosine similarity, then passed to five specialized agents:

* 🛡️ **Security Agent**
  Scans for hardcoded credentials, injection risks, and vulnerabilities.

* 🚀 **Performance Agent**
  Identifies inefficient logic, Big-O bottlenecks, and memory leaks.

* 💅 **Style Agent**
  Enforces naming conventions, readability, and code consistency.

* 🧪 **QA / Test Agent**
  Detects missing unit tests and unhandled edge cases.

* 👔 **PM Agent**
  Evaluates scope creep, over-engineering, and product alignment.

---

## 🛠️ Tech Stack

* **API & Routing:** FastAPI, Python `BackgroundTasks`
* **AI Orchestration:** LangGraph
* **Vector Database:** Qdrant
* **Embeddings & LLM:** Google Gemini 2.5 Flash, `gemini-embedding-001`
* **Parsing:** Python `ast` module

---

## 💻 Local Setup & Testing

To run this pipeline locally, first build the memory bank, then trigger the webhook.

### 1. Clone & Install

```bash
git clone https://github.com/YOUR_USERNAME/pr-reviewer.git
cd pr-reviewer

# Create virtual environment
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/macOS
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Environment Variables

Create a `.env` file in the root directory:

```env
GITHUB_TOKEN=your_github_personal_access_token
GEMINI_API_KEY=your_gemini_api_key
```

### 3. Build the Memory Bank

Before the AI can review code, it must ingest the codebase.

```bash
python ingest.py
```

Expected output:

```bash
✅ Successfully ingested X code blocks
```

### 4. Start the Webhook Server

```bash
uvicorn main:app --reload
```

### 5. Simulate a GitHub Webhook

Use Postman or `curl` to send a `POST` request to:

```text
http://localhost:8000/webhook
```

Headers:

```text
X-GitHub-Event: pull_request
```

Body: provide a dummy GitHub PR payload containing a target `clone_url` and `repo_full_name`.

---

## 🔁 Production Lifecycle

When deployed to a cloud provider with ephemeral storage, such as Render free tier, the system behaves autonomously:

1. **Amnesia Boot:** The server starts with an empty Qdrant database.
2. **Webhook Catch:** A PR is opened. FastAPI catches the payload, returns `200 OK`, and sends the job to the background.
3. **Autonomous Sync:** The background task detects an empty database, clones the `main` branch, ingests AST vectors into Qdrant, and cleans up temporary files.
4. **Swarm Execution:** The PR diff is semantically searched against the rebuilt memory, and the five LangGraph agents run in parallel.
5. **Delivery:** The orchestrator calculates consensus and posts the formatted Markdown review directly to the GitHub PR timeline via the REST API.

---

## 📌 Highlights

* Event-driven architecture for CI/CD workflows
* Background task handling for fast webhook acknowledgment
* AST-based repository ingestion
* Qdrant-powered project memory
* Parallel multi-agent code review
* Deterministic consensus enforcement before merge

---

## 📄 Example Flow

```text
GitHub PR Opened
→ Webhook Received
→ Immediate 200 OK Response
→ Background Ingestion / Memory Sync
→ Qdrant Retrieval
→ 5-Agent Parallel Review
→ Consensus Engine
→ GitHub PR Comment Posted
```

---

## ✅ Outcome

This architecture is designed to demonstrate:

* distributed systems thinking
* production webhook handling
* ephemeral storage recovery
* vector-based semantic memory
* concurrent agent orchestration
* deterministic merge control

If needed, I can also turn this into a more polished **GitHub-style README** with badges, table of contents, and cleaner wording.
