"""艾特回声：图片「引用式存储」的自测（不依赖 pytest，直接 python tests/test_storage.py）。

这些用例锁死插件最重要的一条不变量：**数据库里永远只出现轻量引用，图片字节只躺文件系统**。
上游 astrbot_plugin_who_at_me_pro 的两个空间泄漏 bug 就发生在这里，所以必须有可复现的回归测试：

  bug 1  同一张图存两遍：``images[0]`` 与 ``image_cache[0].source`` 都是同一段 base64 原文；
  bug 2  清理只清一半：``_drop_record_image_cache`` 只 pop ``image_cache``，``images`` 永不释放。
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _ensure_astrbot() -> None:
    """让本文件在没装 AstrBot 的环境（例如 CI）里也能跑。"""
    try:
        import astrbot.api  # noqa: F401
    except Exception:
        pass
    else:
        return

    class _NullLogger:
        def _noop(self, *args: object, **kwargs: object) -> None:
            return None

        debug = info = warning = error = exception = critical = _noop

    package = types.ModuleType("astrbot")
    package.__path__ = []  # type: ignore[attr-defined]
    api = types.ModuleType("astrbot.api")
    api.logger = _NullLogger()  # type: ignore[attr-defined]
    package.api = api  # type: ignore[attr-defined]
    sys.modules["astrbot"] = package
    sys.modules["astrbot.api"] = api


_ensure_astrbot()

from modules.constants import IMAGE_EXPIRED_REF, IMAGE_REF_PREFIX  # noqa: E402
from modules.data import DataMixin  # noqa: E402

_FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _FAILURES.append(label)


def _png_bytes(size: int) -> bytes:
    """造一张能过 _cached_image_suffix 魔数校验的假 PNG。"""
    head = b"\x89PNG\r\n\x1a\n"
    return head + bytes((i * 7 + 13) % 251 for i in range(max(0, size - len(head))))


def _inline_png(size: int) -> str:
    return "base64://" + base64.b64encode(_png_bytes(size)).decode("ascii")


class _Harness(DataMixin):
    """只补齐 DataMixin 需要的配置读取与数据目录，其余逻辑全部走真实实现。"""

    def __init__(self, root: Path, keep_records: int = 20) -> None:
        self._root = root
        self._keep_records = keep_records

    def _plugin_data_dir(self) -> str:
        return str(self._root)

    def _recent_image_cache_records(self) -> int:
        return self._keep_records

    def _max_records_per_target(self) -> int:
        return 300


def _harness(keep_records: int = 20):
    root = Path(tempfile.mkdtemp(prefix="me_storage_"))
    return _Harness(root, keep_records), root


def _walk_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def _has_inline_image(value: object) -> bool:
    return any(
        text.startswith("base64://") or text.lower().startswith("data:image/")
        for text in _walk_strings(value)
    )


def _cache_files(root: Path) -> list[Path]:
    cache_dir = root / "message_images"
    return sorted(p for p in cache_dir.rglob("*") if p.is_file()) if cache_dir.exists() else []


# ---------------------------------------------------------------- 落盘 + 引用化


def test_externalize_spills_bytes_to_disk() -> None:
    print("\n入库前把图片原文换成引用（allow_spill=True）")
    harness, root = _harness()
    try:
        inline = _inline_png(40_000)
        record = {"sender": "1", "message": "看图", "images": [inline]}
        cached = asyncio.run(harness._cache_record_images(record))

        check(not _has_inline_image(cached), "记录里已经没有任何 base64 原文")
        refs = cached.get("images") or []
        check(len(refs) == 1 and str(refs[0]).startswith(IMAGE_REF_PREFIX), "images[0] 变成 mecache:// 引用")
        files = _cache_files(root)
        check(len(files) == 1, f"图片字节落盘为 1 个文件（实际 {len(files)}）")
        check(files and files[0].read_bytes() == _png_bytes(40_000), "落盘内容与原图逐字节一致")

        payload = len(json.dumps(cached, ensure_ascii=False))
        check(payload < 1024, f"整条记录的 JSON 体积 < 1 KB（实际 {payload} B）")

        restored = harness._image_ref_to_path(refs[0])
        check(restored is not None and restored.exists(), "引用可以还原成真实存在的绝对路径")
        data, _ = harness._read_image_source(refs[0])
        check(data == _png_bytes(40_000), "渲染期能通过引用重新读回图片")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_no_double_store() -> None:
    print("\nbug 1 回归：同一张图不许存两遍")
    harness, root = _harness()
    try:
        inline = _inline_png(30_000)
        record = {"sender": "1", "images": [inline]}
        cached = asyncio.run(harness._cache_record_images(record))

        cache = cached.get("image_cache") or []
        sources = [str(item.get("source") or "") for item in cache if isinstance(item, dict)]
        check(not any(s.startswith("base64://") for s in sources), "image_cache[].source 不再是 base64 原文")
        check(
            all(s.startswith(IMAGE_REF_PREFIX) or not s for s in sources),
            "image_cache[].source 只保留引用",
        )
        files = _cache_files(root)
        check(len(files) == 1, f"同一张图在磁盘上只有 1 份（实际 {len(files)}）")

        blob = json.dumps(cached, ensure_ascii=False)
        check(len(blob) < 2048, f"库内体积不随图片大小增长（实际 {len(blob)} B）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_drop_releases_images_and_files() -> None:
    print("\nbug 2 回归：清理必须同时释放 images 与磁盘文件")
    harness, root = _harness()
    try:
        record = {"sender": "1", "images": [_inline_png(20_000)]}
        cached = asyncio.run(harness._cache_record_images(record))
        check(len(_cache_files(root)) == 1, "前置条件：图片已落盘")

        harness._drop_record_image_cache(cached, delete_files=True)
        check("image_cache" not in cached, "image_cache 被移除")
        check(
            cached.get("images") == [IMAGE_EXPIRED_REF],
            f"images 被换成过期占位而不是留着（实际 {cached.get('images')}）",
        )
        check(len(_cache_files(root)) == 0, "磁盘上的图片文件被真正删除")
        check(not harness._record_has_image_content(cached), "全过期的记录不再占用「保留原图」名额")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_drop_keeps_http_links() -> None:
    print("\n清理时 http 链接留着（不占空间，将来还能重取）")
    harness, root = _harness()
    try:
        record = {"images": ["https://127.0.0.1/x.jpg", _inline_png(20_000)]}
        cached = asyncio.run(harness._cache_record_images(record))
        harness._drop_record_image_cache(cached, delete_files=True)
        images = cached.get("images") or []
        check("https://127.0.0.1/x.jpg" in images, "http 链接原样保留")
        check(IMAGE_EXPIRED_REF in images, "落盘那张变成过期占位")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_no_spill_when_window_closed() -> None:
    print("\n不在留盘窗口里的记录：一个字节都不落盘")
    harness, root = _harness(keep_records=0)
    try:
        record = {"images": [_inline_png(50_000)], "message": "hi"}
        cached = asyncio.run(harness._cache_record_images(record))
        check(cached.get("images") == [IMAGE_EXPIRED_REF], "images 直接变过期占位")
        check("image_cache" not in cached, "不生成 image_cache")
        check(len(_cache_files(root)) == 0, "磁盘上不产生任何文件")
        check(not _has_inline_image(cached), "数据库里没有 base64")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_nested_context_is_externalized() -> None:
    print("\n上下文 before/after、引用回复、合并转发里的图一并引用化")
    harness, root = _harness()
    try:
        record = {
            "images": [_inline_png(12_000)],
            "quote": {"images": [_inline_png(12_500)]},
            "media": [{"cover": _inline_png(13_000)}],
            "before": [{"images": [_inline_png(13_500)]}],
            "after": [{"images": [_inline_png(14_000)]}],
        }
        cached = asyncio.run(harness._cache_record_images(record))
        check(not _has_inline_image(cached), "五处嵌套位置全部没有 base64 残留")
        check(len(_cache_files(root)) == 5, f"五张图各落盘一次（实际 {len(_cache_files(root))}）")

        harness._drop_record_image_cache(cached, delete_files=True)
        check(not harness._has_live_image_refs(cached.get("images")), "顶层 images 已释放")
        check(cached["quote"]["images"] == [IMAGE_EXPIRED_REF], "quote.images 已释放")
        check(cached["before"][0]["images"] == [IMAGE_EXPIRED_REF], "before[].images 已释放")
        check(cached["after"][0]["images"] == [IMAGE_EXPIRED_REF], "after[].images 已释放")
        check(len(_cache_files(root)) == 0, "嵌套位置的磁盘文件也被删干净")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 写库闸门


def test_gate_blocks_base64() -> None:
    print("\n写库闸门：任何绕过缓存流程的 base64 都会被拦下")
    harness, root = _harness()
    try:
        inline = _inline_png(60_000)
        records = [{"sender": "1", "images": [inline]}]
        gated = harness._gate_kv_value("records:qq:1:2", records)
        check(not _has_inline_image(gated), "records: 键上的 base64 被剥离")
        check(gated[0]["images"] == [IMAGE_EXPIRED_REF], "被换成过期占位")
        check(records[0]["images"] == [inline], "原对象不被就地破坏（闸门只改副本）")

        gated_pending = harness._gate_kv_value("reminder:pending:qq:1:2", [{"images": [inline]}])
        check(not _has_inline_image(gated_pending), "reminder:pending: 键同样受保护")

        untouched = harness._gate_kv_value("member:qq:1:2", [{"note": inline}])
        check(untouched[0]["note"] == inline, "非记录类键不受影响")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_gate_catches_bare_base64() -> None:
    print("\n写库闸门：连没有 base64:// 前缀的裸原文也抓得住")
    harness, root = _harness()
    try:
        bare = base64.b64encode(_png_bytes(40_000)).decode("ascii")
        records = [{"images": [bare], "message": "x"}]
        gated, freed = harness._slim_records_for_storage(records)
        check(gated[0]["images"] == [IMAGE_EXPIRED_REF], "images 里的裸 base64 被判过期")
        check(freed == len(bare), f"回收字节数等于原文长度（{freed} == {len(bare)}）")

        keep = [{"message": "a" * 500, "images": ["https://example.com/a.jpg"]}]
        same, nothing = harness._slim_records_for_storage(keep)
        check(nothing == 0 and same is keep, "干净数据零拷贝直通，正常路径没有额外开销")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_long_text_is_truncated() -> None:
    print("\n写库闸门：超长文本按上限截断（防单条记录爆库）")
    harness, root = _harness()
    try:
        long_text = "字" * 5000
        gated, freed = harness._slim_records_for_storage([{"message": long_text}])
        check(len(gated[0]["message"]) == 2001, f"截到 2000 字 + 省略号（实际 {len(gated[0]['message'])}）")
        check(freed == 3000, f"回收 3000 字（实际 {freed}）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 引用安全


def test_ref_rejects_escape() -> None:
    print("\n引用解析：拒绝越界路径（防 ../ 注入删到别处）")
    harness, root = _harness()
    try:
        check(harness._image_ref_to_path(IMAGE_REF_PREFIX + "../../etc/passwd") is None, "拒绝 ../ 逃逸")
        check(harness._image_ref_to_path(IMAGE_REF_PREFIX + "/C:/Windows/win.ini") is None, "拒绝绝对路径注入")
        check(harness._image_ref_to_path(IMAGE_REF_PREFIX) is None, "拒绝空引用")
        check(harness._image_ref_to_path(IMAGE_EXPIRED_REF) is None, "过期占位解析不出路径")
        check(harness._image_ref_to_path("https://example.com/a.jpg") is None, "非引用返回 None")
        good = harness._image_ref_to_path(IMAGE_REF_PREFIX + "20260906/msg_1.png")
        check(good is not None and good.name == "msg_1.png", "正常引用解析成功")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_referenced_paths_protect_files() -> None:
    print("\n孤儿回收保护：引用指向的文件必须出现在「已引用」集合里")
    harness, root = _harness()
    try:
        record = {"images": [_inline_png(15_000)], "quote": {"images": [_inline_png(15_500)]}}
        cached = asyncio.run(harness._cache_record_images(record))
        referenced = harness._record_image_cache_paths(cached)
        on_disk = {str(p.resolve()) for p in _cache_files(root)}
        resolved = {str(Path(p).resolve()) for p in referenced}
        check(on_disk and on_disk <= resolved, "磁盘上每个文件都被记录引用着，不会被当孤儿删掉")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_expired_placeholders_not_deduped() -> None:
    print("\n过期占位不去重：前端要按张数提示「N 张图片已释放」")
    harness, root = _harness()
    try:
        merged = harness._dedupe_refs([IMAGE_EXPIRED_REF, IMAGE_EXPIRED_REF, "a", "a", ""])
        check(merged == [IMAGE_EXPIRED_REF, IMAGE_EXPIRED_REF, "a"], f"实际 {merged}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_keep_window_bounds_disk_usage() -> None:
    print("\n留盘窗口：只有最近 N 条留着原图，更早的自动释放")
    harness, root = _harness(keep_records=5)
    try:
        records = [
            asyncio.run(harness._cache_record_images({"sender": str(i), "images": [_inline_png(8_000 + i)]}))
            for i in range(20)
        ]
        check(len(_cache_files(root)) == 20, f"落盘阶段 20 张都在（实际 {len(_cache_files(root))}）")

        pruned = harness._prune_record_image_caches(records)
        harness._drop_records_image_cache(pruned, delete_files=True)

        live = [r for r in records if harness._record_has_image_content(r)]
        check(len(live) == 5, f"只有最近 5 条还持有原图（实际 {len(live)}）")
        check(len(_cache_files(root)) == 5, f"磁盘上只剩 5 个文件（实际 {len(_cache_files(root))}）")
        check(
            all(harness._record_has_image_content(r) for r in records[-5:]),
            "保留的正好是最近 5 条，不是任意 5 条",
        )
        check(
            records[0]["images"] == [IMAGE_EXPIRED_REF],
            "最早那条只留下过期占位（前端会提示「图片已释放」）",
        )
        blob = len(json.dumps(records, ensure_ascii=False))
        check(blob < 4096, f"20 条记录的库内总体积仍 < 4 KB（实际 {blob} B）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    for test in (
        test_externalize_spills_bytes_to_disk,
        test_no_double_store,
        test_drop_releases_images_and_files,
        test_drop_keeps_http_links,
        test_no_spill_when_window_closed,
        test_nested_context_is_externalized,
        test_gate_blocks_base64,
        test_gate_catches_bare_base64,
        test_long_text_is_truncated,
        test_ref_rejects_escape,
        test_referenced_paths_protect_files,
        test_expired_placeholders_not_deduped,
        test_keep_window_bounds_disk_usage,
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