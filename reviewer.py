from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langchain_google_genai import ChatGoogleGenerativeAI
import os
from dotenv import load_dotenv

load_dotenv()

# 1. The Expanded State (The Shared Memory)
# Instead of one string, the State now holds separate reports from each agent.
class AgentState(TypedDict):
    pr_diff: str
    security_review: str
    performance_review: str
    style_review: str
    final_review: str

# Initialize the LLM once
llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.1)

# 2. The Specialized Agents (The Workers)
def security_node(state: AgentState):
    print("🛡️ Security Agent analyzing...")
    prompt = f"You are a strict security auditor. Ignore style. Focus ONLY on vulnerabilities and exposed keys in this diff:\n{state['pr_diff']}\nIf none, output 'No security issues found.'"
    response = llm.invoke(prompt)
    return {"security_review": response.content}

def performance_node(state: AgentState):
    print("🚀 Performance Agent analyzing...")
    prompt = f"You are a performance engineer. Ignore security. Focus ONLY on Big-O bottlenecks and inefficient loops in this diff:\n{state['pr_diff']}\nIf none, output 'No performance issues found.'"
    response = llm.invoke(prompt)
    return {"performance_review": response.content}

def style_node(state: AgentState):
    print("💅 Style Agent analyzing...")
    prompt = f"You are a code formatter. Focus ONLY on PEP8, bad variable names, and readability in this diff:\n{state['pr_diff']}\nIf none, output 'Code style is clean.'"
    response = llm.invoke(prompt)
    return {"style_review": response.content}

# 3. The Orchestrator (The Senior Lead)
def orchestrator_node(state: AgentState):
    print("🧠 Orchestrator merging reports...")
    prompt = f"""
    You are the Lead Senior Engineer. Merge these three reports into one professional, clean Markdown PR review.
    Do not include fluff. Group by category.
    
    Security Report: {state.get('security_review')}
    Performance Report: {state.get('performance_review')}
    Style Report: {state.get('style_review')}
    """
    response = llm.invoke(prompt)
    return {"final_review": response.content}

# 4. Build the Graph Formation
workflow = StateGraph(AgentState)

# Add all players to the field
workflow.add_node("security", security_node)
workflow.add_node("performance", performance_node)
workflow.add_node("style", style_node)
workflow.add_node("orchestrator", orchestrator_node)

# FAN-OUT: START triggers all three specialized agents at the same time
workflow.add_edge(START, "security")
workflow.add_edge(START, "performance")
workflow.add_edge(START, "style")

# FAN-IN: All three agents pass their data to the Orchestrator
workflow.add_edge(["security", "performance", "style"], "orchestrator")

# The Orchestrator finishes the job
workflow.add_edge("orchestrator", END)

# Compile into an executable system
pr_reviewer_graph = workflow.compile()