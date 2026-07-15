import os
import ast
from google.genai import types
from google import genai
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from dotenv import load_dotenv

load_dotenv()
client = genai.Client()

COLLECTION_NAME = "codebase_memory"

# =====================================================================
# PHASE 1: THE AST PARSER
# Why AST? Standard text splitters blindly chop code in half, breaking logic. 
# AST reads code like a compiler, allowing us to extract complete functions 
# and classes so the AI gets full, unbroken context.
# =====================================================================
def extract_code_blocks(filepath):
    """Reads a Python file and extracts complete functions and classes."""
    with open(filepath, "r", encoding="utf-8") as file:
        source_code = file.read()
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []

    blocks = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            segment = ast.get_source_segment(source_code, node)
            if segment:
                blocks.append({
                    "name": node.name,
                    "type": type(node).__name__,
                    "code": segment,
                    "filepath": filepath
                })
    return blocks


# =====================================================================
# PHASE 2: THE INGESTION ENGINE
# Scans the repo, embeds the blocks, and saves them to Qdrant.
# =====================================================================
def ingest_repository(repo_path):
    print(f"Scanning repository: {repo_path}")
    
    # ARCHITECTURE FIX: We initialize the Qdrant client INSIDE the function.
    # Why? If we put this at the top of the file, just importing this script 
    # in main.py would lock the database file and crash the server.
    qdrant = QdrantClient(path="./qdrant_data") 
    
    # Explicitly delete the old memory and create a fresh one
    if qdrant.collection_exists(collection_name=COLLECTION_NAME):
        print(f"🗑️ Deleting old collection: {COLLECTION_NAME}")
        qdrant.delete_collection(collection_name=COLLECTION_NAME)

    qdrant.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=768, distance=Distance.COSINE),
    )

    points = []
    point_id = 1

    # Walk the directory, extract blocks, and ask Gemini for Math Vectors
    for root, _, files in os.walk(repo_path):
        for file in files:
            if file.endswith(".py") and "venv" not in root:
                filepath = os.path.join(root, file)
                blocks = extract_code_blocks(filepath)
                
                for block in blocks:
                    print(f"Embedding {block['type']}: {block['name']}")
                    
                    response = client.models.embed_content(
                        model="gemini-embedding-001",
                        contents=block["code"],
                        config=types.EmbedContentConfig(output_dimensionality=768),
                    )
                    
                    # Package the math vector + the raw source code (payload)
                    points.append(
                        PointStruct(
                            id=point_id,
                            vector=response.embeddings[0].values,
                            payload={
                                "filepath": block["filepath"],
                                "name": block["name"],
                                "type": block["type"],
                                "code": block["code"]
                            }
                        )
                    )
                    point_id += 1

    if points:
        qdrant.upsert(collection_name=COLLECTION_NAME, points=points)
        print(f"\n✅ Successfully ingested {len(points)} code blocks.")
        
    # Always cleanly close the database connection
    qdrant.close()

# Only run this if the file is executed directly from the terminal
if __name__ == "__main__":
    ingest_repository(".")