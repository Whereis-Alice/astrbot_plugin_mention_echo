"""读出 metadata.yaml 里的 version 字段，供 release workflow 打 tag。

用法：python .github/scripts/read_version.py
只用标准库（runner 上不必额外装 PyYAML），容忍行尾注释和引号；
读不到就以退出码 1 结束，让发布流程明确失败而不是打出一个空 tag。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def read_version(text: str) -> str:
    match = re.search(r"^version:\s*(.+?)\s*(?:#.*)?$", text, re.MULTILINE)
    if not match:
        return ""
    return match.group(1).strip().strip("\"'").strip()


def main() -> int:
    path = Path("metadata.yaml")
    if not path.is_file():
        print("metadata.yaml 不存在", file=sys.stderr)
        return 1
    version = read_version(path.read_text(encoding="utf-8"))
    if not version:
        print("metadata.yaml 里没有可用的 version 字段", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())