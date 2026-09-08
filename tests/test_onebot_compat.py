"""OneBot 消息与 API 兼容自测（不依赖 pytest 或网络）。

覆盖 AstrBot 的 aiocqhttp 适配器以及 LLOneBot / LLBot / SnowLuma 常见的原始
OneBot v11 负载形态：标准 ``at`` / ``image`` 段、JSON 字符串形式的
``messageChain``，还有同步、关键字形式的 ``call_api``。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _ensure_astrbot() -> None:
    """让本文件在没有安装 AstrBot 的 CI 环境里也能导入 mixin。"""
    try:
        import astrbot.api  # noqa: F401
        import astrbot.api.message_components  # noqa: F401
    except Exception:
        pass
    else:
        return

    class _NullLogger:
        def _noop(self, *args: object, **kwargs: object) -> None:
            return None

        debug = info = warning = error = exception = critical = _noop

    class _Plain:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Image:
        @classmethod
        def fromURL(cls, value: str):
            return value

        @classmethod
        def fromFileSystem(cls, value: str):
            return value

    package = types.ModuleType("astrbot")
    package.__path__ = []  # type: ignore[attr-defined]
    api = types.ModuleType("astrbot.api")
    api.__path__ = []  # type: ignore[attr-defined]
    api.logger = _NullLogger()  # type: ignore[attr-defined]
    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = _Plain  # type: ignore[attr-defined]
    components.Image = _Image  # type: ignore[attr-defined]
    package.api = api  # type: ignore[attr-defined]
    api.message_components = components  # type: ignore[attr-defined]
    sys.modules.update(
        {
            "astrbot": package,
            "astrbot.api": api,
            "astrbot.api.message_components": components,
        }
    )


_ensure_astrbot()

from modules.data import DataMixin  # noqa: E402
from modules.message import MessageMixin  # noqa: E402
from modules.rendering import RenderingMixin  # noqa: E402

_FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _FAILURES.append(label)


class _Harness(RenderingMixin, DataMixin, MessageMixin):
    """只组装被测 mixin；所有解析和协议调用均走生产代码。"""

    def _assume_sent_on_timeout(self, exc: BaseException, action: str) -> bool:
        return False


class _MessageObject:
    def __init__(self, raw_message: Any = None) -> None:
        self.raw_message = raw_message
        self.message = []


class _Event:
    def __init__(self, raw_message: Any = None, raw_event: dict[str, Any] | None = None, bot: Any = None) -> None:
        self.message_obj = _MessageObject(raw_message)
        self.raw_event = raw_event
        self.bot = bot


def test_llbot_standard_segments() -> None:
    print("LLBot 标准 OneBot v11 at / image 段可被完整识别")
    raw = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 932436510,
        "user_id": 2127074778,
        "self_id": 3642667411,
        "message": [
            {"type": "at", "data": {"qq": "3642667411", "name": "机器人"}},
            {"type": "text", "data": {"text": "请看一下"}},
            {"type": "image", "data": {"file": "ABC.jpg", "url": "https://example.test/image.jpg"}},
        ],
    }
    plugin = _Harness()
    event = _Event(raw)
    segments = plugin._raw_message_segments(event)

    check([segment.get("type") for segment in segments] == ["at", "text", "image"], "读取标准 message 段")
    check(plugin._mentions(event) == ["3642667411"], "识别 at.qq")
    check(plugin._segments_images(segments) == ["https://example.test/image.jpg"], "识别 image.url")
    check(plugin._group_id(event) == "932436510", "识别 group_id")
    check(plugin._sender_id(event) == "2127074778", "识别顶层 user_id")
    check(plugin._self_id(event) == "3642667411", "识别顶层 self_id")


def test_snowluma_json_message_chain() -> None:
    print("SnowLuma 风格的 messageChain JSON 与顶层身份字段可被识别")
    raw_event = {
        "groupId": "10001",
        "senderId": "20002",
        "botId": "30003",
        "messageChain": json.dumps(
            [
                {"type": "mention_user", "data": {"userId": "30003"}},
                {"type": "text", "data": {"text": "补课消息"}},
                {"type": "image", "data": {"imageUrl": "https://example.test/snowluma.png"}},
            ]
        ),
    }
    plugin = _Harness()
    event = _Event(raw_event=raw_event)
    segments = plugin._raw_message_segments(event)

    check(len(segments) == 3, "从 JSON 字符串解析 messageChain")
    check(plugin._mentions(event) == ["30003"], "识别 mention_user.userId")
    check(plugin._segments_images(segments) == ["https://example.test/snowluma.png"], "识别 imageUrl")
    check(plugin._group_id(event) == "10001", "识别 groupId")
    check(plugin._sender_id(event) == "20002", "识别 senderId")
    check(plugin._self_id(event) == "30003", "识别 botId")


def test_keyword_only_sync_call_api() -> None:
    print("同步且仅接受关键字 action 的 call_api 也能用于 OneBot 查询")
    calls: list[tuple[str, dict[str, Any]]] = []

    class _Api:
        def call_api(self, *, action: str, **kwargs: Any):
            calls.append((action, kwargs))
            return {"data": {"ok": True}}

    class _Bot:
        api = _Api()

    plugin = _Harness()
    event = _Event({"self_id": "30003"}, bot=_Bot())
    result = asyncio.run(plugin._call_onebot_action(event, "get_image", file="ABC.jpg"))

    check(result == {"data": {"ok": True}}, "同步 call_api 的返回值会原样返回")
    check(calls == [("get_image", {"file": "ABC.jpg", "self_id": "30003"})], "关键字 action 和 self_id 正确传入")


def test_generic_forward_action_fallback() -> None:
    print("群专用转发动作不可用时，回退 LLBot 可用的 send_forward_msg")
    calls: list[tuple[str, dict[str, Any]]] = []

    class _Api:
        def call_api(self, *, action: str, **kwargs: Any):
            calls.append((action, kwargs))
            if action == "send_group_forward_msg":
                raise RuntimeError("unsupported action")
            if action == "send_forward_msg":
                return {"status": "ok"}
            raise RuntimeError(action)

    class _Bot:
        api = _Api()

    async def bot_name(event, group_id):
        return "艾特回声"

    plugin = _Harness()
    plugin._bot_name = bot_name
    event = _Event({"group_id": "10001", "self_id": "30003"}, bot=_Bot())
    sent = asyncio.run(plugin._try_send_forward_images(event, "", ["one.jpg", "two.jpg"]))

    check(sent is True, "通用转发动作成功后不回退为逐图发送")
    check([action for action, _ in calls] == ["send_group_forward_msg", "send_forward_msg"], "按专用动作再通用动作的顺序尝试")
    check(calls[-1][1].get("message_type") == "group", "通用动作明确标记 group")


def main() -> int:
    for test in (
        test_llbot_standard_segments,
        test_snowluma_json_message_chain,
        test_keyword_only_sync_call_api,
        test_generic_forward_action_fallback,
    ):
        test()
    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} 项失败：")
        for item in _FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
