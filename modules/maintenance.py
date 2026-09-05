"""艾特回声（mention_echo）的自动清理与存储治理。

上游插件只在"下一次渲染"时机会性地清理一次图片缓存，于是长期运行后会出现三处膨胀：

1. ``renders/``：生成的长图只在下次渲染时才被清理，一旦不再查询就永久留存；
2. ``message_images/``：崩溃或热重载导致 ``finally`` 未执行，留下无人引用的孤儿图片；
3. KV 记录：只有"每目标最多 N 条"的条数上限，没有时间上限，SQLite 会随
   群数 × 成员数持续变大。

本模块提供一个低频后台巡检（默认每 6 小时一次），负责：过期记录 TTL、成员缓存 TTL、
三个手工索引的自愈重建、孤儿键回收、渲染目录与图片缓存的分项配额 + 总配额兜底，
以及运行时内存字典的回收。所有阻塞式文件 IO 都通过 ``asyncio.to_thread`` 执行。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:
    from .constants import *
except ImportError:
    from modules.constants import *


_MB = 1024 * 1024
_DAY = 86400


# --------------------------------------------------------------------------- #
# 同步辅助函数（在线程池中执行，禁止触碰事件循环或 self）
# --------------------------------------------------------------------------- #
def _normalize_path(value: Any) -> str:
    """把路径统一成可比较的形式，供"被引用文件"集合与磁盘文件做交叉比对。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.abspath(text))
    except (OSError, ValueError):
        return ""


def _estimate_bytes(value: Any) -> int:
    """估算一个 KV 值序列化后的字节数，仅用于展示。"""
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _human_bytes(size: Any) -> str:
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        value = 0.0
    value = max(0.0, value)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _format_time(timestamp: Any) -> str:
    try:
        value = float(timestamp or 0)
    except (TypeError, ValueError):
        return "从未"
    if value <= 0:
        return "从未"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def _dir_entries(root: Path) -> list[tuple[Path, float, int]]:
    """递归收集目录下所有普通文件的 ``(路径, mtime, 大小)``，不跟随符号链接。"""
    entries: list[tuple[Path, float, int]] = []
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as scanner:
                for item in scanner:
                    try:
                        if item.is_dir(follow_symlinks=False):
                            stack.append(Path(item.path))
                            continue
                        if not item.is_file(follow_symlinks=False):
                            continue
                        stat_result = item.stat()
                    except OSError:
                        continue
                    entries.append((Path(item.path), stat_result.st_mtime, stat_result.st_size))
        except OSError:
            continue
    return entries


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _remove_empty_dirs(root: Path) -> int:
    """自底向上删除空子目录，保留 ``root`` 本身。"""
    try:
        candidates = [item for item in root.rglob("*") if item.is_dir()]
    except OSError:
        return 0
    removed = 0
    for path in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
        try:
            if any(path.iterdir()):
                continue
            path.rmdir()
            removed += 1
        except OSError:
            continue
    return removed


def _date_dir_expired(path: Path, root: Path, cutoff_ymd: str) -> bool:
    """按 ``message_images/YYYYMMDD/`` 这一层日期目录判断过期。

    直接做字符串比较（``"20260101" < "20260201"``）即可等价于按日期比较，
    既省掉一次日期解析，也避免引入不带时区的 ``datetime`` 调用。
    """
    if not cutoff_ymd:
        return False
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    for part in relative.parts[:-1]:
        if len(part) == 8 and part.isdigit():
            return part < cutoff_ymd
    return False


def _sweep_renders_sync(
    render_dir: Path,
    expire_before: float,
    quota_bytes: int,
    grace_before: float,
) -> dict[str, Any]:
    """清理 ``renders/``：先按保留时长删，再按目录配额删最旧的。

    ``grace_before`` 之后修改过的文件视为"可能正在发送中"，计入体积但绝不删除。
    """
    managed_prefixes = (RENDER_FILE_PREFIX, *LEGACY_RENDER_FILE_PREFIXES)
    removed = 0
    freed = 0
    total = 0
    kept: list[tuple[Path, float, int]] = []

    for path, mtime, size in _dir_entries(render_dir):
        if not path.name.startswith(managed_prefixes):
            continue
        if path.suffix.lower() not in RENDER_IMAGE_SUFFIXES:
            continue
        total += size
        if mtime >= grace_before:
            continue
        if expire_before > 0 and mtime < expire_before:
            if _unlink(path):
                removed += 1
                freed += size
                total -= size
            continue
        kept.append((path, mtime, size))

    if quota_bytes > 0 and total > quota_bytes:
        for path, _mtime, size in sorted(kept, key=lambda item: item[1]):
            if total <= quota_bytes:
                break
            if _unlink(path):
                removed += 1
                freed += size
                total -= size

    return {
        "removed_files": removed,
        "removed_dirs": _remove_empty_dirs(render_dir),
        "freed_bytes": freed,
        "size_bytes": max(0, total),
    }


def _sweep_images_sync(
    cache_dir: Path,
    referenced: set[str],
    expire_before: float,
    orphan_before: float,
    quota_bytes: int,
    grace_before: float,
) -> dict[str, Any]:
    """清理 ``message_images/``：过期 + 孤儿 + 配额三重扫描。

    删除仍被记录引用的过期图片是安全的：``_cache_record_direct_images`` 会丢弃
    本地文件已不存在的缓存条目，渲染时按原始 URL 重新下载。

    ``grace_before`` 之后落盘的文件不参与配额裁剪：维护锁只串行化「维护 vs 维护」，
    并不阻塞查询，刚下载完还没渲染进图的文件必须留住，宁可暂时超配额。
    """
    cutoff_ymd = time.strftime("%Y%m%d", time.localtime(expire_before)) if expire_before > 0 else ""
    removed = 0
    freed = 0
    orphans = 0
    total = 0
    kept: list[tuple[Path, float, int]] = []

    for path, mtime, size in _dir_entries(cache_dir):
        total += size
        expired = expire_before > 0 and (
            mtime < expire_before or _date_dir_expired(path, cache_dir, cutoff_ymd)
        )
        orphaned = mtime < orphan_before and _normalize_path(path) not in referenced
        if not expired and not orphaned:
            kept.append((path, mtime, size))
            continue
        if _unlink(path):
            removed += 1
            freed += size
            total -= size
            if orphaned and not expired:
                orphans += 1

    if quota_bytes > 0 and total > quota_bytes:
        for path, mtime, size in sorted(kept, key=lambda item: item[1]):
            if total <= quota_bytes:
                break
            if mtime >= grace_before:
                continue
            if _unlink(path):
                removed += 1
                freed += size
                total -= size

    return {
        "removed_files": removed,
        "removed_dirs": _remove_empty_dirs(cache_dir),
        "freed_bytes": freed,
        "orphans_removed": orphans,
        "size_bytes": max(0, total),
    }


def _enforce_quota_sync(dirs: list[Path], quota_bytes: int, grace_before: float) -> dict[str, Any]:
    """总配额兜底：跨目录按 mtime 从旧到新删，直到回到配额以内。"""
    entries: list[tuple[Path, float, int]] = []
    for directory in dirs:
        entries.extend(_dir_entries(directory))

    total = sum(item[2] for item in entries)
    removed = 0
    freed = 0
    if quota_bytes > 0 and total > quota_bytes:
        for path, mtime, size in sorted(entries, key=lambda item: item[1]):
            if total <= quota_bytes:
                break
            if mtime >= grace_before:
                continue
            if _unlink(path):
                removed += 1
                freed += size
                total -= size
        if removed:
            for directory in dirs:
                _remove_empty_dirs(directory)

    return {
        "removed_files": removed,
        "freed_bytes": freed,
        "size_bytes": max(0, total),
    }


def _measure_dirs(dirs: dict[str, Path]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, path in dirs.items():
        entries = _dir_entries(path)
        try:
            exists = path.is_dir()
        except OSError:
            exists = False
        result[name] = {
            "files": len(entries),
            "size_bytes": sum(item[2] for item in entries),
            "exists": exists,
        }
    return result

# --------------------------------------------------------------------------- #
# Mixin
# --------------------------------------------------------------------------- #
class MaintenanceMixin:
    """定时巡检 + 手动清理。混入 ``MentionEchoPlugin``。"""

    # ---- 基础设施 ----
    def _shared_preferences(self) -> Any:
        """取 AstrBot 的偏好存储实例，用于全量枚举本插件的 KV 数据。"""
        try:
            from astrbot.core import sp

            return sp
        except Exception as exc:
            logger.debug(f"{LOG_TAG} 无法访问偏好存储: {exc}")
            return None

    def _kv_scope_id(self) -> str:
        return str(getattr(self, "plugin_id", "") or "")

    def _maintenance_lock(self) -> asyncio.Lock:
        """保证定时巡检与 #艾特清理 不会并发执行。"""
        lock = getattr(self, "_maintenance_lock_instance", None)
        if lock is None:
            lock = asyncio.Lock()
            self._maintenance_lock_instance = lock
        return lock

    async def _maintenance_put(self, key: str, value: Any) -> bool:
        try:
            await self.put_kv_data(key, value)
            return True
        except Exception as exc:
            logger.warning(f"{LOG_TAG} 维护写入失败 {key}: {exc}")
            return False

    async def _maintenance_delete(self, key: str) -> bool:
        try:
            await self.delete_kv_data(key)
            return True
        except Exception as exc:
            logger.warning(f"{LOG_TAG} 维护删除失败 {key}: {exc}")
            return False

    async def _collect_kv_entries(self) -> tuple[list[tuple[str, Any]], bool]:
        """枚举本插件全部 KV 键值。

        返回 ``(entries, full_scan)``。``full_scan`` 为 True 表示结果覆盖整个 scope，
        此时才允许回收"不认识的键"；否则只能依赖三个手工索引，可能漏掉漂移出去的键。
        """
        preferences_api = self._shared_preferences()
        scope_id = self._kv_scope_id()
        if preferences_api is not None and scope_id:
            try:
                preferences = await preferences_api.range_get_async("plugin", scope_id, None)
            except Exception as exc:
                logger.warning(f"{LOG_TAG} 全量枚举 KV 失败，退化为索引扫描: {exc}")
            else:
                entries: list[tuple[str, Any]] = []
                for preference in preferences or []:
                    key = str(getattr(preference, "key", "") or "")
                    if not key:
                        continue
                    raw = getattr(preference, "value", None)
                    value = raw.get("val") if isinstance(raw, dict) and "val" in raw else raw
                    entries.append((key, value))
                return entries, True

        entries = []
        seen: set[str] = set()
        for index_key in (INDEX_KEY, CONTEXT_INDEX_KEY, REMINDER_PENDING_INDEX_KEY):
            stored = await self.get_kv_data(index_key, [])
            if not isinstance(stored, list):
                continue
            for item in stored:
                key = str(item or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                entries.append((key, await self.get_kv_data(key, None)))
        return entries, False

    # ---- 调度 ----
    def _start_maintenance(self) -> None:
        if not self._maintenance_enabled():
            logger.info(f"{LOG_TAG} 自动清理已关闭，可在插件配置的「自动清理」分组中开启")
            return
        task = getattr(self, "_maintenance_task", None)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(f"{LOG_TAG} 没有运行中的事件循环，自动清理未启动")
            return
        self._maintenance_task = loop.create_task(
            self._maintenance_loop(),
            name="mention_echo_maintenance",
        )
        logger.info(
            f"{LOG_TAG} 自动清理已启动：{self._maintenance_first_run_delay()} 秒后首次巡检，"
            f"之后每 {self._maintenance_interval_hours()} 小时一次"
        )

    async def _stop_maintenance(self) -> None:
        task = getattr(self, "_maintenance_task", None)
        self._maintenance_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.debug(f"{LOG_TAG} 自动清理任务已停止")
        except Exception as exc:
            logger.debug(f"{LOG_TAG} 停止自动清理任务时出现异常: {exc}")

    async def _maintenance_loop(self) -> None:
        await asyncio.sleep(self._maintenance_first_run_delay())
        while True:
            try:
                await self.run_maintenance("scheduled")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"{LOG_TAG} 自动清理执行失败: {exc}")
            await asyncio.sleep(self._maintenance_interval_hours() * 3600)

    # ---- 主流程 ----
    async def run_maintenance(self, reason: str = "scheduled") -> dict[str, Any]:
        """跑一轮完整巡检并返回统计报告。"""
        started = time.time()
        async with self._maintenance_lock():
            kv_stats = await self._sweep_kv()
            referenced: set[str] = kv_stats.pop("referenced", set())
            render_stats = await self._sweep_render_dir()
            image_stats = await self._sweep_image_cache(referenced)
            quota_stats = await self._enforce_total_quota()
            runtime_stats = self._prune_runtime_caches()

        removed_files = (
            int(render_stats.get("removed_files", 0))
            + int(image_stats.get("removed_files", 0))
            + int(quota_stats.get("removed_files", 0))
        )
        freed_bytes = (
            int(render_stats.get("freed_bytes", 0))
            + int(image_stats.get("freed_bytes", 0))
            + int(quota_stats.get("freed_bytes", 0))
        )
        db_reclaimed_bytes = int(kv_stats.get("bytes_reclaimed", 0))
        report: dict[str, Any] = {
            "reason": reason,
            "elapsed": round(time.time() - started, 3),
            "removed_files": removed_files,
            "freed_bytes": freed_bytes,
            "db_reclaimed_bytes": db_reclaimed_bytes,
            "kv": kv_stats,
            "renders": render_stats,
            "images": image_stats,
            "quota": quota_stats,
            "runtime": runtime_stats,
        }
        await self._save_maintenance_state(report)

        summary = (
            f"{LOG_TAG} 巡检完成（{reason}）：删除文件 {removed_files} 个 / "
            f"释放 {_human_bytes(freed_bytes)}，数据库瘦身 {_human_bytes(db_reclaimed_bytes)}，"
            f"过期记录 {kv_stats.get('records_removed', 0)} 条，"
            f"回收键 {kv_stats.get('keys_removed', 0)} 个，耗时 {report['elapsed']:.2f}s"
        )
        if (
            removed_files
            or freed_bytes
            or db_reclaimed_bytes
            or kv_stats.get("records_removed")
            or kv_stats.get("keys_removed")
        ):
            logger.info(summary)
        else:
            logger.debug(summary)
        return report

    # ---- KV 清理 ----
    def _filter_records(
        self,
        value: Any,
        cutoff: float,
        limit: int,
    ) -> tuple[list[dict[str, Any]] | None, int]:
        """按时间与条数裁剪一个记录列表；结构损坏时返回 ``(None, 0)``。"""
        if not isinstance(value, list):
            return None, 0
        original = len(value)
        kept = [item for item in value if isinstance(item, dict)]
        if cutoff > 0:
            kept = [item for item in kept if self._record_time(item) >= cutoff]
        if limit > 0 and len(kept) > limit:
            kept = kept[-limit:]
        return kept, max(0, original - len(kept))

    def _normalized_record_paths(self, record: dict[str, Any]) -> set[str]:
        paths: set[str] = set()
        for value in self._record_image_cache_paths(record):
            normalized = _normalize_path(value)
            if normalized:
                paths.add(normalized)
        return paths

    async def _sweep_kv(self) -> dict[str, Any]:
        """记录 TTL、成员缓存 TTL、孤儿键回收与索引自愈，全程持有维护写锁。

        枚举必须放在锁内：否则巡检期间新写入的键会被误判成"索引里没有"。
        """
        now = time.time()
        record_days = self._record_retention_days()
        member_days = self._member_cache_retention_days()
        record_cutoff = now - record_days * _DAY if record_days > 0 else 0.0
        member_cutoff = now - member_days * _DAY if member_days > 0 else 0.0
        max_records = self._max_records_per_target()
        max_pending = self._max_pending_reminders()
        purge_orphans = self._purge_orphan_keys()
        delete_budget = MAINTENANCE_MAX_KV_DELETES

        referenced: set[str] = set()
        record_keys: set[str] = set()
        context_keys: set[str] = set()
        pending_keys: set[str] = set()
        stats: dict[str, Any] = {
            "scanned_keys": 0,
            "keys_removed": 0,
            "records_removed": 0,
            "members_removed": 0,
            "orphans_removed": 0,
            "index_rebuilt": 0,
            "bytes_reclaimed": 0,
            "full_scan": False,
            "budget_exhausted": False,
        }

        async def drop(key: str, *, kind: str = "") -> None:
            nonlocal delete_budget
            if delete_budget <= 0:
                stats["budget_exhausted"] = True
                return
            if not await self._maintenance_delete(key):
                return
            delete_budget -= 1
            stats["keys_removed"] += 1
            if kind:
                stats[kind] += 1

        async with self._data_maintenance():
            entries, full_scan = await self._collect_kv_entries()
            stats["full_scan"] = full_scan
            stats["scanned_keys"] = len(entries)

            for key, value in entries:
                if key in KNOWN_KV_KEYS:
                    continue

                if key.startswith("records:"):
                    kept, dropped = self._filter_records(value, record_cutoff, max_records)
                    stats["records_removed"] += dropped
                    if not kept:
                        await drop(key)
                        continue
                    kept, freed = self._slim_records_for_storage(kept)
                    stats["bytes_reclaimed"] += freed
                    if dropped or freed:
                        await self._maintenance_put(key, kept)
                    record_keys.add(key)
                    for record in kept:
                        referenced |= self._normalized_record_paths(record)
                    continue

                if key.startswith("reminder:pending:"):
                    kept, dropped = self._filter_records(value, record_cutoff, max_pending)
                    stats["records_removed"] += dropped
                    if not kept:
                        await drop(key)
                        continue
                    kept, freed = self._slim_records_for_storage(kept)
                    stats["bytes_reclaimed"] += freed
                    if dropped or freed:
                        await self._maintenance_put(key, kept)
                    pending_keys.add(key)
                    for record in kept:
                        referenced |= self._normalized_record_paths(record)
                    continue

                if key.startswith("context:"):
                    if value:
                        context_keys.add(key)
                    else:
                        await drop(key)
                    continue

                if key.startswith("member:"):
                    if member_cutoff <= 0:
                        continue
                    stored = value if isinstance(value, dict) else {}
                    try:
                        remembered = float(stored.get("time") or 0)
                    except (TypeError, ValueError):
                        remembered = 0.0
                    if remembered < member_cutoff:
                        await drop(key, kind="members_removed")
                    continue

                # reminder:group_enabled / reminder:user_enabled / reminder:context 是用户设置，
                # maintenance:* 是插件自身状态，都必须长期保留。
                if key.startswith(("reminder:", "maintenance:")):
                    continue

                if not key.startswith(KNOWN_KV_PREFIXES) and full_scan and purge_orphans:
                    await drop(key, kind="orphans_removed")

            stats["index_rebuilt"] += await self._sync_index(INDEX_KEY, record_keys)
            stats["index_rebuilt"] += await self._sync_index(CONTEXT_INDEX_KEY, context_keys)
            stats["index_rebuilt"] += await self._sync_index(REMINDER_PENDING_INDEX_KEY, pending_keys)

        stats["referenced"] = referenced
        return stats

    async def _sync_index(self, index_key: str, keys: set[str]) -> int:
        """把手工索引对齐到实际存在的键集合，返回是否发生了重建。"""
        stored = await self.get_kv_data(index_key, [])
        current: set[str] = set()
        if isinstance(stored, list):
            for item in stored:
                text = str(item or "").strip()
                if text:
                    current.add(text)
        if current == keys:
            return 0
        if keys:
            await self._maintenance_put(index_key, sorted(keys))
        else:
            await self._maintenance_delete(index_key)
        return 1

    # ---- 文件清理 ----
    async def _sweep_render_dir(self) -> dict[str, Any]:
        now = time.time()
        return await asyncio.to_thread(
            _sweep_renders_sync,
            Path(self._render_dir()),
            now - self._cleanup_render_hours() * 3600,
            self._renders_max_mb() * _MB,
            now - RENDER_GRACE_MINUTES * 60,
        )

    async def _sweep_image_cache(self, referenced: set[str]) -> dict[str, Any]:
        now = time.time()
        retention_hours = self._image_cache_retention_hours()
        expire_before = now - retention_hours * 3600 if retention_hours > 0 else 0.0
        grace_before = now - ORPHAN_IMAGE_GRACE_MINUTES * 60
        return await asyncio.to_thread(
            _sweep_images_sync,
            Path(self._message_image_cache_dir()),
            referenced,
            expire_before,
            grace_before,
            self._images_max_mb() * _MB,
            grace_before,
        )

    async def _enforce_total_quota(self) -> dict[str, Any]:
        quota_bytes = self._total_quota_mb() * _MB
        if quota_bytes <= 0:
            return {"removed_files": 0, "freed_bytes": 0, "size_bytes": 0}
        return await asyncio.to_thread(
            _enforce_quota_sync,
            [Path(self._render_dir()), Path(self._message_image_cache_dir())],
            quota_bytes,
            time.time() - RENDER_GRACE_MINUTES * 60,
        )

    # ---- 运行时内存回收 ----
    def _prune_runtime_caches(self) -> dict[str, Any]:
        """回收无限增长的运行时字典。

        锁字典可以安全回收：``async with self._kv_lock(key)`` 从取锁到进入临界区之间
        没有 await 挂起点，因此"未被持有"的锁一定没有等待者，丢弃后按需重建即可。
        """
        stats = {"kv_locks": 0, "pipeline_locks": 0, "activity": 0}
        for attribute, label in (("_kv_locks", "kv_locks"), ("_group_pipeline_locks", "pipeline_locks")):
            locks = getattr(self, attribute, None)
            if not isinstance(locks, dict) or len(locks) <= MAINTENANCE_LOCK_CACHE_LIMIT:
                continue
            idle = [name for name, lock in locks.items() if not lock.locked()]
            for name in idle:
                locks.pop(name, None)
            stats[label] = len(idle)

        activity = getattr(self, "_latest_group_activity", None)
        if isinstance(activity, dict) and len(activity) > MAINTENANCE_ACTIVITY_LIMIT:
            cutoff = time.time() - _DAY
            stale = []
            for entry_key, entry in activity.items():
                timestamp = 0.0
                if isinstance(entry, tuple) and entry:
                    try:
                        timestamp = float(entry[0] or 0)
                    except (TypeError, ValueError):
                        timestamp = 0.0
                if timestamp < cutoff:
                    stale.append(entry_key)
            for entry_key in stale:
                activity.pop(entry_key, None)
            stats["activity"] = len(stale)
        return stats

    # ---- 状态与报告 ----
    async def _save_maintenance_state(self, report: dict[str, Any]) -> None:
        await self._maintenance_put(
            MAINTENANCE_STATE_KEY,
            {
                "last_run": int(time.time()),
                "reason": str(report.get("reason") or ""),
                "freed_bytes": int(report.get("freed_bytes") or 0),
                "db_reclaimed_bytes": int(report.get("db_reclaimed_bytes") or 0),
                "removed_files": int(report.get("removed_files") or 0),
                "elapsed": float(report.get("elapsed") or 0.0),
            },
        )

    async def _storage_report(self) -> dict[str, Any]:
        data_dir = Path(self._plugin_data_dir())
        measured = await asyncio.to_thread(
            _measure_dirs,
            {
                "renders": Path(self._render_dir()),
                "message_images": Path(self._message_image_cache_dir()),
                "resources": data_dir / "resources",
            },
        )
        state = await self.get_kv_data(MAINTENANCE_STATE_KEY, {})
        return {
            "dirs": measured,
            "kv": await self._kv_summary(),
            "state": state if isinstance(state, dict) else {},
            "data_dir": str(data_dir),
        }

    async def _kv_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "keys": 0,
            "record_keys": 0,
            "records": 0,
            "pending_keys": 0,
            "pending": 0,
            "context_keys": 0,
            "member_keys": 0,
            "other_keys": 0,
            "size_bytes": 0,
            "oldest_record": 0,
            "full_scan": False,
        }
        async with self._data_operation():
            entries, full_scan = await self._collect_kv_entries()
        summary["full_scan"] = full_scan
        summary["keys"] = len(entries)

        oldest = 0.0
        for key, value in entries:
            summary["size_bytes"] += _estimate_bytes(value)
            if key in KNOWN_KV_KEYS:
                summary["other_keys"] += 1
            elif key.startswith("records:"):
                summary["record_keys"] += 1
                if isinstance(value, list):
                    summary["records"] += len(value)
                    for record in value:
                        if not isinstance(record, dict):
                            continue
                        stamp = float(self._record_time(record))
                        if stamp > 0 and (oldest <= 0 or stamp < oldest):
                            oldest = stamp
            elif key.startswith("reminder:pending:"):
                summary["pending_keys"] += 1
                if isinstance(value, list):
                    summary["pending"] += len(value)
            elif key.startswith("context:"):
                summary["context_keys"] += 1
            elif key.startswith("member:"):
                summary["member_keys"] += 1
            else:
                summary["other_keys"] += 1
        summary["oldest_record"] = int(oldest)
        return summary

    async def _storage_report_text(self) -> str:
        report = await self._storage_report()
        dirs = report.get("dirs", {})
        renders = dirs.get("renders", {})
        images = dirs.get("message_images", {})
        resources = dirs.get("resources", {})
        kv = report.get("kv", {})
        state = report.get("state", {})
        managed = int(renders.get("size_bytes", 0)) + int(images.get("size_bytes", 0))

        lines = [
            f"{PLUGIN_DISPLAY_NAME} · 存储状态",
            "",
            f"成图缓存 renders：{_human_bytes(renders.get('size_bytes'))}（{renders.get('files', 0)} 个文件 / 上限 {self._renders_max_mb()} MB）",
            f"图片缓存 message_images：{_human_bytes(images.get('size_bytes'))}（{images.get('files', 0)} 个文件 / 上限 {self._images_max_mb()} MB）",
            f"受管目录合计：{_human_bytes(managed)} / 总配额 {self._total_quota_mb()} MB",
            f"用户资源 resources：{_human_bytes(resources.get('size_bytes'))}（{resources.get('files', 0)} 个文件，自动清理不会删除）",
            "",
            f"记录数据：{kv.get('records', 0)} 条 / {kv.get('record_keys', 0)} 个会话，约 {_human_bytes(kv.get('size_bytes'))}",
            f"待发提醒：{kv.get('pending', 0)} 条 / {kv.get('pending_keys', 0)} 个会话｜上下文开启：{kv.get('context_keys', 0)} 个群｜成员缓存：{kv.get('member_keys', 0)} 条",
            f"最早记录：{_format_time(kv.get('oldest_record'))}（保留 {self._record_retention_days() or '不限'} 天）",
            "",
            f"上次清理：{_format_time(state.get('last_run'))}｜释放 {_human_bytes(state.get('freed_bytes'))}｜删除 {state.get('removed_files', 0)} 个文件｜数据库瘦身 {_human_bytes(state.get('db_reclaimed_bytes'))}",
            f"自动清理：{'开启' if self._maintenance_enabled() else '关闭'}（每 {self._maintenance_interval_hours()} 小时）｜成员缓存保留 {self._member_cache_retention_days() or '不限'} 天",
        ]
        if not kv.get("full_scan"):
            lines.append("")
            lines.append("提示：当前无法全量枚举数据库，统计仅覆盖索引内的键。")
        return "\n".join(lines)

    def _maintenance_report_text(self, report: dict[str, Any]) -> str:
        kv = report.get("kv", {})
        renders = report.get("renders", {})
        images = report.get("images", {})
        runtime = report.get("runtime", {})
        lines = [
            f"{PLUGIN_DISPLAY_NAME} · 清理完成（耗时 {float(report.get('elapsed') or 0):.2f}s）",
            "",
            f"删除文件 {report.get('removed_files', 0)} 个，释放 {_human_bytes(report.get('freed_bytes'))}",
            f"数据库瘦身：剥离图片原文 {_human_bytes(kv.get('bytes_reclaimed'))}",
            f"清理过期/超量记录 {kv.get('records_removed', 0)} 条",
            f"回收 KV 键 {kv.get('keys_removed', 0)} 个（成员缓存 {kv.get('members_removed', 0)}，孤儿键 {kv.get('orphans_removed', 0)}）",
            f"索引重建 {kv.get('index_rebuilt', 0)} 处，扫描键 {kv.get('scanned_keys', 0)} 个",
            f"当前占用：renders {_human_bytes(renders.get('size_bytes'))}｜message_images {_human_bytes(images.get('size_bytes'))}",
        ]
        recycled = int(runtime.get("kv_locks", 0)) + int(runtime.get("pipeline_locks", 0))
        if recycled or runtime.get("activity"):
            lines.append(
                f"运行时缓存回收：锁 {recycled} 项，活跃度记录 {runtime.get('activity', 0)} 项"
            )
        if not kv.get("full_scan"):
            lines.append("")
            lines.append("提示：本轮无法全量枚举数据库，孤儿键回收已跳过。")
        if kv.get("budget_exhausted"):
            lines.append("提示：本轮删除量已达上限，剩余部分会在下次巡检继续。")
        return "\n".join(lines)

    # ---- 旧插件数据迁移 ----
    async def _migrate_legacy_kv(self) -> int:
        """首次启动时把"谁艾特我 / 谁艾特我 Pro"的 KV 数据复制过来（只读不删）。"""
        try:
            marker = await self.get_kv_data(LEGACY_KV_MARKER_KEY, None)
        except Exception as exc:
            logger.debug(f"{LOG_TAG} 读取迁移标记失败: {exc}")
            return 0
        if marker:
            return 0

        preferences_api = self._shared_preferences()
        scope_id = self._kv_scope_id()
        if preferences_api is None or not scope_id:
            return 0

        reserved = {LEGACY_KV_MARKER_KEY, MAINTENANCE_STATE_KEY}
        try:
            own = await preferences_api.range_get_async("plugin", scope_id, None)
        except Exception as exc:
            logger.debug(f"{LOG_TAG} 检查自身 KV 数据失败: {exc}")
            return 0
        if any(str(getattr(item, "key", "") or "") not in reserved for item in own or []):
            await self._maintenance_put(LEGACY_KV_MARKER_KEY, int(time.time()))
            return 0

        copied = 0
        for legacy_scope in LEGACY_KV_SCOPE_IDS:
            if legacy_scope == scope_id:
                continue
            try:
                legacy_entries = await preferences_api.range_get_async("plugin", legacy_scope, None)
            except Exception as exc:
                logger.debug(f"{LOG_TAG} 读取旧插件 KV 失败 {legacy_scope}: {exc}")
                continue
            for item in legacy_entries or []:
                key = str(getattr(item, "key", "") or "")
                if not key or key in reserved:
                    continue
                raw = getattr(item, "value", None)
                value = raw.get("val") if isinstance(raw, dict) and "val" in raw else raw
                if await self._maintenance_put(key, value):
                    copied += 1
            if copied:
                break

        await self._maintenance_put(LEGACY_KV_MARKER_KEY, int(time.time()))
        if copied:
            logger.info(f"{LOG_TAG} 已从旧插件迁移 {copied} 条数据（旧数据保留未删除）")
        return copied

    async def _purge_member_cache(self) -> int:
        """清空全部 ``member:*`` 名片缓存，返回删除条数。

        该前缀没有索引，只能整体枚举；若 KV 全量枚举不可用（降级为索引扫描），
        则拿不到 member 键，此时返回 0 而不是报错。
        调用方 ``_clear_all_locked`` 已持有数据锁，这里不再取锁。
        """
        entries, _full_scan = await self._collect_kv_entries()
        removed = 0
        for key, _value in entries:
            if key.startswith("member:") and await self._maintenance_delete(key):
                removed += 1
        return removed
