"""RAG 历史记忆向量管理模块"""
import os
import uuid
import asyncio
from datetime import datetime
import chromadb

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from langchain_huggingface import HuggingFaceEmbeddings

# === 新增适配器：包装 LangChain 的 Embeddings，兼容 ChromaDB 新版本 ===
class ChromaEmbeddingAdapter:
    def __init__(self, model_name):
        self._model = HuggingFaceEmbeddings(
            model_name=model_name,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )

    def name(self) -> str:
        # ChromaDB 强校验的方法
        return "custom_huggingface_embedding"

    def __call__(self, input):
        # ChromaDB 实际调用生成向量的方法
        return self._model.embed_documents(input)

    def embed_query(self, input):
        if isinstance(input, list):
            return self._model.embed_documents(input)
        return self._model.embed_query(input)

# === 使用适配器初始化 ===
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_game_memory")
os.makedirs(DB_DIR, exist_ok=True)

embedding_adapter = ChromaEmbeddingAdapter("BAAI/bge-small-zh-v1.5")

client = chromadb.PersistentClient(path=DB_DIR)

npc_dialogs = client.get_or_create_collection(
    name="npc_dialogs",
    embedding_function=embedding_adapter
)
script_summaries = client.get_or_create_collection(
    name="script_summaries",
    embedding_function=embedding_adapter
)

def add_npc_dialog(thread_id: str, npc_id: str, text: str, metadata: dict):
    """将对话写入向量库"""
    try:
        doc_id = str(uuid.uuid4())
        meta = {"thread_id": thread_id, "npc_id": npc_id, **metadata}
        npc_dialogs.add(documents=[text], metadatas=[meta], ids=[doc_id])
    except Exception as e:
        print(f"[RAG写入失败] {e}")

async def search_npc_dialog(thread_id: str, npc_id: str, query: str, top_k: int = 2) -> str:
    """检索该 NPC 相关的历史对话"""
    if not isinstance(query, str):
        query = str(query)
        
    # 1. 剔除所有标点符号和特殊字符
    import re
    query = re.sub(r"[^\w\u4e00-\u9fa5]", "", query)
    
    # 2. 建立超强停用词表，屏蔽代词、语气词、高频无实意词以及指令词
    _stopwords = [
        "的", "了", "和", "是", "就", "都", "而", "及", "与", "着", "或", "一个", "没有", "我们", "你们", "他们", 
        "吗", "呢", "吧", "啊", "哦", "嗯", "呀", "哪", "谁", "什么", "怎么", "这个", "那个", "这样", "那样",
        "我", "你", "他", "她", "它", "这", "那", "不", "在", "人", "上", "下", "有", "个", "到", "说", "要", 
        "去", "会", "看", "好", "自己", "没", "过", "想", "能", "多", "还", "把", "被", "让", "给", "向", "往",
        "给我", "给你", "拿去", "拿过来", "递给我", "脱掉", "穿上", "过来", "收下", "交出", "给我吧", "给你吧"
    ]
    for w in _stopwords:
        query = query.replace(w, "")
        
    query = query.strip()
    
    # 3. 如果过滤完只剩下 2 个字符以内，说明全无实意，直接返回空，不浪费向量检索性能
    if len(query) < 2:
        return ""

    def _query():
        return npc_dialogs.query(
            query_texts=[query],
            n_results=top_k,
            where={"$and": [{"thread_id": thread_id}, {"npc_id": npc_id}]}
        )
    try:
        results = await asyncio.to_thread(_query)
        docs = results.get("documents", [[]])[0]
        distances = results.get("distances", [[]])[0] if results.get("distances") else []
        if not docs:
            return ""
        
        # 距离阈值截断：ChromaDB 默认 L2 距离，越大越不相关。仅保留距离 < 1.2 的结果
        filtered_docs = []
        for i, d in enumerate(docs):
            if distances and i < len(distances) and distances[i] > 1.2:
                continue
            filtered_docs.append(d)
            
        if not filtered_docs:
            return ""
        return "\n".join([f"- {d}" for d in filtered_docs])
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[RAG检索失败] {e}")
        return ""

def add_script_summary(thread_id: str, task: str, summary: str):
    """将剧本摘要写入向量库"""
    try:
        doc_id = str(uuid.uuid4())
        meta = {"thread_id": thread_id, "task": task}
        script_summaries.add(documents=[summary], metadatas=[meta], ids=[doc_id])
    except Exception as e:
        print(f"[RAG剧本摘要写入失败] {e}")

async def search_script_summary(query: str, top_k: int = 1) -> str:
    """跨剧本检索相似摘要"""
    if not isinstance(query, str):
        query = str(query)
    def _query():
        return script_summaries.query(query_texts=[query], n_results=top_k)
    try:
        results = await asyncio.to_thread(_query)
        docs = results.get("documents", [[]])[0]
        return "\n".join(docs)
    except Exception:
        return ""
