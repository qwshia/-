"""记忆压缩工具：把对话/摘要压成短文本，供 NPC 个人记忆分层使用。"""
from langchain_core.messages import HumanMessage

from config import memory_llm


async def _compress(texts: list) -> str:
    prompt = "把以下对话压缩成一段简短摘要（100字内），保留关键信息：\n" + "\n".join(texts)
    try:
        r = await memory_llm.ainvoke([HumanMessage(content=prompt)])
        return (r.content or "").strip() or "\n".join(texts)[:200]
    except Exception:
        return "\n".join(texts)[:200]


async def _compress_two(a: str, b: str) -> str:
    parts = [x for x in (a, b) if x]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    prompt = "把以下两段摘要合并成一段简短摘要（200字内）：\n" + "\n---\n".join(parts)
    try:
        r = await memory_llm.ainvoke([HumanMessage(content=prompt)])
        return (r.content or "").strip() or " | ".join(parts)[:300]
    except Exception:
        return " | ".join(parts)[:300]
