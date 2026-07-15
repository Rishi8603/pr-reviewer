import os
from google import genai
from google.genai import types
from qdrant_client import QdrantClient
from dotenv import load_dotenv

load_dotenv()
client = genai.Client()
COLLECTION_NAME = "codebase_memory"

# =====================================================================
# SEMANTIC SEARCH PIPELINE
# Converts a PR diff into a vector, and compares it against the database
# using Cosine Similarity to find dependent files.
# =====================================================================
def search_codebase(query_text: str, top_k: int = 3):
    print(f"🔍 Searching codebase for semantic matches...")

    # ARCHITECTURE FIX: Scope the client locally so main.py can import safely
    qdrant = QdrantClient(path="./qdrant_data") 

    # 1. Turn the PR diff into a 768-dimensional math vector
    response = client.models.embed_content(
        model="gemini-embedding-001",
        contents=query_text,
        config=types.EmbedContentConfig(output_dimensionality=768),
    )
    query_vector = response.embeddings[0].values

    # 2. Ask Qdrant to find the top 3 closest math matches
    search_response = qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        limit=top_k 
    )
    
    # 3. Extract the raw source code from the database payload
    context_blocks = []
    for hit in search_response.points:
        filepath = hit.payload.get("filepath")
        name = hit.payload.get("name")
        block_type = hit.payload.get("type")
        code = hit.payload.get("code")
        context_blocks.append(f"--- File: {filepath} | {block_type}: {name} ---\n{code}\n")

    qdrant.close() 
    return "\n".join(context_blocks)