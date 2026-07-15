from fastapi import FastAPI, Request, BackgroundTasks
from dotenv import load_dotenv
import os
import requests
from reviewer import pr_reviewer_graph
from retrieve import search_codebase

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
app = FastAPI()

# =====================================================================
# GITHUB API HELPERS
# =====================================================================
def get_pr_diff(repo_full_name: str, pr_number: int) -> str:
    """Fetches the raw text diff of the PR."""
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3.diff" # Forces raw diff output
    }
    response = requests.get(url, headers=headers)
    return response.text if response.status_code == 200 else None

def post_pr_comment(repo_full_name: str, pr_number: int, comment_body: str):
    """Posts the final Markdown review back to the GitHub PR."""
    url = f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    requests.post(url, headers=headers, json={"body": comment_body})


# =====================================================================
# THE BACKGROUND WORKER (The "Kitchen")
# Why BackgroundTasks? GitHub webhooks time out after 10 seconds. 
# AI reviews take 15+ seconds. We run this asynchronously to prevent crashes.
# =====================================================================
def process_pr_autonomous(repo_full_name: str, pr_number: int):
    print(f"\n⚙️ [BACKGROUND TASK] Processing PR #{pr_number}...")
    
    # 1. Get the Diff
    diff_text = get_pr_diff(repo_full_name, pr_number)
    if not diff_text:
        print("❌ Could not fetch diff.")
        return

    # 2. Query Qdrant for Project-Level Memory
    print("🔍 Searching codebase memory...")
    retrieved_context = search_codebase(diff_text)

    # 3. Load the Data into the LangGraph State Machine
    print("🧠 Triggering LangGraph 5-Agent Swarm...")
    initial_state = {
        "pr_diff": diff_text,
        "codebase_context": retrieved_context,
        "security_review": "",
        "performance_review": "",
        "style_review": "",
        "test_review": "",
        "pm_review": "",
        "final_review": ""
    }
    
    # 4. Execute the Swarm
    result = pr_reviewer_graph.invoke(initial_state)
    final_markdown = result["final_review"]

    # 5. Deliver the Result
    post_pr_comment(repo_full_name, pr_number, final_markdown)
    print(f"✅ [FINISHED] Review posted to PR #{pr_number}")


# =====================================================================
# THE WEBHOOK ENTRYPOINT (The "Drive-Thru Window")
# =====================================================================
@app.post("/webhook")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    event_type = request.headers.get("X-GitHub-Event")
    payload = await request.json()
    
    if event_type == "pull_request":
        action = payload.get("action")
        
        if action in ["opened", "synchronize"]:
            pr_number = payload["pull_request"]["number"]
            repo_full_name = payload["repository"]["full_name"]
            
            print(f"🎯 Target Acquired: PR #{pr_number}. Handing off to background worker...")
            
            # Offload the heavy AI lifting to the background worker
            background_tasks.add_task(process_pr_autonomous, repo_full_name, pr_number)
            
            # Instantly return 200 OK so GitHub doesn't drop the connection!
            return {"status": "success", "message": "PR review queued in background."}
            
    return {"status": "ignored"}