from __future__ import annotations

import re
from typing import Any

try:
    from .constants import *
except ImportError:
    from modules.constants import *


class ConfigMixin:
    def _config_value(self, *keys: str, default: Any = None) -> Any:
        value: Any = self.config
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                getter = getattr(value, "get", None)
                value = getter(key) if callable(getter) else None
            if value is None:
                return default
        return value

    def _config_bool(self, *keys: str, default: bool = False) -> bool:
        value = self._config_value(*keys, default=default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开启"}
        return bool(value)

    def _config_int(self, *keys: str, default: int = 0) -> int:
        value = self._config_value(*keys, default=default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def _config_str(self, *keys: str, default: str = "") -> str:
        value = self._config_value(*keys, default=default)
        return str(value if value is not None else default)

    def _config_list(self, *keys: str) -> list[str]:
        value = self._config_value(*keys, default=[])
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            parts = re.split(r"[\n,，]+", value)
            return [part.strip() for part in parts if part.strip()]
        return []

    def _format_template(self, template: str, **kwargs: Any) -> str:
        try:
            return template.format(**kwargs)
        except Exception:
            return template

    def _max_records_per_target(self) -> int:
        return max(1, self._config_int("record", "max_records_per_target", default=MAX_RECORDS_PER_TARGET))

    def _recent_image_cache_records(self) -> int:
        return max(0, self._config_int("record", "recent_image_cache_records", default=RECENT_IMAGE_CACHE_RECORDS))

    def _image_cache_retention_hours(self) -> int:
        return max(0, self._config_int("record", "image_cache_retention_hours", default=IMAGE_CACHE_RETENTION_HOURS))

    def _query_context_max_messages(self) -> int:
        return max(1, self._config_int("record", "query_context_max_messages", default=MAX_CONTEXT_MESSAGES))

    def _max_messages_per_image(self) -> int:
        return max(1, self._config_int("record", "max_messages_per_image", default=MAX_MESSAGES_PER_IMAGE))

    def _max_query_pages(self) -> int:
        return max(0, self._config_int("record", "max_query_pages", default=0))

    def _max_reminder_pages(self) -> int:
        return max(0, self._config_int("reminder", "max_reminder_pages", default=0))

    def _query_reverse_order(self) -> bool:
        value = self._config_str("record", "query_sort_order", default="asc").strip().lower()
        return value in {"desc", "倒序", "reverse", "newest_first", "latest_first", "最新在上"}

    def _render_mode(self) -> str:
        value = self._config_str("render", "render_mode", default="自动").strip().lower()
        if value in {"api", "api渲染", "html_render", "htmlrender"}:
            return "api"
        if value in {"browser", "local", "local_browser", "本地浏览器渲染", "浏览器渲染", "本地渲染"}:
            return "browser"
        if value in {"text", "plain", "纯文字", "文字"}:
            return "text"
        return "auto"

    def _render_text_only(self) -> bool:
        return self._render_mode() == "text"

    def _render_quality(self) -> int:
        return min(100, max(1, self._config_int("render", "image_quality", default=RENDER_IMAGE_QUALITY)))

    def _render_page_timeout_ms(self) -> int:
        return max(1000, self._config_int("render", "page_timeout_ms", default=RENDER_TIMEOUT_MS))

    def _render_task_timeout_sec(self) -> int:
        return max(3, self._config_int("render", "browser_timeout_seconds", default=RENDER_TASK_TIMEOUT_SEC))

    def _prefer_browser(self) -> bool:
        """默认关闭：先调 t2i 服务，失败才回退本地浏览器。"""
        return self._config_bool("render", "prefer_browser", default=False)

    def _t2i_cooldown_sec(self) -> int:
        return max(
            0,
            self._config_int("render", "t2i_failure_cooldown_sec", default=T2I_FAILURE_COOLDOWN_SECONDS),
        )

    def _header_image_url(self) -> str:
        return self._image_src("header")

    def _footer_image_url(self) -> str:
        return self._image_src("footer")

    def _reminder_away_seconds(self) -> int:
        return max(0, self._config_int("reminder", "away_minutes", default=10)) * 60

    def _reminder_min_messages(self) -> int:
        return max(0, self._config_int("reminder", "min_messages_after_mention", default=5))

    def _max_pending_reminders(self) -> int:
        return max(1, self._config_int("reminder", "max_pending_reminders", default=MAX_PENDING_REMINDERS))

    def _max_reminder_context(self) -> int:
        return max(0, self._config_int("reminder", "max_context_messages", default=MAX_REMINDER_CONTEXT))

    # ---- 自动清理 / 存储治理 ----
    def _maintenance_enabled(self) -> bool:
        return self._config_bool("maintenance", "auto_cleanup", default=True)

    def _maintenance_interval_hours(self) -> int:
        return max(1, self._config_int("maintenance", "interval_hours", default=MAINTENANCE_INTERVAL_HOURS))

    def _maintenance_first_run_delay(self) -> int:
        return max(
            5,
            self._config_int(
                "maintenance", "first_run_delay_sec", default=MAINTENANCE_FIRST_RUN_DELAY_SECONDS
            ),
        )

    def _record_retention_days(self) -> int:
        return max(0, self._config_int("maintenance", "record_retention_days", default=RECORD_RETENTION_DAYS))

    def _member_cache_retention_days(self) -> int:
        return max(
            0,
            self._config_int(
                "maintenance", "member_cache_retention_days", default=MEMBER_CACHE_RETENTION_DAYS
            ),
        )

    def _renders_max_mb(self) -> int:
        return max(0, self._config_int("maintenance", "renders_max_mb", default=RENDERS_MAX_MB))

    def _images_max_mb(self) -> int:
        return max(0, self._config_int("maintenance", "images_max_mb", default=IMAGES_MAX_MB))

    def _total_quota_mb(self) -> int:
        return max(0, self._config_int("maintenance", "total_quota_mb", default=TOTAL_QUOTA_MB))

    def _purge_orphan_keys(self) -> bool:
        return self._config_bool("maintenance", "purge_orphan_keys", default=True)

    def _cleanup_render_hours(self) -> int:
        return max(1, self._config_int("render", "cleanup_render_hours", default=CLEANUP_RENDER_HOURS))

    # ---- 功能开关 ----
    def _rank_enabled(self) -> bool:
        return self._config_bool("feature", "rank_enabled", default=True)

    def _llm_tool_enabled(self) -> bool:
        return self._config_bool("feature", "llm_tool_enabled", default=False)

    def _global_group_allowed(self, event: Any) -> bool:
        enabled_umos = self._global_enabled_group_umos()
        return not enabled_umos or self._event_umo(event) in enabled_umos

    def _global_enabled_group_umos(self) -> set[str]:
        return set(self._config_list("global", "enabled_group_umos"))

    def _reminder_enabled_group_umos(self) -> set[str]:
        return set(self._config_list("reminder", "enabled_group_umos"))

    def _reminder_user_whitelist(self) -> set[str]:
        return set(self._config_list("reminder", "user_whitelist"))

    def _reminder_user_blacklist(self) -> set[str]:
        return set(self._config_list("reminder", "user_blacklist"))

    def _reminder_user_allowed(self, user_id: str) -> bool:
        user_id = str(user_id or "").strip()
        if not user_id:
            return False
        if user_id in self._reminder_user_blacklist():
            return False
        whitelist = self._reminder_user_whitelist()
        return not whitelist or user_id in whitelist

    def _reminder_user_rule_status(self, user_id: str) -> str:
        user_id = str(user_id or "").strip()
        whitelist = self._reminder_user_whitelist()
        blacklist = self._reminder_user_blacklist()
        if user_id and user_id in blacklist:
            return "黑名单命中"
        if whitelist:
            return "白名单命中" if user_id in whitelist else "白名单未命中"
        if blacklist:
            return "黑名单未命中"
        return "未配置名单"

    def _event_umo(self, event: Any) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    def _record_key(self, group_id: str, target: str) -> str:
        return f"records:{group_id}:{target}"

    def _context_key(self, group_id: str) -> str:
        return f"context:{group_id}"

    def _reminder_group_key(self, group_id: str) -> str:
        return f"reminder:group_enabled:{group_id}"

    def _reminder_user_key(self, group_id: str, user_id: str) -> str:
        return f"reminder:user_enabled:{group_id}:{user_id}"

    def _reminder_pending_key(self, group_id: str, user_id: str) -> str:
        return f"reminder:pending:{group_id}:{user_id}"

    def _reminder_context_key(self, group_id: str) -> str:
        return f"reminder:context:{group_id}"

    def _member_cache_key(self, group_id: str, user_id: str) -> str:
        return f"member:name:{group_id}:{user_id}"
