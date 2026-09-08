"""艾特回声：清理逻辑的自测（不依赖 pytest，直接 python tests/test_maintenance.py）。

只测 modules/maintenance.py 里的纯同步扫描函数——它们负责真正删用户的文件，
是这个插件里唯一"不可逆"的部分，所以必须有可复现的验证。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _ensure_astrbot() -> None:
    """让本文件在没装 AstrBot 的环境（例如 CI）里也能跑。

    被测函数全是纯同步的文件扫描逻辑，只用标准库；``modules.maintenance`` 顶部的
    ``from astrbot.api import logger`` 是唯一的外部依赖，缺失时用哑 logger 顶上。
    """
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

from modules.maintenance import (  # noqa: E402
    _date_dir_expired,
    _dir_entries,
    _enforce_quota_sync,
    _human_bytes,
    _remove_empty_dirs,
    _sweep_images_sync,
    _sweep_renders_sync,
)

NOW = time.time()
HOUR = 3600
DAY = 86400
_FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _FAILURES.append(label)


def write(path: Path, size: int, age_seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = NOW - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_dir_entries() -> None:
    print("_dir_entries")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        write(root / "a.jpg", 10, 0)
        write(root / "sub" / "deep" / "b.jpg", 20, 0)
        entries = _dir_entries(root)
        check(len(entries) == 2, "递归收集子目录里的文件")
        check(sum(item[2] for item in entries) == 30, "大小求和正确")
        check(_dir_entries(root / "missing") == [], "目录不存在时返回空列表而不是抛异常")


def test_date_dir_expired() -> None:
    print("_date_dir_expired")
    root = Path("/data/message_images")
    check(_date_dir_expired(root / "20250101" / "a.jpg", root, "20260101"), "旧日期目录判为过期")
    check(not _date_dir_expired(root / "20260301" / "a.jpg", root, "20260101"), "新日期目录不算过期")
    check(not _date_dir_expired(root / "a.jpg", root, "20260101"), "没有日期目录层时不判过期")
    check(not _date_dir_expired(root / "notadate" / "a.jpg", root, "20260101"), "非日期目录名不参与比较")
    check(not _date_dir_expired(root / "20250101" / "a.jpg", root, ""), "未设保留期时一律不过期")
    check(not _date_dir_expired(Path("/elsewhere/a.jpg"), root, "20260101"), "不在 root 下时安全返回 False")


def test_sweep_renders() -> None:
    print("_sweep_renders_sync")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        old = write(root / "mention_echo_old.jpg", 100, 48 * HOUR)
        legacy = write(root / "who_at_me_old.jpg", 100, 48 * HOUR)
        fresh = write(root / "mention_echo_fresh.jpg", 100, 1 * HOUR)
        grace = write(root / "mention_echo_sending.jpg", 100, 60)
        foreign = write(root / "someone_else.jpg", 100, 48 * HOUR)
        notimage = write(root / "mention_echo_note.txt", 100, 48 * HOUR)

        stats = _sweep_renders_sync(root, NOW - 24 * HOUR, 0, NOW - 600)
        check(not old.exists(), "超过保留时长的渲染图被删除")
        check(not legacy.exists(), "上游前缀的历史渲染图也会被清理")
        check(fresh.exists(), "保留期内的渲染图保留")
        check(grace.exists(), "宽限期内（可能正在发送）的图绝不删除")
        check(foreign.exists(), "不是本插件产出的文件绝不碰")
        check(notimage.exists(), "非图片后缀不碰")
        check(stats["removed_files"] == 2, f"删除计数=2（实际 {stats['removed_files']}）")
        check(stats["freed_bytes"] == 200, f"释放字节=200（实际 {stats['freed_bytes']}）")
        check(stats["size_bytes"] == 200, f"剩余体积只统计托管文件=200（实际 {stats['size_bytes']}）")


def test_sweep_renders_quota() -> None:
    print("_sweep_renders_sync 配额")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        oldest = write(root / "mention_echo_1.jpg", 400, 5 * HOUR)
        middle = write(root / "mention_echo_2.jpg", 400, 4 * HOUR)
        newest = write(root / "mention_echo_3.jpg", 400, 3 * HOUR)

        stats = _sweep_renders_sync(root, 0, 900, NOW - 600)
        check(not oldest.exists(), "超配额时先删最旧的")
        check(middle.exists() and newest.exists(), "删到配额以内就停手")
        check(stats["size_bytes"] == 800, f"剩余体积=800（实际 {stats['size_bytes']}）")


def test_sweep_renders_quota_respects_grace() -> None:
    print("_sweep_renders_sync 配额 + 宽限期")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        sending = [write(root / f"mention_echo_g{i}.jpg", 400, 30) for i in range(3)]
        stats = _sweep_renders_sync(root, 0, 500, NOW - 600)
        check(all(path.exists() for path in sending), "全是宽限期内的文件时宁可超配额也不删")
        check(stats["removed_files"] == 0, "宽限期文件不计入删除")


def test_sweep_images() -> None:
    print("_sweep_images_sync")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        current_dir = time.strftime("%Y%m%d", time.localtime(NOW))
        expired_dir_name = time.strftime("%Y%m%d", time.localtime(NOW - 3 * DAY))
        expired_mtime = write(root / current_dir / "msg_expired.jpg", 100, 48 * HOUR)
        expired_dir = write(root / expired_dir_name / "msg_olddir.jpg", 100, 1 * HOUR)
        orphan = write(root / current_dir / "msg_orphan.jpg", 100, 2 * HOUR)
        referenced = write(root / current_dir / "msg_kept.jpg", 100, 2 * HOUR)
        just_saved = write(root / current_dir / "msg_inflight.jpg", 100, 30)

        stats = _sweep_images_sync(
            root,
            {os.path.normcase(os.path.abspath(referenced))},
            NOW - 24 * HOUR,
            NOW - 30 * 60,
            0,
            NOW - 30 * 60,
        )
        check(not expired_mtime.exists(), "按 mtime 过期的图片被删除")
        check(not expired_dir.exists(), "按日期目录过期的图片被删除（即使 mtime 是新的）")
        check(not orphan.exists(), "没有记录引用、且过了宽限期的孤儿图片被回收")
        check(referenced.exists(), "被记录引用的图片保留")
        check(just_saved.exists(), "刚存下来（宽限期内）的图片保留")
        check(stats["orphans_removed"] == 1, f"孤儿计数=1（实际 {stats['orphans_removed']}）")
        check(stats["removed_files"] == 3, f"删除计数=3（实际 {stats['removed_files']}）")
        check(stats["size_bytes"] == 200, f"剩余体积=200（实际 {stats['size_bytes']}）")


def test_sweep_images_quota_respects_grace() -> None:
    print("_sweep_images_sync 配额 + 宽限期")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        referenced = {os.path.normcase(os.path.abspath(root / "20260906" / f"msg_{i}.jpg")) for i in range(3)}
        inflight = write(root / "20260906" / "msg_0.jpg", 400, 20)
        old_a = write(root / "20260906" / "msg_1.jpg", 400, 3 * HOUR)
        old_b = write(root / "20260906" / "msg_2.jpg", 400, 2 * HOUR)

        # 配额 500B，实占 1200B：需要删到 500 以内，但 msg_0 在宽限期内不能碰
        stats = _sweep_images_sync(root, referenced, 0, NOW - 30 * 60, 500, NOW - 30 * 60)
        check(inflight.exists(), "配额裁剪不能删掉刚下载、可能正在渲染的新图片")
        check(not old_a.exists(), "配额裁剪从最旧的开始删")
        check(not old_b.exists(), "仍超配额就继续删次旧的")
        check(stats["removed_files"] == 2, f"删除计数=2（实际 {stats['removed_files']}）")
        check(stats["size_bytes"] == 400, f"宁可停在 400B > 配额也不动宽限期文件（实际 {stats['size_bytes']}）")


def test_sweep_images_quota_all_fresh() -> None:
    print("_sweep_images_sync 配额：全是新文件")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        fresh = [write(root / "20260906" / f"msg_{i}.jpg", 400, 20) for i in range(3)]
        referenced = {os.path.normcase(os.path.abspath(path)) for path in fresh}

        stats = _sweep_images_sync(root, referenced, 0, NOW - 30 * 60, 500, NOW - 30 * 60)
        check(all(path.exists() for path in fresh), "全是宽限期内的图片时宁可超配额也不删")
        check(stats["removed_files"] == 0, "宽限期文件不计入删除")


def test_enforce_quota() -> None:
    print("_enforce_quota_sync")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        renders = root / "renders"
        images = root / "message_images"
        oldest = write(renders / "mention_echo_1.jpg", 400, 10 * HOUR)
        middle = write(images / "20260906" / "msg_1.jpg", 400, 5 * HOUR)
        newest = write(renders / "mention_echo_2.jpg", 400, 2 * HOUR)
        sending = write(renders / "mention_echo_3.jpg", 400, 30)

        stats = _enforce_quota_sync([renders, images], 900, NOW - 600)
        check(not oldest.exists(), "总配额兜底跨目录按最旧优先删")
        check(not middle.exists(), "第二旧的也会被删（跨目录）")
        check(newest.exists(), "删到配额以内即停")
        check(sending.exists(), "宽限期内的文件在总配额兜底里同样受保护")
        check(stats["removed_files"] == 2, f"删除计数=2（实际 {stats['removed_files']}）")

        no_quota = _enforce_quota_sync([renders, images], 0, NOW - 600)
        check(no_quota["removed_files"] == 0, "配额=0（不限制）时不删任何东西")


def test_remove_empty_dirs() -> None:
    print("_remove_empty_dirs")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "a" / "b" / "c").mkdir(parents=True)
        write(root / "keep" / "f.jpg", 10, 0)
        removed = _remove_empty_dirs(root)
        check(removed == 3, f"自底向上删掉 3 层空目录（实际 {removed}）")
        check(not (root / "a").exists(), "空目录树被完全清掉")
        check((root / "keep").exists(), "有文件的目录保留")
        check(root.exists(), "root 本身永不删除")


def test_human_bytes() -> None:
    print("_human_bytes")
    check(_human_bytes(0) == "0 B", "0 字节")
    check(_human_bytes(512) == "512 B", "字节级")
    check(_human_bytes(1536) == "1.5 KB", "KB 级")
    check(_human_bytes(5 * 1024 * 1024) == "5.0 MB", "MB 级")
    check(_human_bytes(3 * 1024 ** 3) == "3.0 GB", "GB 级")
    check(_human_bytes(None) == "0 B", "None 兜底")
    check(_human_bytes(-5) == "0 B", "负数兜底")


def main() -> int:
    for test in (
        test_dir_entries,
        test_date_dir_expired,
        test_sweep_renders,
        test_sweep_renders_quota,
        test_sweep_renders_quota_respects_grace,
        test_sweep_images,
        test_sweep_images_quota_respects_grace,
        test_sweep_images_quota_all_fresh,
        test_enforce_quota,
        test_remove_empty_dirs,
        test_human_bytes,
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
