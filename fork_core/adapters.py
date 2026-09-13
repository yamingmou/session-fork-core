"""fork_core.adapters — adapter 注册表（内置 WorkBuddy / Claude Code 适配器）。

用法：
    from fork_core.adapters import get_adapter
    adapter = get_adapter("workbuddy")

⚠️ 布局约束（2026-09-14）：WorkBuddy 开放平台要求技能包**最多两级目录**
（根目录/二级目录/文件），所以 adapter 不再是 `adapters/` 子包，而是与本文件
同级的 `adapter_*.py` 平铺模块。新增产品适配器时请沿用 `adapter_<product>.py`
命名，勿再建子目录，否则发布包会被平台拒绝解析。
"""

from .adapter_base import TranscriptionAdapter

_REGISTRY: dict[str, TranscriptionAdapter] = {}


def register(adapter: TranscriptionAdapter) -> TranscriptionAdapter:
    _REGISTRY[adapter.name] = adapter
    return adapter


def get_adapter(name: str = "workbuddy") -> TranscriptionAdapter:
    """按名称取 adapter；未注册则动态导入。"""
    if name in _REGISTRY:
        return _REGISTRY[name]
    if name == "workbuddy":
        from .adapter_workbuddy import WorkBuddyAdapter
        return register(WorkBuddyAdapter())
    if name == "claude-code":
        from .adapter_claude_code import ClaudeCodeAdapter
        return register(ClaudeCodeAdapter())
    raise SystemExit(f"Unknown adapter: {name!r} (available: workbuddy, claude-code)")


def available() -> list[str]:
    return ["workbuddy", "claude-code"]
