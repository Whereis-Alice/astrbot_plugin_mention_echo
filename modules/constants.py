from __future__ import annotations

import re


PLUGIN_NAME = "astrbot_plugin_mention_echo"
PLUGIN_DISPLAY_NAME = "艾特回声"
LOG_TAG = "[艾特回声]"

# 上游插件（谁艾特我 / 谁艾特我 Pro）的数据目录名与 KV scope，仅用于一次性迁移和兼容清理。
LEGACY_PLUGIN_NAMES = ("astrbot_plugin_who_at_me_pro", "astrbot_plugin_who_at_me")
LEGACY_KV_SCOPE_IDS = (
    "xiaoruange39/astrbot_plugin_who_at_me_pro",
    "xiaoruange39/astrbot_plugin_who_at_me",
)
LEGACY_DATA_MARKER = ".migrated_from_legacy_who_at_me"
LEGACY_KV_MARKER_KEY = "maintenance:legacy_kv_migrated"
MAINTENANCE_STATE_KEY = "maintenance:state"

MAX_IMAGE_SOURCE_BYTES = 20 * 1024 * 1024
MEMBER_CACHE_TTL_SECONDS = 5 * 60

# ---- 图片存储约束（v1.1.0 起的硬规则）----
# 数据库（KV）里只允许出现「可再取回的轻量引用」，图片字节一律落在插件数据目录的
# 文件里，由存储配额与自动清理统一管理。允许的三种引用形态：
#   1. http(s) 链接            —— 最省空间，渲染时按需下载
#   2. mecache://<相对路径>     —— 指向插件图片缓存目录内的文件
#   3. IMAGE_EXPIRED_REF       —— 原图已按策略释放，只保留「图片已过期」占位
IMAGE_REF_PREFIX = "mecache://"
IMAGE_EXPIRED_REF = "mecache://expired"
# 单个图片引用允许的最大字符数；超过就判定成图片原文，直接换成过期占位。
MAX_IMAGE_REF_CHARS = 1024
# 单条记录正文允许的最大字符数，超出截断，避免异常长文本撑爆数据库。
MAX_RECORD_TEXT_CHARS = 2000
# 允许被截断的文本字段；其它字段（哈希、路径、ID）一律不动。
# 这些字段天然只该放「图片引用」：一旦超过 MAX_IMAGE_REF_CHARS，
# 就必然是历史遗留的图片原文（含没有 base64:// 前缀的裸原文），可以直接判过期。
IMAGE_PAYLOAD_KEYS = frozenset(
    {
        "images",
        "image",
        "cover",
        "source",
        "local",
        "url",
        "file",
        "path",
        "thumb",
        "thumbnail",
    }
)
SLIMMABLE_TEXT_KEYS = frozenset(
    {"message", "message_after_images", "text", "content", "title", "summary"}
)


QUERY_PATTERN = re.compile(r"^(谁(艾特|@|at)(我|他|她|它)|哪个逼(艾特|@|at)我)(?:\s*(?:\[CQ:at,[^\]]+\]|@.+))?$", re.I)
HELP_PATTERN = re.compile(r"^(艾特帮助|at_help|who_at_me_help|mention_echo_help|help_at)$", re.I)
CLEAR_PATTERN = re.compile(r"^(clear_at|清除(艾特|at)数据)$", re.I)
CLEAR_ALL_PATTERN = re.compile(r"^(clear_all|清除全部(艾特|at)数据)$", re.I)
CONTEXT_ON_PATTERN = re.compile(r"^(开启|打开)(艾特|at)上下文$", re.I)
CONTEXT_OFF_PATTERN = re.compile(r"^关闭(艾特|at)上下文$", re.I)
REMINDER_GROUP_ON_PATTERN = re.compile(r"^(开启|启用)(本群|群)(艾特|at)提醒$", re.I)
REMINDER_GROUP_OFF_PATTERN = re.compile(r"^关闭(本群|群)(艾特|at)提醒$", re.I)
REMINDER_PERSONAL_ON_PATTERN = re.compile(r"^(开启我的(艾特|at)提醒|开启(艾特|at)提醒)$", re.I)
REMINDER_PERSONAL_OFF_PATTERN = re.compile(r"^(关闭我的(艾特|at)提醒|关闭(艾特|at)提醒)$", re.I)
REMINDER_STATUS_PATTERN = re.compile(r"^(我的)?(艾特|at)提醒状态$", re.I)
REMINDER_CONTEXT_ON_PATTERN = re.compile(r"^开启提醒上下文$", re.I)
REMINDER_CONTEXT_OFF_PATTERN = re.compile(r"^关闭提醒上下文$", re.I)
REMINDER_CONTEXT_SET_PATTERN = re.compile(r"^设置提醒上下文\s*(\d+)\s*[,，]\s*(\d+)$", re.I)
RANK_PATTERN = re.compile(
    r"^(?:艾特排行榜?|谁最(?:常|爱)(?:艾特|@|at)我|(?:艾特|at)排[名行]|at_rank)"
    r"(?:\s*(\d{1,3})\s*天?)?$",
    re.I,
)
STORAGE_PATTERN = re.compile(r"^((艾特|at)(存储|占用|空间)(状态|情况)?|at_usage|at_storage)$", re.I)
CLEANUP_PATTERN = re.compile(r"^((艾特|at)清理|立即清理(艾特|at)数据|at_cleanup)$", re.I)
# 「艾特回顾」：久不看群时补课用——最近几次艾特 + 每次艾特前后的群聊上下文。
RECAP_PATTERN = re.compile(
    r"^(?:(?:艾特|at)回顾|回顾(?:艾特|at)|(?:艾特|at)补课|at_recap|catch_?up)"
    r"(?:\s*(\d{1,2})\s*(?:次|条)?)?$",
    re.I,
)

ALL_TARGET = "__all__"
INDEX_KEY = "records:index"
CONTEXT_INDEX_KEY = "context:index"
REMINDER_PENDING_INDEX_KEY = "reminder:pending:index"
# 自动维护识别自身键的白名单：不在这里的键会被视为历史遗留的孤儿键。
KNOWN_KV_KEYS = frozenset({INDEX_KEY, CONTEXT_INDEX_KEY, REMINDER_PENDING_INDEX_KEY})
KNOWN_KV_PREFIXES = ("records:", "context:", "reminder:", "member:", "maintenance:")

MAX_RECORDS_PER_TARGET = 300
# 每个会话保留「原图文件」的最近记录条数。0 = 不落盘（只留还能重取的链接）。
# 上游默认 0，但同时把 base64 原文写进数据库，等于把图片存在最贵的地方；
# 这里改成落盘 20 条，数据库只留引用。
RECENT_IMAGE_CACHE_RECORDS = 20
IMAGE_CACHE_RETENTION_HOURS = 24
MAX_CONTEXT_MESSAGES = 5
MAX_MESSAGES_PER_IMAGE = 12
RENDER_IMAGE_QUALITY = 92
RENDER_TIMEOUT_MS = 20000
RENDER_TASK_TIMEOUT_SEC = 25

# ---- 图片渲染引擎 ----
# 引擎展示名，用于日志与回退提示。
RENDER_ENGINE_LABELS = {"t2i": "t2i 服务", "browser": "本地浏览器"}
# t2i 页面渲染超时下限（毫秒），太小的话服务端来不及出图。
T2I_MIN_PAGE_TIMEOUT_MS = 10000
# 除页面渲染外，额外留给请求往返与图片下载的秒数。
T2I_DOWNLOAD_GRACE_SECONDS = 8
# t2i 单次出图的总超时上限（秒），防止端点"假活"时长时间卡住查询。
T2I_MAX_TASK_TIMEOUT_SEC = 45
# t2i 失败后的冷却秒数；冷却期内直接先走本地浏览器，不再白等一次超时。
T2I_FAILURE_COOLDOWN_SECONDS = 180
# 多页查询图走 t2i 时的并发上限（t2i 是网络调用，适度并发能明显加速）。
T2I_PAGE_CONCURRENCY = 3
# 出图时"顺手清理"的最小间隔（秒），避免每次渲染都全量扫描目录。
RENDER_CLEANUP_MIN_INTERVAL_SECONDS = 300
REMINDER_AWAY_SECONDS = 10 * 60
MAX_PENDING_REMINDERS = 50
MAX_REMINDER_CONTEXT = 5

# ---- 自动清理 / 存储治理 ----
MAINTENANCE_INTERVAL_HOURS = 6
MAINTENANCE_FIRST_RUN_DELAY_SECONDS = 60
RECORD_RETENTION_DAYS = 30
MEMBER_CACHE_RETENTION_DAYS = 14
CLEANUP_RENDER_HOURS = 24
RENDERS_MAX_MB = 64
IMAGES_MAX_MB = 128
TOTAL_QUOTA_MB = 256
ORPHAN_IMAGE_GRACE_MINUTES = 30
RENDER_GRACE_MINUTES = 10
RENDER_FILE_PREFIX = "mention_echo_"
LEGACY_RENDER_FILE_PREFIXES = ("who_at_me_",)
RENDER_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
MANAGED_DATA_SUBDIRS = ("renders", "message_images")
RANK_TOP_N = 10
# 艾特回顾默认看最近 3 次艾特，最多 5 次（再多就该直接翻群了）。
RECAP_DEFAULT_COUNT = 3
RECAP_MAX_COUNT = 5
# 供 LLM 调用的函数名；关闭配置项时会在 initialize() 里停用同名工具。
LLM_TOOL_NAME = "mention_echo_recent_mentions"
LLM_TOOL_MAX_RESULTS = 20
# 单轮维护最多删除的 KV 键数量，避免一次巡检长时间占用写锁。
MAINTENANCE_MAX_KV_DELETES = 2000
# 运行时内存字典的软上限，超过后在维护时回收空闲项。
MAINTENANCE_LOCK_CACHE_LIMIT = 512
MAINTENANCE_ACTIVITY_LIMIT = 4096

LEGACY_HEADER_IMAGE_URL = "https://pic1.imgdb.cn/item/69e60edc1d6508f56becb8fa.png"
LEGACY_FOOTER_IMAGE_URL = "https://pic1.imgdb.cn/item/69e5f9e51d6508f56bec8ea5.png"
HEADER_IMAGE_URL = ""
FOOTER_IMAGE_URL = ""
DEFAULT_HEADER_IMAGE_FILE = "assets/default_header.png"
DEFAULT_FOOTER_IMAGE_FILE = "assets/default_footer.png"
IMAGE_KINDS = {"header": "顶部图片", "footer": "底部图片"}
IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
REFERENCE_SEGMENT_TYPES = {"reply", "quote", "source", "reference"}
POKE_SEGMENT_TYPES = {"poke", "nudge", "touch", "pat"}
POKE_ACTION_TOKENS = ("戳了戳", "拍了拍", "摸了摸", "揉了揉", "亲了亲", "贴了贴", "碰了碰")
PAGE_SETTINGS_DEFAULTS = {
    "time_x": 30,
    "time_y": 7,
    "time_font_size": 16,
    "group_x": 56,
    "group_y": 45,
    "group_font_size": 22,
    "font_bold": False,
    "font_bold_strength": 0,
    "font_path": "",
    "header_image_path": "",
    "footer_image_path": "",
}