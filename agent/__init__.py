from .llm import LLMClient, call_llm, get_default_client, set_default_client
from .memory import Memory
from .tools import ToolRegistry, default_registry

__all__ = [
    "LLMClient",
    "call_llm",
    "get_default_client",
    "set_default_client",
    "Memory",
    "ToolRegistry",
    "default_registry",
    "Agent",
]


def __getattr__(name):
    if name == "Agent":
        from .main import Agent
        return Agent
    raise AttributeError(f"module 'agent' has no attribute {name!r}")