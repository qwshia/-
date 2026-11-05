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

# 四层模型分配
WORLD_MODEL = os.getenv("WORLD_MODEL", "qwen3.8-max-0902")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "deepseek-v4.1-flash")
ACTOR_MODEL = os.getenv("ACTOR_MODEL", "qwen3.8-omni-flash")
MEMORY_MODEL = os.getenv("MEMORY_MODEL", "qwen3.8-27b")

# 全局强制关闭深度思考
def _thinking_extra(model, base_url):
    # 无论什么模型，统统塞入关闭思考的参数（兼容各大厂商字段）
    return {
        "thinking": {"type": "disabled"},
        "enable_thinking": False
    }

# 1. 导演（温度低，保证 JSON 稳定不乱编）
world_llm = ChatOpenAI(
    model=WORLD_MODEL, api_key=LLM_API_KEY, base_url=LLM_BASE_URL,
    temperature=0.2, timeout=300, max_retries=0,
    extra_body=_thinking_extra(WORLD_MODEL, LLM_BASE_URL),
    stream_usage=True,
)

# 2. 审查员（温度极低，保证提取线索绝对客观）
judge_llm = ChatOpenAI(
    model=JUDGE_MODEL, api_key=LLM_API_KEY, base_url=LLM_BASE_URL,
    temperature=0.1, timeout=120, max_retries=0,
    extra_body=_thinking_extra(JUDGE_MODEL, LLM_BASE_URL),
    stream_usage=True,
)

# 3. 演员（温度高，保证 NPC 说话有感情、不死板）
actor_llm = ChatOpenAI(
    model=ACTOR_MODEL, api_key=LLM_API_KEY, base_url=LLM_BASE_URL,
    temperature=0.85, timeout=120, max_retries=0,
    extra_body=_thinking_extra(ACTOR_MODEL, LLM_BASE_URL),
    stream_usage=True,
)

# 4. 记忆压缩（普通温度，快速总结）
memory_llm = ChatOpenAI(
    model=MEMORY_MODEL, api_key=LLM_API_KEY, base_url=LLM_BASE_URL,
    temperature=0.3, timeout=60, max_retries=0,
    extra_body=_thinking_extra(MEMORY_MODEL, LLM_BASE_URL),
    stream_usage=True,
)