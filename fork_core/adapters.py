"""fork_core.adapters — adapter 注册表（内置各产品适配器）。

用法：
    from fork_core.adapters import get_adapter
    adapter = get_adapter("workbuddy")

⚠️ 布局约束（2026-09-14）：WorkBuddy 开放平台要求技能包**最多两级目录**
（根目录/二级目录/文件），所以 adapter 不再是 `adapters/` 子包，而是与本文件
同级的 `adapter_*.py` 平铺模块。新增产品适配器时请沿用 `adapter_<product>.py`
命名，勿再建子目录，否则发布包会被平台拒绝解析。

⚠️ 单一事实源（v2.4.8 起）：产品清单只登记在 `_SPECS` 一处，`get_adapter()` 的分发
与 `available()` 的列表都由它派生——历史上这两处 + 错误提示串是三份独立硬编码，
加产品时漏改一处就会"已知产品但报未知 adapter"。新增产品只需在 `_SPECS` 加一行。
"""

import os

from .adapter_base import TranscriptionAdapter

_REGISTRY: dict[str, TranscriptionAdapter] = {}

#: 产品名 → (模块名, 类名)。模块与本文件同级平铺。
#: 「有没有实现」以模块文件是否存在为准 —— 这样"已宣称未实现"的产品不会
#: 出现在 CLI 的 --adapter 可选项里（避免给出一个必然 ImportError 的选择）。
_SPECS: dict[str, tuple[str, str]] = {
    "workbuddy": ("adapter_workbuddy", "WorkBuddyAdapter"),
    "claude-code": ("adapter_claude_code", "ClaudeCodeAdapter"),
    "pi": ("adapter_pi", "PiAdapter"),
    "codex": ("adapter_codex", "CodexAdapter"),
    "hermes": ("adapter_hermes", "HermesAdapter"),
}

#: 展示/报错顺序（含多后端产品 openclaw）
_ORDER: tuple[str, ...] = (
    "workbuddy",
    "claude-code",
    "pi",
    "openclaw",
    "codex",
    "hermes",
)

#: 同一产品存在多个介质后端的产品（不在 _SPECS 里，走各自的分支）
_MULTI_BACKEND: tuple[str, ...] = ("openclaw",)


def _module_present(module_name: str) -> bool:
    """模块文件是否与本文件同级存在（只查文件，不导入，无副作用）。"""
    return os.path.isfile(os.path.join(os.path.dirname(__file__), f"{module_name}.py"))


def register(adapter: TranscriptionAdapter) -> TranscriptionAdapter:
    _REGISTRY[adapter.name] = adapter
    return adapter


def available() -> list[str]:
    """当前**真的可用**的产品名（未实现模块不列出）。"""
    out: list[str] = []
    for name in _ORDER:
        if name in _MULTI_BACKEND:
            out.append(name)
        elif name in _SPECS and _module_present(_SPECS[name][0]):
            out.append(name)
    return out


def _openclaw() -> TranscriptionAdapter:
    """OpenClaw 两种介质（同一产品的两个世代），按环境自动选后端：

    - ≤2026.6.x → `sessions/*.jsonl`（JSONL 后端）
    - ≥2026.9.x → `agents/<id>/agent/openclaw-agent.sqlite`（SQLite 后端）

    也可强制：`FORK_OPENCLAW_BACKEND=jsonl|sqlite`
    """
    from .adapter_openclaw import OpenClawJsonlAdapter
    from .adapter_openclaw_sqlite import OpenClawSqliteAdapter

    want = (os.environ.get("FORK_OPENCLAW_BACKEND") or "auto").lower()
    if want in ("auto", "sqlite"):
        sqlite_adapter = OpenClawSqliteAdapter()
        if sqlite_adapter.is_available():
            return register(sqlite_adapter)
        if want == "sqlite":
            raise SystemExit(
                f"FORK_OPENCLAW_BACKEND=sqlite 但未发现可用转录库：{sqlite_adapter.DB_PATH}"
            )
    return register(OpenClawJsonlAdapter())


def get_adapter(name: str = "workbuddy") -> TranscriptionAdapter:
    """按名称取 adapter；未注册则动态导入。"""
    if name in _REGISTRY:
        return _REGISTRY[name]
    if name in _MULTI_BACKEND:
        return _openclaw()
    spec = _SPECS.get(name)
    if spec is not None:
        module_name, class_name = spec
        if not _module_present(module_name):
            raise SystemExit(
                f"Adapter {name!r} is declared but not implemented yet "
                f"(missing fork_core/{module_name}.py). available: {', '.join(available())}"
            )
        import importlib

        module = importlib.import_module(f".{module_name}", package=__package__)
        return register(getattr(module, class_name)())
    raise SystemExit(f"Unknown adapter: {name!r} (available: {', '.join(available())})")
