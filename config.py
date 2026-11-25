"""Config layer."""
import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

try:
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    _SQLITE_OK = True
except Exception:
    _SQLITE_OK = False

# 统一配置读取
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")

# 导演/审查员/场景布局通道（火山方舟，doubao）
ACTOR_BASE_URL = os.getenv("ACTOR_BASE_URL", LLM_BASE_URL)
ACTOR_API_KEY = os.getenv("ACTOR_API_KEY", LLM_API_KEY)

# NPC 演员通道（OpenRouter 免费模型池，独立 API Key，需自行填写）
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "在这里填写你的OpenRouter密钥")

# 四层模型分配
WORLD_MODEL = os.getenv("WORLD_MODEL", "doubao-seed-evolving")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "doubao-seed-evolving")
ACTOR_MODEL = os.getenv("ACTOR_MODEL", "qwen/qwen3-next-80b-a3b-instruct:free")
MEMORY_MODEL = os.getenv("MEMORY_MODEL", "qwen3.8-flash")

# NPC 演员候选池：主模型 429/额度用尽后，按此顺序依次 fallback
ACTOR_FALLBACK_MODELS = [
    "sao10k/l3.1-euryale-70b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
    "arliai/qwq-32b-arliai-rpr-v1:free",
    "dots-studio/dots-3-note-preview:free",
    "google/gemma-4-26b-a4b-it:free",
    "aion-labs/aion-3.5-mini:free",
]

# 全局强制关闭深度思考
def _thinking_extra(model, base_url):
    # Doubao-Seed-Character 默认不思考，传 thinking 参数可能报错
    if "doubao-seed-character" in str(model).lower():
        return {}
    # Doubao-Seed-Evolving 默认开深度思考，必须显式关闭，否则结构化 JSON 任务延迟暴涨
    if "doubao-seed-evolving" in str(model).lower():
        return {"thinking": {"type": "disabled"}}
    # 已有的 Dots3 判断保留
    if "dots" in str(model).lower() or "openrouter" in str(base_url).lower():
        return {}
    return {
        "thinking": {"type": "disabled"},
        "enable_thinking": False
    }

# 1. 导演（Doubao-Seed-Evolving，走火山通道，温度低保证 JSON 稳定）
world_llm = ChatOpenAI(
    model=WORLD_MODEL, api_key=ACTOR_API_KEY, base_url=ACTOR_BASE_URL,
    temperature=0.2, timeout=300, max_retries=3, max_tokens=8192,
    extra_body=_thinking_extra(WORLD_MODEL, ACTOR_BASE_URL),
    stream_usage=True,
)

# 2. 审查员（Doubao-Seed-Evolving，走火山通道，温度极低保证线索提取客观）
judge_llm = ChatOpenAI(
    model=JUDGE_MODEL, api_key=ACTOR_API_KEY, base_url=ACTOR_BASE_URL,
    temperature=0.1, timeout=120, max_retries=3,
    extra_body=_thinking_extra(JUDGE_MODEL, ACTOR_BASE_URL),
    stream_usage=True,
)

# 3. 演员（OpenRouter 免费模型池：主模型 + 候选池按序 fallback）
def _make_actor_llm(model: str):
    return ChatOpenAI(
        model=model, api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL,
        temperature=0.85, timeout=120, max_retries=1,
        stream_usage=True,
    )

actor_llm = _make_actor_llm(ACTOR_MODEL)
if ACTOR_FALLBACK_MODELS:
    actor_llm = actor_llm.with_fallbacks([_make_actor_llm(m) for m in ACTOR_FALLBACK_MODELS])

# 5. 记忆提炼（极轻量，用于生活层更新）
memory_llm = ChatOpenAI(
    model=MEMORY_MODEL, api_key=LLM_API_KEY, base_url=LLM_BASE_URL,
    temperature=0.3, timeout=60, max_retries=3, max_tokens=2048,
    extra_body=_thinking_extra(MEMORY_MODEL, LLM_BASE_URL),
    stream_usage=True,
)