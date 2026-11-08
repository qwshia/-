"""RAG 历史记忆向量管理模块"""
import os
import asyncio
from datetime import datetime
import chromadb

# 设置国内镜像以加速本地模型下载
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from langchain_huggingface import HuggingFaceEmbeddings

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_game_memory")
os.makedirs(DB_DIR, exist_ok=True)

# 初始化本地 Embedding 模型（如果本地没有会自动下载）
# 使用轻量级 BGE 模型，适合中文短句检索
embeddings = HuggingFaceEmbeddings(
    model_name="BAAI/bge-small-zh-v1.5",
    model_kwargs={"device": "cpu"},
    encode_kwargs={"normalize_embeddings": True},
)

# 初始化 Chroma 持久化客户端
client = chromadb.PersistentClient(path=DB_DIR)

# 获取或创建两个集合
npc_dialogs = client.get_or_create_collection(
    name="npc_dialogs", 
    embedding_function=embeddings
)
script_summaries = client.get_or_create_collection(
    name="script_summaries", 
    embedding_function=embeddings
)

def add_npc_dialog(thread_id: str, npc_id: str, text: str, metadata: dict):
    """将对话写入向量库"""
    try:
        doc_id = f"{thread_id}_{npc_id}_{datetime.now().timestamp()}"
        meta = {"thread_id": thread_id, "npc_id": npc_id, **metadata}
        npc_dialogs.add(documents=[text], metadatas=[meta], ids=[doc_id])
    except Exception as e:
        print(f"[RAG写入失败] {e}")

def search_npc_dialog(thread_id: str, npc_id: str, query: str, top_k: int = 2) -> str:
    """检索该 NPC 相关的历史对话"""
    try:
        results = npc_dialogs.query(
            query_texts=[query],
            n_results=top_k,
            where={"$and": [{"thread_id": thread_id}, {"npc_id": npc_id}]}
        )
        docs = results.get("documents", [[]])[0]
        if not docs:
            return ""
        return "\n".join([f"- {d}" for d in docs])
    except Exception as e:
        print(f"[RAG检索失败] {e}")
        return ""

def add_script_summary(thread_id: str, task: str, summary: str):
    """将剧本摘要写入向量库"""
    try:
        meta = {"thread_id": thread_id, "task": task}
        script_summaries.add(documents=[summary], metadatas=[meta], ids=[f"{thread_id}_summary"])
    except Exception as e:
        print(f"[RAG剧本摘要写入失败] {e}")

def search_script_summary(query: str, top_k: int = 1) -> str:
    """跨剧本检索相似摘要"""
    try:
        results = script_summaries.query(query_texts=[query], n_results=top_k)
        docs = results.get("documents", [[]])[0]
        return "\n".join(docs)
    except Exception:
        return ""
