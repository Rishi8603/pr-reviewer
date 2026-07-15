from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langchain_google_genai import ChatGoogleGenerativeAI
import os
from dotenv import load_dotenv

load_dotenv()

# 1. The Enterprise State (The Shared Memory)
class AgentState(TypedDict):
    pr_diff: str
    codebase_context: str
    security_review: str
    performance_review: str
    style_review: str
    test_review: str  # NEW: QA Agent Memory
    pm_review: str    # NEW: Product Manager Memory
    final_review: str

# Initialize the LLM once
llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.1)

# 2. The Core 3 Agents (Unchanged)
def security_node(state: AgentState):
    print("🛡️ Security Agent analyzing...")
    prompt = f"You are a strict security auditor. Analyze this PR diff:\n{state['pr_diff']}\nContext:\n{state['codebase_context']}\nIf no security issues are found, output 'APPROVE'."
    response = llm.invoke(prompt)
    return {"security_review": response.content}

def performance_node(state: AgentState):
    print("🚀 Performance Agent analyzing...")
    prompt = f"You are a performance engineer. Analyze this PR diff for Big-O bottlenecks:\n{state['pr_diff']}\nContext:\n{state['codebase_context']}\nIf no performance issues are found, output 'APPROVE'."
    response = llm.invoke(prompt)
    return {"performance_review": response.content}

def style_node(state: AgentState):
    print("💅 Style Agent analyzing...")
    prompt = f"You are a code formatter. Analyze this diff for PEP8 readability:\n{state['pr_diff']}\nContext:\n{state['codebase_context']}\nIf code style is clean, output 'APPROVE'."
    response = llm.invoke(prompt)
    return {"style_review": response.content}

# 3. THE NEW ENTERPRISE AGENTS
def test_node(state: AgentState):
    print("🧪 Test Agent analyzing...")
    prompt = f"""You are a QA Lead Engineer. 
    Analyze this PR diff and the surrounding context:
    Diff: {state['pr_diff']}
    Context: {state['codebase_context']}
    
    Check if the author included unit tests for their new code. 
    Identify any unhandled edge cases (e.g., null values, empty arrays, negative numbers).
    If the code is fully covered and mathematically sound, output 'APPROVE'."""
    
    response = llm.invoke(prompt)
    return {"test_review": response.content}

def pm_node(state: AgentState):
    print("👔 PM Agent analyzing...")
    prompt = f"""You are a strict Product Manager. 
    Analyze this PR diff:
    Diff: {state['pr_diff']}
    
    Evaluate the scope. Did the developer over-engineer this? Are they refactoring things they shouldn't be? 
    Does this code solve a clear business logic problem?
    If the scope is tight and the business logic is sound, output 'APPROVE'."""
    
    response = llm.invoke(prompt)
    return {"pm_review": response.content}

def orchestrator_node(state: AgentState):
    print("🧠 Orchestrator calculating mathematical consensus...")
    
    # 1. Gather all reports into a dictionary
    reports = {
        "Security": state.get('security_review', ''),
        "Performance": state.get('performance_review', ''),
        "Style": state.get('style_review', ''),
        "Tests": state.get('test_review', ''),
        "Product": state.get('pm_review', '')
    }
    
    # 2. The Deterministic Engine (Pure Python Math)
    approvals = 0
    rejections = []
    
    for agent_name, report in reports.items():
        # We explicitly told our agents to output "APPROVE" if everything is fine
        if "APPROVE" in report.upper():
            approvals += 1
        else:
            rejections.append(agent_name)
            
    # 3. The State Machine Routing
    if approvals == 5:
        consensus_status = "✅ APPROVED (5/5 Consensus Reached)"
        prompt_instruction = "All agents approved. Write a brief, positive Markdown summary of the clean PR."
    else:
        consensus_status = f"❌ CHANGES REQUESTED ({approvals}/5 Approvals)"
        prompt_instruction = f"Consensus failed. The following agents rejected the PR: {', '.join(rejections)}. Write a strict Markdown report highlighting ONLY the issues that need to be fixed."

    # 4. The Spokesperson (LLM Formatting)
    prompt = f"""
    You are the Lead Senior Engineer. 
    STATUS: {consensus_status}
    
    INSTRUCTION: {prompt_instruction}
    
    RAW REPORTS:
    Security: {reports['Security']}
    Performance: {reports['Performance']}
    Style: {reports['Style']}
    Tests: {reports['Tests']}
    Product: {reports['Product']}
    """
    
    response = llm.invoke(prompt)
    return {"final_review": response.content}

# 5. Build the Enterprise Graph
workflow = StateGraph(AgentState)

# Add all 5 workers
workflow.add_node("security", security_node)
workflow.add_node("performance", performance_node)
workflow.add_node("style", style_node)
workflow.add_node("tests", test_node)
workflow.add_node("pm", pm_node)
workflow.add_node("orchestrator", orchestrator_node)

# FAN-OUT: Trigger all 5 specialized agents concurrently
workflow.add_edge(START, "security")
workflow.add_edge(START, "performance")
workflow.add_edge(START, "style")
workflow.add_edge(START, "tests")
workflow.add_edge(START, "pm")

# FAN-IN: All 5 agents pass their data to the Orchestrator
workflow.add_edge(["security", "performance", "style", "tests", "pm"], "orchestrator")

workflow.add_edge("orchestrator", END)

pr_reviewer_graph = workflow.compile()