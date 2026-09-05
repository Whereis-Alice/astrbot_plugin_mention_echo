"""从 CHANGELOG.md 里抽出指定版本的正文，供 release workflow 填进 Release 说明。

用法：python .github/scripts/extract_changelog.py v1.0.0
匹配 `## [v1.0.0] - YYYY-MM-DD` 到下一个 `## [` 之间的内容；找不到就输出兜底文案，
永远以退出码 0 结束，不让文档格式问题卡住发布流程。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FALLBACK = "详见 CHANGELOG.md。"


def extract(content: str, version: str) -> str:
    pattern = rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)"
    match = re.search(pattern, content, re.DOTALL | re.MULTILINE)
    if not match:
        return ""
    body = match.group(1)
    # 去掉尾部的链接引用行（[v1.0.0]: https://... 之类），它们对 Release 说明没用
    lines = [line for line in body.splitlines() if not re.match(r"^\[[^\]]+\]:\s+\S+", line)]
    return "\n".join(lines).strip()


def main(argv: list[str]) -> int:
    version = argv[1] if len(argv) > 1 else ""
    path = Path("CHANGELOG.md")
    if not version or not path.is_file():
        print(FALLBACK)
        return 0
    body = extract(path.read_text(encoding="utf-8"), version)
    print(body or FALLBACK)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))