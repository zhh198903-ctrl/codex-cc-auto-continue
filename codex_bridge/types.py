from dataclasses import dataclass, field
from typing import Optional
import re
_MD_NOISE = None

@dataclass
class Ask:
    id: str
    session_id: str
    payload: dict                       # 不含 cmd；v2.build_ask 加上
    nums: list[int]                     # 手表第 i 项 → 屏幕编号
    inputs: set = field(default_factory=set)    # 哪几项是自己输入行（手表下标）
    layout: str = "list"                # list | preview（preview 里数字键只移光标，要补 Enter）
    multi: bool = False
    tabs: bool = False                  # 有页签（nav next/prev 才有意义）
    esc: bool = False
    step: str = ""
    checks: tuple = ()                  # 各项勾选（多选题按完核对用）


@dataclass
class Prompt:
    id: str
    session_id: str
    tool: str
    hint: str
    created_at: float
    kind: str = "perm"          # perm(hooks) | screen(读屏) | act(借道动作)
    decision: Optional[str] = None   # once | deny


@dataclass(frozen=True)
class IndexRow:
    pid: int
    session_id: str
    cwd: str
    name: str
    status: str            # idle | busy | shell | unknown
    updated_at: float      # epoch 秒
    started_at: float      # epoch 秒
    kind: str = "interactive"
    version: str = ""



def clip(text: str, limit: int) -> str:
    """按 UTF-8 字节截断（不切半个字），截了就补「…」。"""
    text = text.strip()
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[:max(0, limit - 3)]
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    if cut and cut[-1] >= 0xC0:                        # 多字节字的首字节也去掉
        cut = cut[:-1]
    return cut.decode("utf-8", "ignore").rstrip() + "…"


def clean_text(text: str, limit: int = 400) -> str:
    """把 assistant 回复压成手表能显示的一行：去 markdown 记号、表格线、代码围栏，折叠空白。"""
    global _MD_NOISE
    import re
    if _MD_NOISE is None:
        _MD_NOISE = re.compile(r"```[a-zA-Z0-9_-]*|[*_`#>]+|\|[-:| ]+\||\|")
    t = _MD_NOISE.sub(" ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]
