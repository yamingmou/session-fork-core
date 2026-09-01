"""fork_core.adapters — adapter 注册表。

用法：
    from fork_core.adapters import get_adapter
    adapter = get_adapter("workbuddy")
"""

from .base import TranscriptionAdapter

_REGISTRY: dict[str, TranscriptionAdapter] = {}


def register(adapter: TranscriptionAdapter) -> TranscriptionAdapter:
    _REGISTRY[adapter.name] = adapter
    return adapter


def get_adapter(name: str = "workbuddy") -> TranscriptionAdapter:
    """按名称取 adapter；未注册则动态导入。"""
    if name in _REGISTRY:
        return _REGISTRY[name]
    if name == "workbuddy":
        from .workbuddy import WorkBuddyAdapter
        return register(WorkBuddyAdapter())
    if name == "claude-code":
        from .claude_code import ClaudeCodeAdapter
        return register(ClaudeCodeAdapter())
    raise SystemExit(f"Unknown adapter: {name!r} (available: workbuddy, claude-code)")


def available() -> list[str]:
    return ["workbuddy", "claude-code"]
