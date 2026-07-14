# 🚀 Multi-Agent CI/CD Pull Request Reviewer

An event-driven, autonomous code review pipeline powered by multi-agent AI orchestration.

This infrastructure tool intercepts GitHub webhooks when a Pull Request is created or updated, extracts the raw `diff`, and routes it to specialized LLM agents for Security, Performance, and Style analysis in parallel. An Orchestrator agent compiles the findings into a single consensus report and publishes it directly to the GitHub PR interface.

## 🏗️ System Architecture

* **Event Trigger:** GitHub Webhooks (`opened`, `synchronize`)
* **API Gateway:** FastAPI (Python)
* **AI Orchestration:** LangGraph (stateful multi-agent graph)
* **LLM Engine:** Google Gemini 2.5 Flash
* **Deployment:** Render (cloud container)

## ⚙️ How It Works

1. **Extraction:** The FastAPI endpoint receives the webhook payload and fetches the raw code diff from the GitHub REST API.
2. **Fan-Out (Parallel Execution):** LangGraph injects the diff into a state machine and simultaneously triggers three specialized agents:

   * 🛡️ **Security Agent:** Scans for hardcoded credentials, SQL injection risks, and vulnerabilities.
   * 🚀 **Performance Agent:** Identifies inefficient logic, O(N²) bottlenecks, and memory issues.
   * 💅 **Style Agent:** Checks naming conventions, readability, and code consistency.
3. **Fan-In (Consensus):** An Orchestrator node waits for all agents to finish, merges their independent reports, and formats a clean Markdown review.
4. **Delivery:** The backend pushes the compiled review back to the GitHub PR as an automated comment.

## 🛠️ Local Setup

If you want to run this CI/CD pipeline locally:

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/pr-reviewer.git
cd pr-reviewer
```

### 2. Create a virtual environment and install dependencies

**Windows**

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

**Linux/macOS**

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Create a `.env` file in the root directory

```env
GITHUB_TOKEN=your_github_personal_access_token
GOOGLE_API_KEY=your_gemini_api_key
GITHUB_WEBHOOK_SECRET=your_webhook_secret
```

### 4. Run the local server

```bash
uvicorn main:app --reload
```

## 🔁 Production Flow

1. A developer opens or updates a Pull Request on GitHub.
2. GitHub sends a webhook to the live Render endpoint.
3. FastAPI receives the event and fetches the PR diff.
4. LangGraph fans out the diff to multiple Gemini-based agents.
5. The orchestrator combines the results into one review.
6. The review is posted back to the PR as a GitHub comment.

## ✨ Why this is useful

* Automates code review for every PR
* Checks different concerns in parallel
* Produces a single consolidated review
* Fits naturally into a CI/CD workflow
