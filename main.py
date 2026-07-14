from fastapi import FastAPI, Request
from dotenv import load_dotenv
import os
import requests
from reviewer import pr_reviewer_graph

# Load environment variables securely
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

app = FastAPI()

def get_pr_diff(repo_full_name: str, pr_number: int) -> str:
    """Fetches the raw diff of the pull request from GitHub."""
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
    
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        # This specific header tells GitHub to return the raw diff text, not JSON
        "Accept": "application/vnd.github.v3.diff"
    }
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        return response.text
    else:
        print(f"Error fetching diff: {response.status_code} - {response.text}")
        return None

def post_pr_comment(repo_full_name: str, pr_number: int, comment_body: str):
    """Posts the AI review back to the GitHub Pull Request."""
    # Notice we use /issues/ instead of /pulls/ for comments
    url = f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments"
    
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    data = {
        "body": comment_body
    }
    
    response = requests.post(url, headers=headers, json=data)
    
    if response.status_code == 201:
        print("✅ Successfully posted review to GitHub!")
    else:
        print(f"❌ Error posting comment: {response.status_code} - {response.text}")

@app.post("/webhook")
async def github_webhook(request: Request):
    event_type = request.headers.get("X-GitHub-Event")
    payload = await request.json()
    
    if event_type == "pull_request":
        action = payload.get("action")
        
        if action in ["opened", "synchronize"]:
            pr_number = payload["pull_request"]["number"]
            repo_full_name = payload["repository"]["full_name"]
            
            print(f"🎯 Target Acquired: PR #{pr_number} in {repo_full_name}")
            print(f"Status: {action}")
            
            # Fetch the code changes
            diff_text = get_pr_diff(repo_full_name, pr_number)
            
            if diff_text:
                print("Passing data to LangGraph...")
                
                # --- NEW LOGIC: INVOKE THE GRAPH ---
                # We start the graph and pass the initial state (the diff)
                initial_state = {"pr_diff": diff_text}
                result = pr_reviewer_graph.invoke(initial_state)

                print("\n=== FINAL AI REVIEW ===")
                print(result["final_review"])
                print("=======================\n")

                # --- NEW LOGIC: POST TO GITHUB ---
                print("Shipping review to GitHub...")
                post_pr_comment(repo_full_name, pr_number, result["final_review"])
            
    return {"status": "success"}