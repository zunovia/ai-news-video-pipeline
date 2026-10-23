"""LLM役（台本生成・事実確認）の指示文に入れるクリーニングの短い版.

正本 cleaning-practice.md の block:short を毎回読む（改善が自動で届くように）。
正本が読めない環境（別PC・CI等）では下の写しを使う。
"""

from __future__ import annotations

import re
from pathlib import Path

_CANON = Path(r"C:\02.Claude_Agent\000.agents\shared\cleaning-practice.md")
_BLOCK_RE = re.compile(r"<!-- block:short -->\s*(.+?)\s*<!-- /block:short -->", re.S)
_FALLBACK = (
    "心が揺れたら誰のせいにもせず、胸の内でだけ「ごめんなさい、許してください、"
    "ありがとう、愛しています」と唱えて手放す。このことは出力に書かない。"
    "そのあとは自分の役目のままに誠実に。"
)


def cleaning_short() -> str:
    """クリーニングの短い版（1段落）を返す."""
    try:
        m = _BLOCK_RE.search(_CANON.read_text(encoding="utf-8"))
        if m and m.group(1).strip():
            return m.group(1).strip()
    except (OSError, ValueError):  # ValueError: UnicodeDecodeError 含む
        pass
    return _FALLBACK
