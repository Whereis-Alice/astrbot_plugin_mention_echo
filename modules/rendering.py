from __future__ import annotations

import asyncio
import base64
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
import astrbot.api.message_components as Comp

try:
    from .constants import *
except ImportError:
    from modules.constants import *


def _load_result_template() -> str:
    return (Path(__file__).resolve().parents[1] / "templates" / "result.html").read_text(encoding="utf-8")


def _load_help_template() -> str:
    return (Path(__file__).resolve().parents[1] / "templates" / "help.html").read_text(encoding="utf-8")


HTML_TEMPLATE = _load_result_template()
HELP_TEMPLATE = _load_help_template()

# 协议端只是没等到 NTQQ 的回执，消息大概率已经发出去了，不能当作发送失败去回退重发。
SEND_UNKNOWN_RETCODES = {1200}
SEND_UNKNOWN_TOKENS = ("invoke timeout", "send timeout", "sendmsg timeout", "timed out")


class RenderingMixin:
    # t2i 失败冷却的截止时间戳（0 表示健康）。放类级别，混入类无需改 __init__。
    _t2i_unhealthy_until: float = 0.0
    # 上次"顺手清理"渲染目录的时间戳。
    _last_render_cleanup_ts: float = 0.0

    async def _render_help_image(self, data: dict[str, Any]) -> str:
        prepared = self._prepare_render_data(data)
        return await self._render_via_engines(
            "帮助图",
            {
                "t2i": (
                    lambda: self._render_html_with_t2i(HELP_TEMPLATE, prepared),
                    self._t2i_task_timeout_sec(),
                ),
                "browser": (
                    lambda: self._render_html_with_browser(HELP_TEMPLATE, prepared),
                    float(self._render_task_timeout_sec()),
                ),
            },
        )

    async def _render_query_image(self, data: dict[str, Any]) -> str:
        prepared = self._prepare_render_data(data)
        return await self._render_via_engines(
            "查询图",
            {
                "t2i": (
                    lambda: self._render_html_with_t2i(HTML_TEMPLATE, prepared),
                    self._t2i_task_timeout_sec(),
                ),
                "browser": (
                    lambda: self._render_html_with_browser(HTML_TEMPLATE, prepared),
                    float(self._render_task_timeout_sec()),
                ),
            },
        )

    async def _render_query_images(self, items: list[dict[str, Any]]) -> list[str]:
        if not items:
            return []
        prepared = [self._prepare_render_data(item) for item in items]
        pages = len(prepared)
        return await self._render_via_engines(
            "查询图",
            {
                "t2i": (
                    lambda: self._render_html_pages_with_t2i(HTML_TEMPLATE, prepared),
                    self._t2i_task_timeout_sec() * pages,
                ),
                "browser": (
                    lambda: self._render_html_pages_with_browser(HTML_TEMPLATE, prepared),
                    float(self._render_task_timeout_sec()) * pages,
                ),
            },
        )

    def _prepare_render_data(self, data: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(data)
        prepared.setdefault("layout", self._render_layout())
        prepared.setdefault("custom_font_css", self._custom_font_css())
        return prepared

    # ---- 引擎调度 ----
    def _render_engine_order(self) -> tuple[str, ...]:
        """返回渲染引擎的尝试顺序。

        默认 t2i 优先：调用现成的 t2i 服务比每次本地拉起 Chromium 更快、更省内存。
        任一引擎失败会自动回退到下一个；t2i 刚失败过时，冷却期内直接先走浏览器，
        免得每条查询都白等一次超时。
        """
        mode = self._render_mode()
        if mode == "api":
            return ("t2i",)
        if mode == "browser":
            return ("browser",)
        if self._prefer_browser() or self._t2i_in_cooldown():
            return ("browser", "t2i")
        return ("t2i", "browser")

    async def _render_via_engines(self, label: str, plans: dict[str, tuple[Any, float]]) -> Any:
        """按 :meth:`_render_engine_order` 顺序尝试各引擎，只有最后一个的异常会抛出。

        ``plans`` 的键是引擎名，值是 ``(无参协程工厂, 该引擎的超时秒数)``。
        """
        order = [name for name in self._render_engine_order() if name in plans]
        if not order:
            raise RuntimeError(f"{label}没有可用的渲染引擎")
        for index, name in enumerate(order):
            factory, raw_timeout = plans[name]
            timeout = max(3.0, float(raw_timeout))
            try:
                result = await asyncio.wait_for(factory(), timeout=timeout)
            except Exception as exc:
                if name == "t2i":
                    self._mark_t2i_failure()
                if index == len(order) - 1:
                    raise
                current = RENDER_ENGINE_LABELS.get(name, name)
                following = RENDER_ENGINE_LABELS.get(order[index + 1], order[index + 1])
                if isinstance(exc, TimeoutError):
                    logger.warning(f"{LOG_TAG} {label}用{current}渲染超时（{timeout:.0f} 秒），回退到{following}")
                else:
                    logger.warning(
                        f"{LOG_TAG} {label}用{current}渲染失败，回退到{following}: {type(exc).__name__}: {exc}"
                    )
                continue
            if name == "t2i":
                self._mark_t2i_success()
            return result
        raise RuntimeError(f"{label}的所有渲染引擎都失败了")

    def _t2i_in_cooldown(self) -> bool:
        if self._t2i_unhealthy_until <= 0.0:
            return False
        if time.time() >= self._t2i_unhealthy_until:
            self._t2i_unhealthy_until = 0.0
            return False
        return True

    def _mark_t2i_failure(self) -> None:
        cooldown = self._t2i_cooldown_sec()
        if cooldown <= 0:
            return
        already_cooling = self._t2i_in_cooldown()
        self._t2i_unhealthy_until = time.time() + cooldown
        if not already_cooling:
            logger.warning(f"{LOG_TAG} t2i 渲染不可用，接下来 {cooldown} 秒改用本地浏览器渲染")

    def _mark_t2i_success(self) -> None:
        if self._t2i_unhealthy_until > 0.0:
            self._t2i_unhealthy_until = 0.0
            logger.info(f"{LOG_TAG} t2i 渲染已恢复")

    def _send_outcome_unknown(self, exc: BaseException) -> bool:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return True
        retcode = getattr(exc, "retcode", None)
        try:
            if retcode is not None and int(retcode) in SEND_UNKNOWN_RETCODES:
                return True
        except (TypeError, ValueError):
            pass
        text = " ".join(str(getattr(exc, attr, "") or "") for attr in ("message", "wording"))
        text = f"{text} {exc}".lower()
        return any(token in text for token in SEND_UNKNOWN_TOKENS)

    def _send_error_text(self, exc: BaseException, limit: int = 200) -> str:
        text = str(exc).replace("\n", " ").replace("\r", " ")
        return text if len(text) <= limit else text[:limit] + "..."

    def _assume_sent_on_timeout(self, exc: BaseException, action: str) -> bool:
        if not self._send_outcome_unknown(exc):
            return False
        logger.warning(f"[艾特回声] {action}未收到协议端回执，按已送达处理，不再回退: {self._send_error_text(exc)}")
        return True

    async def _try_send(self, event: AstrMessageEvent, result: Any) -> bool:
        try:
            await event.send(result)
            return True
        except Exception as exc:
            if self._assume_sent_on_timeout(exc, "主动发送"):
                return True
            logger.error(f"[艾特回声] 主动发送失败: {exc}")
            return False

    async def _try_send_images(self, event: AstrMessageEvent, image_paths: list[str]) -> bool:
        if not image_paths:
            return False
        if len(image_paths) == 1:
            return await self._try_send(event, event.image_result(image_paths[0]))
        if await self._try_send_forward_images(event, "", image_paths):
            return True
        try:
            await event.send(event.chain_result([self._image_component(path) for path in image_paths]))
            return True
        except Exception as exc:
            if self._assume_sent_on_timeout(exc, "普通合并发送图片"):
                return True
            logger.warning(f"[艾特回声] 普通合并发送图片失败，回退到分开发送: {exc}")
            return False

    async def _try_send_text_images(self, event: AstrMessageEvent, text: str, image_paths: list[str]) -> bool:
        if not image_paths:
            return False
        if len(image_paths) > 1 and await self._try_send_forward_images(event, text, image_paths):
            return True
        try:
            components = [Comp.Plain(text)]
            components.extend(self._image_component(path) for path in image_paths)
            await event.send(event.chain_result(components))
            return True
        except Exception as exc:
            if self._assume_sent_on_timeout(exc, "合并发送提醒"):
                return True
            logger.warning(f"[艾特回声] 合并发送提醒失败，回退到分开发送: {exc}")
            return False

    async def _try_send_forward_images(self, event: AstrMessageEvent, text: str, image_paths: list[str]) -> bool:
        group_id = self._group_id(event)
        if not group_id or len(image_paths) <= 1:
            return False

        self_id = self._self_id(event) or "10000"
        bot_name = await self._bot_name(event, group_id)
        uin = self._numeric_id(self_id)
        nodes = []
        if text.strip():
            nodes.append(
                {
                    "type": "node",
                    "data": {
                        "name": bot_name,
                        "uin": uin,
                        "content": [{"type": "text", "data": {"text": text}}],
                    },
                }
            )

        for image_path in image_paths:
            nodes.append(
                {
                    "type": "node",
                    "data": {
                        "name": bot_name,
                        "uin": uin,
                        "content": [{"type": "image", "data": {"file": self._onebot_image_file(image_path)}}],
                    },
                }
            )

        sent = await self._try_onebot_action(
            event,
            "send_group_forward_msg",
            group_id=self._numeric_id(group_id),
            messages=nodes,
        )
        if sent:
            return True

        logger.warning("[艾特回声] 合并转发发送失败，回退到普通图片发送")
        return False

    async def _try_onebot_action(self, event: AstrMessageEvent, action: str, **kwargs: Any) -> bool:
        bot = getattr(event, "bot", None)
        caller = getattr(bot, "call_action", None)
        if not callable(caller):
            return False

        self_id = self._self_id(event)
        if self_id and "self_id" not in kwargs:
            kwargs["self_id"] = self_id

        try:
            await caller(action, **kwargs)
            return True
        except TypeError:
            kwargs.pop("self_id", None)
            try:
                await caller(action, **kwargs)
                return True
            except Exception as exc:
                if self._assume_sent_on_timeout(exc, f"调用协议端 API {action}"):
                    return True
                logger.debug(f"[艾特回声] 调用协议端 API {action} 失败: {exc}")
        except Exception as exc:
            if self._assume_sent_on_timeout(exc, f"调用协议端 API {action}"):
                return True
            logger.debug(f"[艾特回声] 调用协议端 API {action} 失败: {exc}")
        return False

    def _image_component(self, image_path: str) -> Any:
        image_path = str(image_path)
        if re.match(r"^https?://", image_path, re.I):
            return Comp.Image.fromURL(image_path)
        return Comp.Image.fromFileSystem(image_path)

    def _onebot_image_file(self, image_path: str) -> str:
        image_path = str(image_path)
        if re.match(r"^https?://", image_path, re.I):
            return image_path
        try:
            return "base64://" + base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        except Exception:
            try:
                return Path(image_path).resolve().as_uri()
            except Exception:
                return image_path

    async def _render_html_pages_with_browser(
        self,
        template: str,
        items: list[dict[str, Any]],
    ) -> list[str]:
        from jinja2 import Environment
        from playwright.async_api import async_playwright

        self._cleanup_old_renders()
        renderer = Environment(autoescape=True).from_string(template)
        output_paths = []
        browser = None
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    args=["--disable-dev-shm-usage", "--disable-gpu", "--no-sandbox"]
                )
                for data in items:
                    page = await browser.new_page(
                        viewport={"width": 600, "height": 800},
                        device_scale_factor=2,
                    )
                    output_path = self._new_render_path()
                    try:
                        await page.set_content(
                            renderer.render(**data),
                            wait_until="domcontentloaded",
                            timeout=self._render_page_timeout_ms(),
                        )
                        await self._wait_for_browser_assets(page)
                        element = await page.query_selector(".app")
                        if element:
                            await element.screenshot(
                                path=str(output_path),
                                type="jpeg",
                                quality=self._render_quality(),
                            )
                        else:
                            await page.screenshot(
                                path=str(output_path),
                                type="jpeg",
                                quality=self._render_quality(),
                                full_page=True,
                            )
                        output_paths.append(str(output_path))
                    finally:
                        await page.close()
            finally:
                if browser:
                    await browser.close()
        return output_paths

    async def _render_html_with_browser(self, template: str, data: dict[str, Any]) -> str:
        from jinja2 import Environment
        from playwright.async_api import async_playwright

        self._cleanup_old_renders()
        html_text = Environment(autoescape=True).from_string(template).render(**data)
        output_path = self._new_render_path()
        browser = None
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    args=["--disable-dev-shm-usage", "--disable-gpu", "--no-sandbox"]
                )
                page = await browser.new_page(
                    viewport={"width": 600, "height": 800},
                    device_scale_factor=2,
                )
                await page.set_content(
                    html_text,
                    wait_until="domcontentloaded",
                    timeout=self._render_page_timeout_ms(),
                )
                await self._wait_for_browser_assets(page)
                element = await page.query_selector(".app")
                if element:
                    await element.screenshot(
                        path=str(output_path),
                        type="jpeg",
                        quality=self._render_quality(),
                    )
                else:
                    await page.screenshot(
                        path=str(output_path),
                        type="jpeg",
                        quality=self._render_quality(),
                        full_page=True,
                    )
            finally:
                if browser:
                    await browser.close()
        return str(output_path)

    async def _render_html_pages_with_t2i(self, template: str, items: list[dict[str, Any]]) -> list[str]:
        """多页查询图走 t2i：网络调用适度并发，比一页页串行快得多。"""
        if len(items) <= 1:
            return [await self._render_html_with_t2i(template, data) for data in items]

        limit = asyncio.Semaphore(max(1, T2I_PAGE_CONCURRENCY))

        async def render_one(data: dict[str, Any]) -> str:
            async with limit:
                return await self._render_html_with_t2i(template, data)

        return list(await asyncio.gather(*(render_one(data) for data in items)))

    async def _render_html_with_t2i(self, template: str, data: dict[str, Any]) -> str:
        from jinja2 import Environment

        self._cleanup_old_renders()
        html_text = Environment(autoescape=True).from_string(template).render(**data)

        last_error: Exception | None = None
        for options in self._t2i_render_options():
            image_type = str(options.get("type") or "")
            try:
                image_data = await asyncio.wait_for(
                    self.html_render(html_text, {}, False, options),
                    timeout=self._t2i_attempt_timeout_sec(options),
                )
            except Exception as exc:
                last_error = exc
                logger.warning(f"{LOG_TAG} t2i 渲染失败（{image_type or 'default'}）: {type(exc).__name__}: {exc}")
                continue

            image_path = self._store_t2i_render_result(image_data, image_type)
            if image_path:
                return image_path
            logger.warning(f"{LOG_TAG} t2i 返回了无效图片数据，尝试下一策略: {options}")

        if last_error:
            raise last_error
        raise RuntimeError("t2i 渲染没有返回有效图片")

    def _t2i_render_options(self) -> list[dict[str, Any]]:
        """t2i 出图策略，按顺序降级。

        JPEG 放在前面：体积比 PNG 小一个量级，出图也更快，
        对"结果图"这种截图型内容画质完全够用，同时明显减少渲染缓存占用。
        """
        page_timeout = max(T2I_MIN_PAGE_TIMEOUT_MS, self._render_page_timeout_ms())
        return [
            {
                "full_page": True,
                "type": "jpeg",
                "quality": min(self._render_quality(), 90),
                "device_scale_factor_level": "high",
                "timeout": page_timeout,
            },
            {
                "full_page": True,
                "type": "png",
                "device_scale_factor_level": "ultra",
                "timeout": page_timeout,
            },
        ]

    def _t2i_attempt_timeout_sec(self, options: dict[str, Any]) -> float:
        """单次 t2i 尝试的本地超时 = 服务端页面超时 + 网络往返余量。"""
        page_seconds = float(options.get("timeout") or 0.0) / 1000.0
        return max(5.0, page_seconds + T2I_DOWNLOAD_GRACE_SECONDS)

    def _t2i_task_timeout_sec(self) -> float:
        """t2i 引擎的整体超时：要装得下所有降级策略，同时有个硬上限。"""
        budget = sum(self._t2i_attempt_timeout_sec(item) for item in self._t2i_render_options())
        floor = float(self._render_task_timeout_sec())
        return min(float(T2I_MAX_TASK_TIMEOUT_SEC), max(floor, budget + 2.0))

    def _store_t2i_render_result(self, image_data: Any, image_type: str = "") -> str | None:
        if isinstance(image_data, bytes | bytearray):
            return self._store_t2i_image_bytes(bytes(image_data), image_type)

        text = str(image_data or "").strip()
        if not text:
            return None
        if text.startswith("base64://"):
            try:
                return self._store_t2i_image_bytes(base64.b64decode(text[len("base64://") :]), image_type)
            except Exception as exc:
                logger.warning(f"[艾特回声] 解析 t2i base64 图片失败: {type(exc).__name__}: {exc}")
                return None
        if text.lower().startswith("data:image/"):
            try:
                header, payload = text.split(",", 1)
                source_type = header.split(";", 1)[0].rsplit("/", 1)[-1]
                return self._store_t2i_image_bytes(base64.b64decode(payload), source_type)
            except Exception as exc:
                logger.warning(f"[艾特回声] 解析 t2i data-uri 图片失败: {type(exc).__name__}: {exc}")
                return None
        if text.startswith("<") or "<html" in text[:200].lower():
            return None
        if re.match(r"^https?://", text, re.I):
            return text
        return self._store_t2i_file_result(text)

    def _store_t2i_image_bytes(self, data: bytes, image_type: str = "") -> str | None:
        suffix = self._t2i_image_suffix(data, image_type)
        if not suffix:
            return None

        output = self._new_render_path(suffix)
        try:
            output.write_bytes(data)
            self._trim_t2i_blank_margin(output)
            return str(output)
        except Exception as exc:
            logger.warning(f"[艾特回声] 保存 t2i 图片失败: {type(exc).__name__}: {exc}")
            return None

    def _store_t2i_file_result(self, value: str) -> str | None:
        try:
            path = Path(value)
            if not path.exists() or not path.is_file():
                return None
            suffix = self._t2i_image_suffix(path.read_bytes()[:16], path.suffix)
            if not suffix:
                logger.warning(f"{LOG_TAG} t2i 返回了非图片文件，已忽略: {path}")
                return None
            path = self._adopt_render_file(path, suffix)
            self._trim_t2i_blank_margin(path)
            return str(path)
        except Exception as exc:
            logger.warning(f"{LOG_TAG} t2i 文件结果被拒绝: {type(exc).__name__}: {exc}")
            return None

    def _adopt_render_file(self, path: Path, suffix: str) -> Path:
        """把 t2i 落在 AstrBot 公共临时目录里的图片搬进插件自己的 renders 目录。

        这样它才会被本插件的保留时长、容量配额和 ``#艾特清理`` 管到，
        否则出图越多、AstrBot 的 temp 目录越大，而插件完全看不见这部分占用。
        搬移失败时退回原路径，不影响本次发送。
        """
        try:
            render_dir = self._render_dir()
            if path.parent.resolve() == render_dir.resolve():
                return path
            target = self._new_render_path(suffix)
            shutil.move(str(path), str(target))
            return target
        except Exception as exc:
            logger.debug(f"{LOG_TAG} t2i 图片搬移失败，沿用原路径: {type(exc).__name__}: {exc}")
            return path

    def _trim_t2i_blank_margin(self, path: Path) -> None:
        try:
            from PIL import Image

            with Image.open(path) as img:
                width, height = img.size
                if width <= 720 or height <= 0:
                    return
                work = img.convert("RGB")
                bg = work.getpixel((width - 1, min(10, height - 1)))
                y_step = max(1, height // 600)
                x_step = 1 if width <= 1600 else 2
                right = width - 1
                for x in range(width - 1, -1, -x_step):
                    has_content = False
                    for y in range(0, height, y_step):
                        pixel = work.getpixel((x, y))
                        if sum(abs(pixel[i] - bg[i]) for i in range(3)) > 24:
                            has_content = True
                            break
                    if has_content:
                        right = min(width - 1, x + 28)
                        break
                if width - right < 80 or right < 320:
                    return
                cropped = img.crop((0, 0, right + 1, height))
                suffix = path.suffix.lower()
                if suffix in {".jpg", ".jpeg"}:
                    cropped.convert("RGB").save(path, format="JPEG", quality=max(90, self._render_quality()), optimize=True)
                elif suffix == ".webp":
                    cropped.save(path, format="WEBP", quality=max(90, self._render_quality()))
                else:
                    cropped.save(path)
        except Exception as exc:
            logger.debug(f"{LOG_TAG} t2i 图片留白裁剪跳过: {type(exc).__name__}: {exc}")

    def _t2i_image_suffix(self, data: bytes, image_type: str = "") -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8"):
            return ".jpg"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        image_type = image_type.lower().strip().lstrip(".")
        if image_type in {"png", "jpg", "jpeg", "webp"}:
            return ".jpg" if image_type == "jpeg" else f".{image_type}"
        return ""

    async def _wait_for_browser_assets(self, page: Any) -> None:
        asset_timeout = min(10000, max(1000, self._render_page_timeout_ms() // 2))
        try:
            await page.evaluate(
                """
                async (assetTimeout) => {
                  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                  if (document.fonts && document.fonts.ready) {
                    await Promise.race([document.fonts.ready.catch(() => {}), delay(Math.min(assetTimeout, 1500))]);
                  }

                  const images = Array.from(document.images || []);
                  await Promise.race([
                    Promise.all(images.map((img) => new Promise((resolve) => {
                      if (img.complete) {
                        resolve();
                        return;
                      }
                      const done = () => resolve();
                      img.addEventListener("load", done, { once: true });
                      img.addEventListener("error", done, { once: true });
                    }))),
                    delay(assetTimeout),
                  ]);

                  for (const img of images) {
                    const optional = img.classList.contains("msg-img") || img.classList.contains("quote-img");
                    if (optional && (!img.complete || img.naturalWidth === 0)) {
                      img.remove();
                    }
                  }

                }
                """,
                asset_timeout,
            )
            await page.wait_for_timeout(300)
        except Exception as exc:
            logger.debug(f"[艾特回声] 等待浏览器资源加载失败，继续截图: {type(exc).__name__}: {exc}")

    def _new_render_path(self, suffix: str = ".jpg") -> Path:
        render_dir = self._render_dir()
        render_dir.mkdir(parents=True, exist_ok=True)
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        return render_dir / f"{RENDER_FILE_PREFIX}{int(time.time())}_{uuid.uuid4().hex}{suffix}"

    def _render_dir(self) -> Path:
        plugin_dir = getattr(self, "_plugin_data_dir", None)
        if callable(plugin_dir):
            return Path(plugin_dir()) / "renders"
        try:
            from astrbot.api.star import StarTools

            return Path(StarTools.get_data_dir(PLUGIN_NAME)) / "renders"
        except Exception:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / "renders"
            except Exception:
                return Path.cwd() / "data" / PLUGIN_NAME / "renders"

    def _cleanup_old_renders(self) -> None:
        """渲染时的机会性清理；总量/配额兜底由 MaintenanceMixin 负责。

        带节流：目录扫描是同步 IO，没必要每次出图都跑一遍。
        """
        now = time.time()
        if now - self._last_render_cleanup_ts < RENDER_CLEANUP_MIN_INTERVAL_SECONDS:
            return
        self._last_render_cleanup_ts = now
        render_dir = self._render_dir()
        if not render_dir.exists():
            return
        expire_before = time.time() - self._cleanup_render_hours() * 60 * 60
        prefixes = (RENDER_FILE_PREFIX, *LEGACY_RENDER_FILE_PREFIXES)
        for path in render_dir.iterdir():
            try:
                if not path.is_file() or not path.name.startswith(prefixes):
                    continue
                if path.suffix.lower() not in RENDER_IMAGE_SUFFIXES:
                    continue
                if path.stat().st_mtime < expire_before:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
