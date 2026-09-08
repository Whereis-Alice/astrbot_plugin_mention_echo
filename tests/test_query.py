"""艾特回声：查询入口的自测（不依赖 pytest，直接 python tests/test_query.py）。

v1.2.0 把「艾特回顾」并进了「谁艾特我」，这个文件锁死合并之后的约定：

  1. 只有一条查询正则 ``QUERY_PATTERN``，条数写在命令里（``谁艾特我 3``）；
  2. ``艾特回顾 / 回顾艾特 / 艾特补课 / at_recap / catch_up`` 退化成纯别名，
     没点名就查自己，带 @ 就查对方；
  3. 上下文展不展示由 ``query_context_mode`` 三态（auto/always/never）决定；
     ``always`` 还会为之后的新艾特自动采集查询上下文，提醒（reminder）路径仍是独立配置。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _ensure_astrbot() -> None:
    """让本文件在没装 AstrBot 的环境（例如 CI）里也能 ``import main``。"""
    try:
        import astrbot.api.event  # noqa: F401
        import astrbot.api.star  # noqa: F401
    except Exception:
        pass
    else:
        return

    class _NullLogger:
        def _noop(self, *args: object, **kwargs: object) -> None:
            return None

        debug = info = warning = error = exception = critical = _noop

    class _Stub:
        """既能当基类又能随便实例化的占位类。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

    class _EventMessageType:
        GROUP_MESSAGE = "group"
        PRIVATE_MESSAGE = "private"
        ALL = "all"

    def _passthrough(*args: object, **kwargs: object):
        def decorator(func):
            return func

        return decorator

    class _Filter:
        EventMessageType = _EventMessageType
        event_message_type = staticmethod(_passthrough)
        llm_tool = staticmethod(_passthrough)
        command = staticmethod(_passthrough)

    package = types.ModuleType("astrbot")
    package.__path__ = []  # type: ignore[attr-defined]
    api = types.ModuleType("astrbot.api")
    api.__path__ = []  # type: ignore[attr-defined]
    api.logger = _NullLogger()  # type: ignore[attr-defined]
    api.AstrBotConfig = _Stub  # type: ignore[attr-defined]
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = _Stub  # type: ignore[attr-defined]
    event.filter = _Filter  # type: ignore[attr-defined]
    star = types.ModuleType("astrbot.api.star")
    star.Context = _Stub  # type: ignore[attr-defined]
    star.Star = _Stub  # type: ignore[attr-defined]
    star.StarTools = _Stub  # type: ignore[attr-defined]
    components = types.ModuleType("astrbot.api.message_components")

    package.api = api  # type: ignore[attr-defined]
    api.event = event  # type: ignore[attr-defined]
    api.star = star  # type: ignore[attr-defined]
    api.message_components = components  # type: ignore[attr-defined]
    sys.modules.update(
        {
            "astrbot": package,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.api.message_components": components,
        }
    )


_ensure_astrbot()

import main  # noqa: E402
from modules import constants  # noqa: E402
from modules.constants import (  # noqa: E402
    QUERY_PATTERN,
    QUERY_RECENT_DEFAULT,
    QUERY_RECENT_MAX,
    QUERY_SELF_ALIAS_PATTERN,
)

_FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _FAILURES.append(label)


class _Event:
    """只提供 _sender_id 需要的那一个方法。"""

    def __init__(self, sender_id: str = "111111") -> None:
        self._sender_id_value = sender_id

    def get_sender_id(self) -> str:
        return self._sender_id_value


def _plugin(**record_config: Any) -> main.MentionEchoPlugin:
    """绕开 Star.__init__ 造一个只带配置的插件实例。"""
    plugin = object.__new__(main.MentionEchoPlugin)
    plugin.config = {"record": dict(record_config)}
    return plugin


def _count(match) -> str | None:
    return match.groupdict().get("count") if match else None


def _record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "user_id": "222222",
        "name": "小张",
        "message": "记得看一下这个",
        "time": 1700000000,
        "is_context": False,
        "before": [{"user_id": "333333", "name": "小李", "message": "前一条", "time": 1699999990}],
        "after": [{"user_id": "444444", "name": "小王", "message": "后一条", "time": 1700000010}],
    }
    record.update(overrides)
    return record


def _message_count(blocks: list[dict[str, Any]]) -> int:
    return sum(len(block.get("msgs") or []) for block in blocks)

def test_one_pattern_understands_both_wordings() -> None:
    print("查询正则同时认识「谁艾特我」和「艾特回顾」两种说法")
    for text in (
        "谁艾特我",
        "谁at我",
        "谁@我",
        "谁艾特他",
        "哪个逼艾特我",
        "艾特回顾",
        "回顾艾特",
        "艾特补课",
        "at回顾",
        "at_recap",
        "catch_up",
        "catchup",
        "CATCH_UP",
    ):
        check(QUERY_PATTERN.match(text) is not None, f"{text} 能被识别")
    for text in ("谁在艾特我", "艾特回顾一下", "谁艾特我 abc", "谁艾特我 1000", "艾特了我"):
        check(QUERY_PATTERN.match(text) is None, f"{text} 不该被识别")


def test_count_comes_from_the_command() -> None:
    print("条数直接写在命令里，「次 / 条」和空格都随便写")
    check(_count(QUERY_PATTERN.match("谁艾特我")) is None, "谁艾特我 不带条数")
    check(_count(QUERY_PATTERN.match("谁艾特我 3")) == "3", "谁艾特我 3 -> 3")
    check(_count(QUERY_PATTERN.match("谁艾特我3次")) == "3", "谁艾特我3次 -> 3")
    check(_count(QUERY_PATTERN.match("谁艾特我 12 条")) == "12", "谁艾特我 12 条 -> 12")
    check(_count(QUERY_PATTERN.match("谁艾特我 100")) == "100", "谁艾特我 100 -> 100")
    check(_count(QUERY_PATTERN.match("艾特回顾 3")) == "3", "艾特回顾 3 -> 3")
    check(_count(QUERY_PATTERN.match("CATCH_UP 2")) == "2", "CATCH_UP 2 -> 2")
    check(_count(QUERY_PATTERN.match("谁艾特他 3 @张三(12345)")) == "3", "查别人也能带条数")
    check(_count(QUERY_PATTERN.match("谁艾特他 @张三(12345)")) is None, "查别人可以不带条数")
    check(QUERY_PATTERN.match("谁艾特我[CQ:at,qq=123]") is not None, "CQ 码形态仍然匹配")


def test_recap_command_is_gone() -> None:
    print("独立的「艾特回顾」命令和文字摘要都已合并移除")
    check(not hasattr(constants, "RECAP_PATTERN"), "constants 里没有 RECAP_PATTERN")
    check(not hasattr(constants, "RECAP_DEFAULT_COUNT"), "constants 里没有 RECAP_DEFAULT_COUNT")
    check(not hasattr(constants, "RECAP_MAX_COUNT"), "constants 里没有 RECAP_MAX_COUNT")
    check(not hasattr(main.MentionEchoPlugin, "_recap"), "插件上没有 _recap 方法")
    check(not hasattr(main.MentionEchoPlugin, "_query_recap_summary"), "查询成功不再拼接文字摘要")
    check(not hasattr(main.MentionEchoPlugin, "_query_waiting_text"), "查询成功前不再发送等待文字")
    check(QUERY_RECENT_DEFAULT == 0, "默认看全部记录")
    check(QUERY_RECENT_MAX == 50, "单次最多回顾 50 次艾特")
    check(_plugin()._is_plugin_command("艾特回顾"), "艾特回顾 仍被视作插件命令（不会被别的插件抢走）")
    check(_plugin()._is_plugin_command("谁艾特我 3"), "谁艾特我 3 仍被视作插件命令")


def test_alias_defaults_to_self() -> None:
    print("别名没点名就查自己，带 @ 就查对方")
    plugin = _plugin()
    event = _Event("111111")
    check(plugin._query_target(event, "谁艾特我", []) == "111111", "谁艾特我 -> 自己")
    check(plugin._query_target(event, "艾特回顾", []) == "111111", "艾特回顾 -> 自己")
    check(plugin._query_target(event, "catch_up 3", []) == "111111", "catch_up 3 -> 自己")
    check(plugin._query_target(event, "艾特回顾 @张三(999888)", []) == "999888", "艾特回顾 @某人 -> 对方")
    check(plugin._query_target(event, "艾特回顾", ["999888"]) == "999888", "别名 + at 段 -> 对方")
    check(plugin._query_target(event, "谁艾特他", []) == "", "谁艾特他 没点名 -> 空（会提示补 @）")


def test_recent_limit_prefers_the_command() -> None:
    print("条数取值顺序：命令里的数字 > 配置项 > 0（全部）")
    check(_plugin()._query_recent_limit(QUERY_PATTERN.match("谁艾特我")) == 0, "无数字无配置 -> 0")
    check(_plugin()._query_recent_limit(None) == 0, "match 为 None 也安全")
    check(_plugin()._query_recent_limit(QUERY_PATTERN.match("谁艾特我 3")) == 3, "命令里的 3 生效")
    check(
        _plugin(query_recent_count=5)._query_recent_limit(QUERY_PATTERN.match("谁艾特我 3")) == 3,
        "命令覆盖配置",
    )
    check(
        _plugin(query_recent_count=5)._query_recent_limit(QUERY_PATTERN.match("艾特回顾")) == 5,
        "不写数字时回落到配置",
    )
    check(
        _plugin()._query_recent_limit(QUERY_PATTERN.match("谁艾特我 999")) == QUERY_RECENT_MAX,
        f"命令里的大数字被夹到 {QUERY_RECENT_MAX}",
    )
    check(_plugin(query_recent_count=999)._query_recent_limit(None) == QUERY_RECENT_MAX, "配置的大数字同样被夹")
    check(_plugin(query_recent_count=-5)._query_recent_limit(None) == 0, "配置负数 -> 0")
    check(_plugin(query_recent_count="abc")._query_recent_limit(None) == 0, "配置写错字 -> 0")


def test_context_mode_tri_state() -> None:
    print("query_context_mode 三态解析；始终展示会自动采集之后的查询上下文")
    check(_plugin()._query_context_mode() == "auto", "缺省 -> auto")
    check(_plugin()._query_context_capture_enabled() is False, "缺省不额外采集查询上下文")
    for value in ("跟随群设置", "auto", "AUTO", "随便写点啥"):
        check(_plugin(query_context_mode=value)._query_context_mode() == "auto", f"{value} -> auto")
    for value in ("始终展示", "always", "总是展示", "强制展示", "on", "true"):
        plugin = _plugin(query_context_mode=value)
        check(plugin._query_context_mode() == "always", f"{value} -> always")
        check(plugin._query_context_capture_enabled() is True, f"{value} -> 自动采集")
    for value in ("从不展示", "never", "不展示", "关闭", "off", "false"):
        plugin = _plugin(query_context_mode=value)
        check(plugin._query_context_mode() == "never", f"{value} -> never")
        check(plugin._query_context_capture_enabled() is False, f"{value} -> 不采集")


def test_always_mode_captures_future_query_context() -> None:
    print("始终展示会让后续消息实际进入查询上下文缓存，普通前文不额外落图")
    plugin = _plugin(query_context_mode="始终展示")
    plugin.before_cache = {}
    plugin.after_tasks = {}
    plugin.reminder_after_tasks = {}
    calls = {"context_message": 0, "after": 0, "before": 0, "cache": 0, "externalize": []}

    async def context_enabled(group_id):
        return False

    async def reminder_context(group_id):
        return {"enabled": False, "before": 0, "after": 0}

    async def member_info(event, group_id, user_id):
        return {}

    async def quote(event):
        return None

    async def context_message(event, group_id, mentions, member_info, quote):
        calls["context_message"] += 1
        return {"message": "新消息", "images": [], "media": []}

    async def cache_record_images(record):
        calls["cache"] += 1
        return record

    async def externalize_record_images(record, *, allow_spill):
        calls["externalize"].append(allow_spill)

    async def append_after(group_id, record):
        calls["after"] += 1

    async def append_before(group_id, record):
        calls["before"] += 1

    plugin._context_enabled = context_enabled
    plugin._reminder_context_config = reminder_context
    plugin._member_info = member_info
    plugin._quote = quote
    plugin._context_message = context_message
    plugin._cache_record_images = cache_record_images
    plugin._externalize_record_images = externalize_record_images
    plugin._append_after_context = append_after
    plugin._append_before_context_cache = append_before

    state = asyncio.run(plugin._record_context_message_locked(_Event(), "10001", [], append_to_cache=True))

    check(state["context_on"] is False, "群级查询上下文仍可保持关闭")
    check(state["query_context_on"] is True, "始终展示启用自动查询上下文采集")
    check(state["record_context_on"] is True, "记录路径因此开启上下文")
    check(calls["context_message"] == 1 and calls["after"] == 1 and calls["before"] == 1, "新消息被解析、补后文任务并进入前文缓存")
    check(calls["cache"] == 0 and calls["externalize"] == [False], "普通前文不缓存原图，只做轻量化")

    asyncio.run(plugin._record_context_message_locked(_Event(), "10001", ["333333"], append_to_cache=False))

    check(calls["cache"] == 1, "真正的艾特消息仍保留图片缓存以便后续回放")


def test_never_mode_does_not_capture_query_context() -> None:
    print("从不展示会停止查询上下文采集，即使群开关此前仍是开启状态")
    plugin = _plugin(query_context_mode="从不展示")
    plugin.before_cache = {}
    plugin.after_tasks = {}
    plugin.reminder_after_tasks = {}
    calls = {"context_message": 0, "after": 0, "before": 0}

    async def context_enabled(group_id):
        return True

    async def reminder_context(group_id):
        return {"enabled": False, "before": 0, "after": 0}

    async def context_message(*args, **kwargs):
        calls["context_message"] += 1
        return {"message": "不应被采集", "images": []}

    async def append_after(group_id, record):
        calls["after"] += 1

    async def append_before(group_id, record):
        calls["before"] += 1

    plugin._context_enabled = context_enabled
    plugin._reminder_context_config = reminder_context
    plugin._context_message = context_message
    plugin._append_after_context = append_after
    plugin._append_before_context_cache = append_before

    state = asyncio.run(plugin._record_context_message_locked(_Event(), "10001", [], append_to_cache=True))

    check(state["context_on"] is True, "群上下文开关本身仍保留")
    check(state["query_context_on"] is False, "从不展示没有全局自动采集")
    check(state["record_context_on"] is False, "从不展示覆盖查询上下文采集")
    check(state["current"] is None, "不解析普通群消息")
    check(calls == {"context_message": 0, "after": 0, "before": 0}, "不写前后文缓存或后文任务")


def test_build_blocks_respects_context_mode() -> None:
    print("出图时间线：auto 跟随群设置、always 无视开关、never 一律只留被艾特那条")
    plugin = _plugin()
    off = _record(is_context=False)
    on = _record(is_context=True)

    check(_message_count(plugin._build_blocks([off], "我", "111111", context_mode="auto")) == 1, "auto + 群没开 -> 只有 1 条")
    check(_message_count(plugin._build_blocks([on], "我", "111111", context_mode="auto")) == 3, "auto + 群开了 -> 前后各 1 条")
    check(_message_count(plugin._build_blocks([off], "我", "111111", context_mode="always")) == 3, "always + 群没开 -> 照样展示")
    check(_message_count(plugin._build_blocks([on], "我", "111111", context_mode="never")) == 1, "never + 群开了 -> 也只留 1 条")
    check(
        _message_count(plugin._build_blocks([_record(is_context=True, before=[], after=[])], "我", "111111", context_mode="always")) == 1,
        "always 但压根没存上下文 -> 还是 1 条",
    )
    check(_message_count(plugin._build_blocks([on], "我", "111111")) == 3, "不传 context_mode 时默认 auto")


def test_trim_record_context_respects_context_mode() -> None:
    print("放不下要裁剪时，也得按同一套 context_mode 规则裁")
    plugin = _plugin()
    on = _record(is_context=True)
    off = _record(is_context=False)

    trimmed = plugin._trim_record_context(on, 1, context_mode="auto")
    check(trimmed["before"] == [] and trimmed["after"] == [], "只剩 1 条额度 -> 上下文全丢")
    trimmed = plugin._trim_record_context(on, 10, context_mode="never")
    check(trimmed["before"] == [] and trimmed["after"] == [], "never -> 上下文全丢")
    trimmed = plugin._trim_record_context(off, 10, context_mode="auto")
    check(trimmed["before"] == [] and trimmed["after"] == [], "auto + 群没开 -> 上下文全丢")
    trimmed = plugin._trim_record_context(off, 10, context_mode="always")
    check(len(trimmed["before"]) == 1 and len(trimmed["after"]) == 1, "always + 群没开 -> 上下文保留")
    trimmed = plugin._trim_record_context(on, 3, context_mode="auto")
    check(len(trimmed["before"]) == 1 and len(trimmed["after"]) == 1, "auto + 群开了 -> 上下文保留")
    check(on["before"] and on["after"], "裁剪不能改到原始记录")


def test_records_show_context_flag() -> None:
    print("表头「含上下文」提示与实际出图保持一致")
    plugin = _plugin()
    on = _record(is_context=True)
    off = _record(is_context=False)
    bare = _record(is_context=True, before=[], after=[])

    check(plugin._records_show_context([on], "auto") is True, "auto + 群开了 -> True")
    check(plugin._records_show_context([off], "auto") is False, "auto + 群没开 -> False")
    check(plugin._records_show_context([off], "always") is True, "always + 存了上下文 -> True")
    check(plugin._records_show_context([bare], "always") is False, "always 但没存上下文 -> False")
    check(plugin._records_show_context([on], "never") is False, "never -> 恒 False")
    check(plugin._records_show_context([], "always") is False, "没有记录 -> False")


def test_context_record_reuses_the_parsed_message() -> None:
    print("有上下文时，同一条艾特只解析一次，并复用到记录和前文缓存")
    plugin = _plugin()
    plugin.before_cache = {"10001": [{"message": "上一条"}]}
    plugin.after_tasks = {}
    plugin.reminder_after_tasks = {}
    plugin._message_count_epoch = "test-epoch"
    calls = {"mention_record": 0, "append": 0}
    appended: list[dict[str, Any]] = []
    current = {
        "user_id": "222222",
        "name": "小张",
        "role": "member",
        "message": "上下文原消息",
        "images": [],
        "media": [],
        "time": 1700000000,
        "at_targets": ["old-target"],
    }

    async def context_state(event, group_id, mentions, *, append_to_cache):
        return {
            "context_on": False,
            "query_context_on": True,
            "record_context_on": True,
            "reminder_context": {"before": 0, "after": 0},
            "reminder_context_on": False,
            "sender_info": {},
            "quote": None,
            "current": current,
        }

    async def mention_record(*args, **kwargs):
        calls["mention_record"] += 1
        return {"message": "不应走到这里"}

    async def append_record(group_id, target, record):
        calls["append"] += 1
        appended.append(record)

    async def queue_reminder(*args, **kwargs):
        return False

    async def append_before(group_id, record):
        return None

    plugin._record_context_message_locked = context_state
    plugin._mention_record = mention_record
    plugin._append_record = append_record
    plugin._queue_reminder_if_needed = queue_reminder
    plugin._append_before_context_cache = append_before
    plugin._self_id = lambda event: "999999"
    plugin._sender_id = lambda event: "222222"
    plugin._message_text_for_record = lambda event, mentions: "这是艾特目标后的正文"
    plugin._mention_after_image = lambda event, mentions: False
    plugin._message_after_images = lambda event, mentions: ""
    plugin._event_message_sequence = lambda event: 12
    plugin._event_group_message_count = lambda event, group_id: 34

    asyncio.run(plugin._record_mentions_locked(object(), "10001", ["333333"]))

    check(calls["mention_record"] == 0, "不会第二次调用 _mention_record")
    check(calls["append"] == 1, "目标只追加一条记录")
    check(current["message"] == "上下文原消息", "前文缓存里的当前消息没有被改写")
    check(current["at_targets"] == ["old-target"], "前文缓存的 at 目标没有被改写")
    check(appended[0]["message"] == "这是艾特目标后的正文", "目标记录保留正确的艾特正文")
    check(appended[0]["before"] == [{"message": "上一条"}], "目标记录带上已有前文")


def test_successful_query_sends_only_images() -> None:
    print("查询渲染成功时只发送结果图片，不额外刷等待或摘要文字")
    plugin = _plugin(query_recent_count=2)
    event = _Event("111111")
    event.plain_results = []
    event.plain_result = lambda text: event.plain_results.append(text) or {"plain": text}
    record = _record(is_context=True)
    sent_images: list[list[str]] = []
    cleaned: list[set[str]] = []

    async def get_records(group_id, target):
        return [record] if target == "111111" else []

    async def target_name(event, group_id, target):
        return "我"

    async def resolve_pokes(event, group_id, records):
        return records

    async def prepare_records(records):
        return records, {"temporary-image"}

    async def group_name(event, group_id):
        return "测试群"

    async def member_count(event, group_id):
        return 3

    async def render_images(items):
        return ["query-result.jpg"]

    async def send_images(event, image_paths):
        sent_images.append(image_paths)
        return True

    async def forward_text(*args, **kwargs):
        raise AssertionError("出图成功时不应回退为文字转发")

    plugin._get_records = get_records
    plugin._dedupe_records = lambda records: records
    plugin._target_name = target_name
    plugin._query_recent_limit = lambda match: 2
    plugin._query_context_mode = lambda: "always"
    plugin._query_reverse_order = lambda: False
    plugin._render_text_only = lambda: False
    plugin._select_query_records = lambda records, *args, **kwargs: records
    plugin._resolve_record_pokes = resolve_pokes
    plugin._prepare_records_for_render = prepare_records
    plugin._build_blocks = lambda *args, **kwargs: [{"msgs": []}]
    plugin._chunk_blocks = lambda blocks: [blocks]
    plugin._limit_chunks = lambda chunks, limit: chunks
    plugin._max_query_pages = lambda: 0
    plugin._log_query_image_diagnostics = lambda *args, **kwargs: None
    plugin._group_name = group_name
    plugin._member_count = member_count
    plugin._records_show_context = lambda records, mode: True
    plugin._header_image_url = lambda: ""
    plugin._footer_image_url = lambda: ""
    plugin._render_query_images = render_images
    plugin._try_send_images = send_images
    plugin._try_send_records_forward_text = forward_text
    plugin._delete_cached_image_paths = lambda paths: cleaned.append(paths)

    result = asyncio.run(plugin._query(event, "10001", "谁艾特我", [], None))

    check(result == [], "成功路径没有再交给 AstrBot 发送文字结果")
    check(sent_images == [["query-result.jpg"]], "只发送一张结果图")
    check(event.plain_results == [], "没有等待提示或文字摘要")
    check(cleaned == [{"temporary-image"}], "查询结束后仍会回收临时图片")


def test_self_alias_pattern_is_narrow() -> None:
    print("别名判定只认开头，避免把普通聊天里的「回顾」当成命令")
    check(QUERY_SELF_ALIAS_PATTERN.search("艾特回顾") is not None, "艾特回顾 命中")
    check(QUERY_SELF_ALIAS_PATTERN.search("catchup 2") is not None, "catchup 2 命中")
    check(QUERY_SELF_ALIAS_PATTERN.search("谁艾特我") is None, "谁艾特我 不该走别名分支")
    check(QUERY_SELF_ALIAS_PATTERN.search("我们回顾一下艾特") is None, "句中出现不算命令")


def main_() -> int:
    for test in (
        test_one_pattern_understands_both_wordings,
        test_count_comes_from_the_command,
        test_recap_command_is_gone,
        test_alias_defaults_to_self,
        test_recent_limit_prefers_the_command,
        test_context_mode_tri_state,
        test_always_mode_captures_future_query_context,
        test_never_mode_does_not_capture_query_context,
        test_build_blocks_respects_context_mode,
        test_trim_record_context_respects_context_mode,
        test_records_show_context_flag,
        test_context_record_reuses_the_parsed_message,
        test_successful_query_sends_only_images,
        test_self_alias_pattern_is_narrow,
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
    raise SystemExit(main_())
