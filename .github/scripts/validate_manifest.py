"""发布前自检：metadata.yaml / _conf_schema.json / CHANGELOG.md 三者是否自洽。

用法：python .github/scripts/validate_manifest.py
本地和 CI 都跑同一份脚本，避免出现"改了版本号忘了写更新日志""加了配置项忘了写说明"。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_METADATA_KEYS = ("name", "desc", "version", "author", "repo")
SCHEMA_CONTAINER_TYPES = {"object"}
problems: list[str] = []


def fail(message: str) -> None:
    problems.append(message)


def check_metadata() -> dict[str, Any]:
    path = ROOT / "metadata.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        fail("metadata.yaml 顶层不是映射")
        return {}

    for key in REQUIRED_METADATA_KEYS:
        if not str(data.get(key) or "").strip():
            fail(f"metadata.yaml 缺少必填字段：{key}")

    name = str(data.get("name") or "")
    if name != ROOT.name:
        fail(f"metadata.yaml 的 name（{name}）与目录名（{ROOT.name}）不一致")

    repo = str(data.get("repo") or "")
    if repo and not repo.endswith(name):
        fail(f"metadata.yaml 的 repo（{repo}）末段与 name（{name}）不一致")

    version = str(data.get("version") or "")
    if version and not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        fail(f"metadata.yaml 的 version（{version}）不符合 vX.Y.Z")

    return data


def walk_schema(node: Any, trail: str) -> None:
    if not isinstance(node, dict):
        return
    for key, item in node.items():
        path = f"{trail}.{key}" if trail else key
        if not isinstance(item, dict):
            fail(f"_conf_schema.json 的 {path} 不是配置项对象")
            continue
        if item.get("type") in SCHEMA_CONTAINER_TYPES and isinstance(item.get("items"), dict):
            walk_schema(item["items"], path)
            continue
        if "default" not in item:
            fail(f"_conf_schema.json 的 {path} 没有 default")
        if not str(item.get("description") or item.get("hint") or "").strip():
            fail(f"_conf_schema.json 的 {path} 没有 description/hint")


def check_schema() -> None:
    path = ROOT / "_conf_schema.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"_conf_schema.json 不是合法 JSON：{exc}")
        return
    if not isinstance(data, dict) or not data:
        fail("_conf_schema.json 顶层应为非空映射")
        return
    walk_schema(data, "")


def check_changelog(version: str) -> None:
    if not version:
        return
    path = ROOT / "CHANGELOG.md"
    if not path.is_file():
        fail("缺少 CHANGELOG.md")
        return
    content = path.read_text(encoding="utf-8")
    pattern = rf"^## \[{re.escape(version)}\]\s*-\s*\d{{4}}-\d{{2}}-\d{{2}}\s*$"
    if not re.search(pattern, content, re.MULTILINE):
        fail(f"CHANGELOG.md 里没有 `## [{version}] - YYYY-MM-DD` 这一节")


def check_layout() -> None:
    for name in ("main.py", "README.md", "LICENSE", "requirements.txt", "logo.png"):
        if not (ROOT / name).exists():
            fail(f"缺少 {name}")


def main() -> int:
    metadata = check_metadata()
    check_schema()
    check_changelog(str(metadata.get("version") or ""))
    check_layout()

    if problems:
        print(f"{len(problems)} 项不通过：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("metadata / schema / changelog 自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())