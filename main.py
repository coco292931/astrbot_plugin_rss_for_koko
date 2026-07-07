from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import socket
import time
import traceback
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dateutil import parser as date_parser
from lxml import etree

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.agent.message import AssistantMessageSegment, TextPart, ThinkPart, UserMessageSegment
except Exception:  # pragma: no cover - older AstrBot fallback
    AssistantMessageSegment = None
    TextPart = None
    ThinkPart = None
    UserMessageSegment = None

try:
    from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform import PlatformStatus
except Exception:  # pragma: no cover - optional precise platform sending
    AstrBotMessage = None
    Group = None
    MessageMember = None
    MessageType = None
    PlatformStatus = None

try:
    from astrbot.core.platform.astr_message_event import MessageSession as MS
except ImportError:  # pragma: no cover - compatibility with older AstrBot
    try:
        from astrbot.core.platform.message_session import MessageSession as MS
    except Exception:
        MS = None

try:
    from astrbot.core.platform.sources.webchat.message_parts_helper import message_chain_to_storage_message_parts
except Exception:  # pragma: no cover - optional history persistence helper
    message_chain_to_storage_message_parts = None

from .data_handler import DataHandler
from .pic_handler import RssImageHandler
from .rss import RSSItem


@register(
    "astrbot_plugin_rss_for_koko",
    "Soulter / coco",
    "面向 LLM 的 RSS 订阅插件",
    "1.2.0",
    "https://github.com/coco292931/astrbot_plugin_rss",
)
class RssPlugin(Star):
    """RSS 订阅插件：以 LLM 为主、用户为辅处理订阅更新。"""

    USER_AGENT = "AstrBot-RSS-LLM/1.2 (+https://github.com/coco292931/astrbot_plugin_rss)"
    IMAGE_CAPTION_PROMPT = "请用中文简洁转述这张 RSS 内容中的 {image_type}。只描述图片实际内容，不要输出图片占位符。"
    IMAGE_CAPTION_PROMPT = '' #故意的

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)

        self.logger = logging.getLogger("astrbot")
        self.context = context
        self.config = config
        self.data_handler = DataHandler()

        self.title_max_length = self._cfg_int("title_max_length", 80)
        self.description_max_length = self._cfg_int("description_max_length", 800)
        self.max_items_per_poll = self._cfg_int("max_items_per_poll", 5)
        self.hard_max_items_per_fetch = self._cfg_int("hard_max_items_per_fetch", 50)
        self.max_item_chars = self._cfg_int("max_item_chars", 1200)
        self.max_total_chars = self._cfg_int("max_total_chars", 8000)
        self.history_seen_limit = self._cfg_int("history_seen_limit", 500)
        self.default_interval_minutes = self._cfg_int("default_interval_minutes", 60)
        self.max_response_bytes = self._cfg_int("max_response_bytes", 2 * 1024 * 1024)

        self.llm_mode_enabled = bool(config.get("llm_mode_enabled", True))
        self.llm_prompt_template = config.get("llm_prompt_template") or ""
        self.image_caption_prompt = (
            str(config.get("image_caption_prompt", "") or "").strip()
            or self.IMAGE_CAPTION_PROMPT
        )
        self.preserve_reasoning_in_history = bool(config.get("preserve_reasoning_in_history", True))
        self.send_user_fallback_on_llm_error = bool(config.get("send_user_fallback_on_llm_error", True))

        self.proxy_url = (config.get("proxy_url") or "").strip()
        self.trust_env = bool(config.get("trust_env", True))
        self.proxy_timeout_fallback = bool(config.get("proxy_timeout_fallback", True))
        self.allow_private_network = bool(config.get("allow_private_network", False))

        runtime_settings = self.data_handler.data.get("settings", {})
        if isinstance(runtime_settings, dict):
            self.proxy_url = str(runtime_settings.get("proxy_url", self.proxy_url) or "").strip()
            for attr in (
                "default_interval_minutes",
                "max_items_per_poll",
                "max_item_chars",
                "max_total_chars",
            ):
                if runtime_settings.get(attr):
                    try:
                        setattr(self, attr, int(runtime_settings[attr]))
                    except (TypeError, ValueError):
                        pass

        self.t2i = bool(config.get("t2i", False))
        self.is_hide_url = bool(config.get("is_hide_url", False))
        pic_config = config.get("pic_config") or {}
        self.is_read_pic = bool(pic_config.get("is_read_pic", False))
        self.is_adjust_pic = bool(pic_config.get("is_adjust_pic", False))
        self.max_pic_item = int(pic_config.get("max_pic_item", 3) or 3)
        self.is_compose = bool(config.get("compose", True))
        self.pic_handler = RssImageHandler(self.is_adjust_pic)

        self.scheduler = AsyncIOScheduler()
        self.scheduler.start()
        self._migrate_existing_data()
        self._fresh_asyncIOScheduler()

    async def terminate(self) -> None:
        """插件卸载时关闭 RSS 调度器。"""
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _cfg_int(self, key: str, default: int) -> int:
        """安全读取整数配置。"""
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _migrate_existing_data(self) -> None:
        """将旧版 cron 订阅迁移为 interval 订阅结构。"""
        changed = False
        for url, info in list(self.data_handler.data.items()):
            if url in self.data_handler.RESERVED_KEYS or not isinstance(info, dict):
                continue
            info.setdefault("info", {"title": "未知频道", "description": ""})
            info.setdefault("state", {})
            info.setdefault("subscribers", {})
            state = info["state"]
            state.setdefault("seen_ids", [])
            state.setdefault("latest_link", "")
            state.setdefault("last_update", 0)

            for user, sub_info in list(info.get("subscribers", {}).items()):
                if not isinstance(sub_info, dict):
                    info["subscribers"][user] = self._new_subscription_payload(self.default_interval_minutes)
                    changed = True
                    continue
                if "interval_minutes" not in sub_info:
                    sub_info["interval_minutes"] = self._interval_from_legacy_cron(sub_info.get("cron_expr", ""))
                    changed = True
                sub_info.setdefault("last_update", int(sub_info.get("last_update", 0) or 0))
                sub_info.setdefault("latest_link", sub_info.get("latest_link", ""))
                sub_info.setdefault("seen_ids", [])
                sub_info.setdefault("enabled", True)
                sub_info.setdefault("subscriber_kind", "user")
                sub_info.setdefault("created_at", int(time.time()))
        if changed:
            self.data_handler.save_data()

    def _new_subscription_payload(
        self,
        interval_minutes: int | None = None,
        subscriber_kind: str = "user",
    ) -> dict:
        """创建订阅者状态。"""
        interval = self._normalize_interval_minutes(interval_minutes)
        return {
            "interval_minutes": interval,
            "last_update": 0,
            "latest_link": "",
            "seen_ids": [],
            "enabled": True,
            "subscriber_kind": subscriber_kind,
            "created_at": int(time.time()),
        }

    def _normalize_interval_minutes(self, interval_minutes: int | None) -> int:
        """规范化订阅拉取间隔，避免负数或异常大值。"""
        try:
            interval = int(interval_minutes) if interval_minutes is not None else 0
        except (TypeError, ValueError):
            interval = 0
        if interval <= 0:
            interval = int(self.default_interval_minutes or 60)
        return max(1, min(interval, 7 * 24 * 60))

    def _interval_from_legacy_cron(self, cron_expr: str) -> int:
        """尽量从旧 cron 表达式推断分钟间隔。"""
        fields = (cron_expr or "").split()
        if len(fields) != 5:
            return max(1, self.default_interval_minutes)
        minute, hour, day, month, day_of_week = fields
        if hour == day == month == day_of_week == "*":
            if minute.startswith("*/") or minute.startswith("0/"):
                try:
                    return max(1, int(minute.split("/", 1)[1]))
                except (TypeError, ValueError):
                    return max(1, self.default_interval_minutes)
            if minute == "*":
                return 1
        if day == month == day_of_week == "*" and minute.isdigit():
            if hour.startswith("*/") or hour.startswith("0/"):
                try:
                    return max(1, int(hour.split("/", 1)[1]) * 60)
                except (TypeError, ValueError):
                    pass
            if hour.isdigit():
                return 24 * 60
        return max(1, self.default_interval_minutes)

    def _normalize_url(self, url: str) -> str:
        """规范化并验证订阅 URL。"""
        normalized = (url or "").strip()
        if not normalized:
            raise ValueError("URL 不能为空")
        if not re.match(r"^https?://", normalized, re.I):
            normalized = "https://" + normalized.lstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("仅支持 http/https Feed URL")
        return normalized

    def _is_url_allowed(self, url: str) -> bool:
        """基础 SSRF 防护，默认禁止 localhost、内网和 metadata 地址。"""
        if self.allow_private_network:
            return True
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return False
        lowered = host.lower()
        if lowered in {"localhost", "0.0.0.0"} or lowered.endswith(".localhost"):
            return False
        try:
            ip = ipaddress.ip_address(lowered)
            return not self._is_private_ip(ip)
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            self.logger.warning(f"rss: 无法解析 {host}，出于安全考虑拒绝访问")
            return False
        for info in infos:
            ip_text = info[4][0]
            try:
                if self._is_private_ip(ipaddress.ip_address(ip_text)):
                    return False
            except ValueError:
                continue
        return True

    def _is_private_ip(self, ip: ipaddress._BaseAddress) -> bool:
        """判断 IP 是否属于不应由默认 RSS 抓取访问的地址段。"""
        return bool(
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
            or str(ip) == "169.254.169.254"
        )

    async def parse_channel_info(self, url: str) -> bytes | None:
        """拉取 Feed 内容；配置代理时优先代理，代理超时后可回退直连。"""
        try:
            url = self._normalize_url(url)
        except ValueError as e:
            self.logger.error(f"rss: URL 无效 {url}: {e}")
            return None
        if not self._is_url_allowed(url):
            self.logger.error(f"rss: 已阻止访问内网或本地地址 {url}")
            return None

        if self.proxy_url:
            text = await self._fetch_url(url, proxy=self.proxy_url, trust_env=False)
            if text is not None:
                return text
            if not self.proxy_timeout_fallback:
                return None
            self.logger.warning(f"rss: 代理获取失败，尝试直连 {url}")
        return await self._fetch_url(url, proxy=None, trust_env=self.trust_env)

    async def _fetch_url(self, url: str, *, proxy: str | None, trust_env: bool) -> bytes | None:
        """执行一次 HTTP 获取并限制响应体大小。"""
        headers = {"User-Agent": self.USER_AGENT}
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, trust_env=trust_env) as session:
                async with session.get(url, proxy=proxy, allow_redirects=True, max_redirects=5) as resp:
                    if resp.status != 200:
                        self.logger.error(f"rss: 无法正常打开站点 {url}，状态码 {resp.status}")
                        return None
                    chunks = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(32768):
                        total += len(chunk)
                        if total > self.max_response_bytes:
                            self.logger.error(f"rss: 响应体过大，已停止读取 {url}")
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks)
        except asyncio.TimeoutError:
            self.logger.error(f"rss: 请求站点 {url} 超时")
            return None
        except aiohttp.ClientError as e:
            self.logger.error(f"rss: 请求站点 {url} 网络错误: {e}")
            return None
        except Exception as e:
            self.logger.error(f"rss: 请求站点 {url} 发生未知错误: {e}")
            return None

    async def poll_rss(
        self,
        url: str,
        num: int = -1,
        after_timestamp: int = 0,
        after_link: str = "",
        seen_ids: list[str] | None = None,
        only_new: bool = True,
    ) -> list[RSSItem]:
        """从站点拉取并标准化 RSS/Atom/JSON Feed。"""
        text = await self.parse_channel_info(url)
        if text is None:
            self.logger.error(f"rss: 无法解析站点 {url} 的 RSS 信息")
            return []
        limit = self._normalize_limit(num)
        try:
            if self._looks_like_json(text):
                items = self._parse_json_feed(text, url)
            else:
                items = self._parse_xml_feed(text, url)
        except Exception as e:
            self.logger.error(f"rss: 解析 Feed 失败 {url}: {e}")
            self.logger.debug(traceback.format_exc())
            return []

        seen_set = set(seen_ids or [])
        filtered: list[RSSItem] = []
        for item in items:
            identity = item.identity()
            if only_new:
                if identity and identity in seen_set:
                    continue
                if after_timestamp and item.pubDate_timestamp and item.pubDate_timestamp <= after_timestamp:
                    continue
                if not item.pubDate_timestamp and after_link and item.link == after_link:
                    continue
            filtered.append(item)
            if limit != -1 and len(filtered) >= limit:
                break
        return filtered

    def _normalize_limit(self, limit: int) -> int:
        """将调用方 limit 约束在硬上限内。"""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = self.max_items_per_poll
        hard = max(1, int(self.hard_max_items_per_fetch or 50))
        if limit == -1:
            return hard
        return max(0, min(limit, hard))

    def _looks_like_json(self, text: bytes) -> bool:
        """粗略判断响应是否为 JSON Feed。"""
        return text.lstrip()[:1] in (b"{", b"[")

    def _parse_xml_feed(self, text: bytes, feed_url: str) -> list[RSSItem]:
        """解析 RSS/RDF/Atom XML。"""
        parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
        root = etree.fromstring(text, parser=parser)
        channel_title, _ = self.data_handler.parse_channel_text_info(text)
        nodes = root.xpath("//*[local-name()='item']")
        if not nodes:
            nodes = root.xpath("//*[local-name()='entry']")

        items = []
        for node in nodes:
            item = self._xml_node_to_item(node, feed_url, channel_title)
            if item:
                items.append(item)
        return items

    def _xml_node_to_item(self, node, feed_url: str, channel_title: str) -> RSSItem | None:
        """将 XML item/entry 节点转换为 RSSItem。"""
        title = self._node_text(node, ["title"]) or "无标题"
        link = self._node_link(node, feed_url)
        guid = self._node_text(node, ["guid", "id"])
        raw_description = self._node_text(node, ["description", "summary"])
        raw_content = self._node_text(node, ["encoded", "content"]) or raw_description
        description = self.data_handler.strip_html(raw_description or raw_content)
        content = self.data_handler.strip_html(raw_content or raw_description)
        pub_date = self._node_text(node, ["pubDate", "published", "updated", "date", "created"])
        pub_ts = self._parse_datetime_to_timestamp(pub_date)
        author = self._node_text(node, ["author", "creator", "name"])
        tags = [str(x).strip() for x in node.xpath("./*[local-name()='category']/text()") if str(x).strip()]
        pic_urls = self.data_handler.strip_html_pic(raw_content or raw_description, feed_url)
        pic_urls.extend(self._extract_image_links(node, feed_url))
        pic_urls = list(dict.fromkeys(pic_urls))
        media_urls = self.data_handler.strip_html_media(raw_content or raw_description, feed_url)
        media_urls.extend(self._extract_media_links(node, feed_url))
        content_hash = self._content_hash(title, link, content or description)
        if not link:
            link = guid or feed_url
        return RSSItem(
            channel_title,
            self._truncate_display(title, self.title_max_length),
            link,
            self._truncate_display(description, self.description_max_length),
            pub_date,
            pub_ts,
            pic_urls,
            guid=guid,
            author=author,
            content=content,
            tags=tags,
            media_urls=media_urls,
            image_captions=[],
            source_url=feed_url,
            content_hash=content_hash,
        )

    def _node_text(self, node, names: list[str]) -> str:
        """按 local-name 顺序提取节点文本。"""
        for name in names:
            values = node.xpath(f"./*[local-name()='{name}']")
            for value in values:
                if value is None:
                    continue
                text = "".join(value.itertext()).strip()
                if text:
                    return text
        return ""

    def _node_link(self, node, feed_url: str) -> str:
        """兼容 RSS link 文本和 Atom link href。"""
        text_link = self._node_text(node, ["link"])
        if text_link:
            return urljoin(feed_url, text_link)
        links = node.xpath("./*[local-name()='link']")
        for link_node in links:
            href = link_node.get("href")
            rel = (link_node.get("rel") or "alternate").lower()
            if href and rel in {"alternate", ""}:
                return urljoin(feed_url, href)
        for link_node in links:
            href = link_node.get("href")
            if href:
                return urljoin(feed_url, href)
        return ""

    def _extract_media_links(self, node, feed_url: str) -> list[str]:
        """提取 media/enclosure 等附件链接，用占位符交给 LLM。"""
        media_urls = []
        for media_node in node.xpath("./*[local-name()='enclosure' or local-name()='content' or local-name()='thumbnail']"):
            media_url = media_node.get("url") or media_node.get("href")
            if not media_url:
                continue
            media_type = (media_node.get("type") or "").lower()
            if media_type.startswith("image"):
                continue
            label = "音频" if media_type.startswith("audio") else "视频" if media_type.startswith("video") else "媒体"
            media_urls.append(f"[{label}] {urljoin(feed_url, media_url)}")
        return media_urls

    def _extract_image_links(self, node, feed_url: str) -> list[str]:
        """提取 enclosure/media:content/media:thumbnail 中的图片/GIF 链接。"""
        image_urls = []
        for media_node in node.xpath("./*[local-name()='enclosure' or local-name()='content' or local-name()='thumbnail']"):
            media_url = media_node.get("url") or media_node.get("href")
            if not media_url:
                continue
            media_type = (media_node.get("type") or "").lower()
            tag_name = etree.QName(media_node).localname.lower()
            is_image = (
                tag_name == "thumbnail"
                or media_type.startswith("image")
                or bool(re.search(r"\.(?:png|jpe?g|gif|webp)(?:$|[?#])", media_url, re.I))
            )
            if is_image:
                image_urls.append(urljoin(feed_url, media_url))
        return image_urls

    def _parse_json_feed(self, text: bytes, feed_url: str) -> list[RSSItem]:
        """解析 JSON Feed。"""
        payload = json.loads(text.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            self.logger.error(f"rss: JSON Feed 顶层不是对象，跳过 {feed_url}")
            return []
        channel_title = str(payload.get("title") or "未知频道")
        items = []
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("summary") or "无标题")
            link = urljoin(feed_url, str(item.get("url") or item.get("external_url") or ""))
            guid = str(item.get("id") or link or "")
            raw_content = str(item.get("content_html") or item.get("content_text") or item.get("summary") or "")
            description = self.data_handler.strip_html(str(item.get("summary") or raw_content))
            content = self.data_handler.strip_html(raw_content)
            pub_date = str(item.get("date_published") or item.get("date_modified") or "")
            author = ""
            if isinstance(item.get("author"), dict):
                author = str(item["author"].get("name") or "")
            attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
            pic_urls = self.data_handler.strip_html_pic(raw_content, feed_url)
            media_urls = []
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                media_type = str(attachment.get("mime_type") or "").lower()
                media_url = attachment.get("url")
                if not media_url:
                    continue
                if media_type.startswith("image"):
                    pic_urls.append(urljoin(feed_url, media_url))
                    continue
                label = "音频" if media_type.startswith("audio") else "视频" if media_type.startswith("video") else "媒体"
                media_urls.append(f"[{label}] {urljoin(feed_url, media_url)}")
            tags = [str(tag) for tag in item.get("tags", [])] if isinstance(item.get("tags"), list) else []
            items.append(
                RSSItem(
                    channel_title,
                    self._truncate_display(title, self.title_max_length),
                    link or guid or feed_url,
                    self._truncate_display(description, self.description_max_length),
                    pub_date,
                    self._parse_datetime_to_timestamp(pub_date),
                    list(dict.fromkeys(pic_urls)),
                    guid=guid,
                    author=author,
                    content=content,
                    tags=tags,
                    media_urls=media_urls,
                    image_captions=[],
                    source_url=feed_url,
                    content_hash=self._content_hash(title, link, content or description),
                )
            )
        return items

    def _parse_datetime_to_timestamp(self, value: str) -> int:
        """兼容多种 RSS/Atom 日期格式。"""
        if not value:
            return 0
        try:
            dt = parsedate_to_datetime(value)
        except Exception:
            try:
                dt = date_parser.parse(value)
            except Exception:
                return 0
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    def _content_hash(self, *parts: str) -> str:
        """为缺少 guid/link 的条目生成内容哈希。"""
        raw = "\n".join(part or "" for part in parts)
        return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()

    def _truncate_display(self, text: str, max_len: int) -> str:
        """传统展示文本截断。"""
        text = (text or "").strip()
        if max_len and max_len > 0 and len(text) > max_len:
            return text[:max_len].rstrip() + "..."
        return text

    def _fresh_asyncIOScheduler(self):
        """按订阅间隔刷新定时任务。"""
        self.logger.info("刷新 RSS 定时任务")
        self.scheduler.remove_all_jobs()
        for url, info in self.data_handler.data.items():
            if url in self.data_handler.RESERVED_KEYS or not isinstance(info, dict):
                continue
            for user, sub_info in info.get("subscribers", {}).items():
                if not isinstance(sub_info, dict) or not sub_info.get("enabled", True):
                    continue
                interval = max(1, int(sub_info.get("interval_minutes") or self.default_interval_minutes))
                self.scheduler.add_job(
                    self.cron_task_callback,
                    "interval",
                    minutes=interval,
                    args=[url, user],
                    id=self._job_id(url, user),
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                )

    def _job_id(self, url: str, user: str) -> str:
        """生成稳定 scheduler job id。"""
        digest = hashlib.sha1(f"{url}\n{user}".encode("utf-8", errors="ignore")).hexdigest()
        return f"rss_{digest}"

    async def cron_task_callback(self, url: str, user: str):
        """定时拉取订阅，默认交由 LLM 生成最终推送。"""
        if url not in self.data_handler.data:
            return
        feed_data = self.data_handler.data[url]
        sub_info = feed_data.get("subscribers", {}).get(user)
        if not isinstance(sub_info, dict) or not sub_info.get("enabled", True):
            return

        rss_items = await self.poll_rss(
            url,
            num=self.max_items_per_poll,
            after_timestamp=int(sub_info.get("last_update", 0) or 0),
            after_link=sub_info.get("latest_link", ""),
            seen_ids=sub_info.get("seen_ids", []),
            only_new=True,
        )
        if not rss_items:
            self.logger.info(f"RSS 定时任务 {url} 无消息更新 - {user}")
            return

        delivered = False
        try:
            if self.llm_mode_enabled:
                delivered = await self._send_items_via_llm(user, url, rss_items)
                if not delivered and self.send_user_fallback_on_llm_error:
                    delivered = await self._send_items_plain(user, rss_items)
            else:
                delivered = await self._send_items_plain(user, rss_items)
        except Exception as e:
            self.logger.error(
                f"rss: 推送 {url} - {user} 失败，将于下次重试: {e}",
                exc_info=True,
            )
            return

        if not delivered:
            self.logger.warning(f"rss: {url} - {user} 未成功推送，保留未读状态")
            return

        self._mark_items_seen(url, user, rss_items)
        self.data_handler.save_data()
        self.logger.info(f"RSS 定时任务 {url} 推送完成 - {user}")

    async def _send_items_via_llm(self, session_id: str, url: str, items: list[RSSItem]) -> bool:
        """将 RSS 更新作为任务交给 LLM，并发送 LLM 最终回复。"""
        await self._ensure_item_image_captions(items)
        prompt = self._build_llm_prompt(session_id, url, items)
        try:
            provider_id = await self.context.get_current_chat_provider_id(session_id)
            conv_id, history_messages, system_prompt = await self._prepare_conversation_context(session_id)
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                contexts=history_messages,
                system_prompt=system_prompt,
            )
            if response is None or not getattr(response, "completion_text", None):
                return False
            response_text = response.completion_text.strip()
            if not response_text:
                return False
            await self._send_text_to_session(session_id, response_text)
        except Exception as e:
            self.logger.error(f"rss: LLM 主动处理 RSS 更新失败: {e}", exc_info=True)
            return False

        try:
            await self._persist_llm_history(session_id, conv_id, prompt, response_text, response)
        except Exception as e:
            self.logger.warning(f"rss: 写入 LLM 对话历史失败: {e}", exc_info=True)
        return True

    async def _prepare_conversation_context(self, session_id: str) -> tuple[str, list[dict], str]:
        """获取/创建会话对话，并读取历史与人格。"""
        conv_mgr = self.context.conversation_manager
        conv_id = await conv_mgr.get_curr_conversation_id(session_id)
        if not conv_id:
            conv_id = await conv_mgr.new_conversation(session_id)
        conversation = await conv_mgr.get_conversation(session_id, conv_id, create_if_not_exists=True)
        history_messages: list[dict] = []
        if conversation and conversation.history:
            try:
                loaded = json.loads(conversation.history) if isinstance(conversation.history, str) else conversation.history
                if isinstance(loaded, list):
                    history_messages = loaded
            except Exception:
                history_messages = []
        system_prompt = await self._get_system_prompt(session_id, conversation)
        return conv_id, history_messages, system_prompt

    async def _get_system_prompt(self, session_id: str, conversation: Any) -> str:
        """读取当前会话 persona，失败时回退为空系统提示。"""
        try:
            if conversation and getattr(conversation, "persona_id", None):
                persona = await self.context.persona_manager.get_persona(conversation.persona_id)
                if persona:
                    return persona.system_prompt
            default_persona = await self.context.persona_manager.get_default_persona_v3(umo=session_id)
            if default_persona:
                return default_persona.get("prompt", "")
        except Exception as e:
            self.logger.debug(f"rss: 获取人格失败: {e}")
        return ""

    async def _persist_llm_history(self, session_id: str, conv_id: str, prompt: str, response_text: str, response: Any) -> None:
        """把 RSS 主动 LLM 回合写入 AstrBot 对话历史，含可选思考内容。"""
        conv_mgr = self.context.conversation_manager
        reasoning = (getattr(response, "reasoning_content", None) or "").strip()
        if UserMessageSegment and AssistantMessageSegment and TextPart:
            user_msg = UserMessageSegment(content=[TextPart(text=prompt)])
            assistant_parts = []
            if self.preserve_reasoning_in_history and reasoning and ThinkPart:
                assistant_parts.append(ThinkPart(think=reasoning, encrypted=getattr(response, "reasoning_signature", None)))
            assistant_parts.append(TextPart(text=response_text))
            assistant_msg = AssistantMessageSegment(content=assistant_parts)
            await conv_mgr.add_message_pair(cid=conv_id, user_message=user_msg, assistant_message=assistant_msg)
            return
        conversation = await conv_mgr.get_conversation(session_id, conv_id, create_if_not_exists=True)
        history = json.loads(conversation.history or "[]") if conversation else []
        history.append({"role": "user", "content": prompt})
        if self.preserve_reasoning_in_history and reasoning:
            history.append({"role": "assistant", "content": [{"type": "think", "think": reasoning}, {"type": "text", "text": response_text}]})
        else:
            history.append({"role": "assistant", "content": response_text})
        await conv_mgr.update_conversation(session_id, conv_id, history=history)

    def _build_llm_prompt(self, session_id: str, url: str, items: list[RSSItem]) -> str:
        """用配置模板渲染 RSS -> LLM 提示词。"""
        feed_info = self.data_handler.data.get(url, {}).get("info", {})
        sub_info = self.data_handler.data.get(url, {}).get("subscribers", {}).get(session_id, {})
        subscriber_kind = sub_info.get("subscriber_kind", "user")
        items_json = json.dumps(self._items_to_llm_payload(items), ensure_ascii=False, indent=2)
        template = self.llm_prompt_template or self.config.get("llm_prompt_template") or "{{rss_items}}"
        return (
            template.replace("{{session_id}}", session_id)
            .replace("{{subscriber_kind}}", str(subscriber_kind))
            .replace("{{feed_title}}", str(feed_info.get("title", items[0].chan_title if items else "未知频道")))
            .replace("{{feed_url}}", url)
            .replace("{{current_time}}", datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("{{item_count}}", str(len(items)))
            .replace("{{rss_items}}", items_json)
        )

    def _items_to_llm_payload(self, items: list[RSSItem], include_full_content: bool = True) -> list[dict]:
        """将条目转成受长度限制的 LLM payload。"""
        payload = []
        total = 0
        for item in items:
            data = item.to_dict(include_full_content=include_full_content, max_chars=self.max_item_chars)
            serialized = json.dumps(data, ensure_ascii=False)
            if self.max_total_chars > 0 and total + len(serialized) > self.max_total_chars:
                remaining = max(0, self.max_total_chars - total)
                data["content"] = str(data.get("content", ""))[:remaining].rstrip() + "..."
                payload.append(data)
                break
            payload.append(data)
            total += len(serialized)
        return payload

    def _get_toolbox_plugin_instance(self):
        """参考 toolbox 跨插件查找方式，定位 astrbot_plugin_toolbox_for_koko 实例。"""
        candidate_names = [
            "astrbot_plugin_toolbox_for_koko",
            "toolbox_for_koko",
            "toolbox",
            "多功能工具箱",
        ]
        for plugin_name in candidate_names:
            try:
                meta = self.context.get_registered_star(plugin_name)
            except Exception:
                meta = None
            if meta and getattr(meta, "star_cls", None):
                return meta.star_cls

        try:
            all_stars = self.context.get_all_stars()
        except Exception:
            all_stars = []

        for meta in all_stars:
            module_path = str(getattr(meta, "module_path", "") or "").lower()
            star_name = str(getattr(meta, "name", "") or "").lower()
            if "toolbox_for_koko" in module_path or "toolbox_for_koko" in star_name:
                star_cls = getattr(meta, "star_cls", None)
                if star_cls:
                    return star_cls
        return None

    async def _ensure_item_image_captions(self, items: list[RSSItem]) -> None:
        """遇到图片/GIF 时调用 toolbox 的 image_caption 转述，并保留图片链接。"""
        for item in items:
            if not item.pic_urls or item.image_captions:
                continue
            captions = await self._caption_image_urls(item.pic_urls)
            if captions:
                item.image_captions = captions

    async def _caption_image_urls(self, image_urls: list[str]) -> list[dict[str, str]]:
        """跨插件调用 toolbox tool_image_caption，返回 url + caption 映射。"""
        urls = list(dict.fromkeys([str(url or "").strip() for url in image_urls if str(url or "").strip()]))
        if not urls:
            return []

        toolbox = self._get_toolbox_plugin_instance()
        if not toolbox:
            self.logger.debug("rss: 未找到 toolbox 插件实例，跳过图片转述")
            return []

        result = None
        try:
            tool_image_caption = getattr(toolbox, "tool_image_caption", None)
            if callable(tool_image_caption):
                result = await tool_image_caption(
                    None,
                    urls=urls,
                    use_event_images=False,
                    prompt=self.image_caption_prompt,
                )
            else:
                run_koko_tool = getattr(toolbox, "run_koko_tool", None)
                if callable(run_koko_tool):
                    result = await run_koko_tool(
                        None,
                        tool_name="tool_image_caption",
                        args={
                            "urls": urls,
                            "use_event_images": False,
                            "prompt": self.image_caption_prompt,
                        },
                    )
        except Exception as e:
            self.logger.debug(f"rss: 调用 toolbox image_caption 失败: {e}")
            return []

        message = ""
        if isinstance(result, dict):
            if str(result.get("status", "success") or "success").lower() == "error":
                return []
            message = str(result.get("message") or "")
        elif isinstance(result, str):
            message = result
        return self._parse_image_caption_message(urls, message)

    def _parse_image_caption_message(self, image_urls: list[str], message: str) -> list[dict[str, str]]:
        """解析 toolbox 转述结果，省略 [图片]/[GIF] 占位符但保留链接。"""
        lines = [line.strip() for line in str(message or "").splitlines() if line.strip()]
        captions = []
        for idx, url in enumerate(image_urls):
            line = lines[idx] if idx < len(lines) else ""
            line = re.sub(r"^\d+[\.、]\s*", "", line).strip()
            text = ""
            match = re.match(r"^\[[^\]:：]+[:：]\s*(.*?)\]$", line, re.S)
            if match:
                text = match.group(1).strip()
            elif re.match(r"^\[[^\]:：]+\]$", line):
                text = ""
            else:
                text = line.strip()
            if text:
                captions.append({"url": url, "caption": '[图像]'+text})
        return captions

    async def _send_items_plain(self, session_id: str, items: list[RSSItem]) -> bool:
        """辅助模式：不经 LLM，直接发送 RSS 文本。返回是否至少送出一条。"""
        parsed = self._parse_session_id(session_id)
        if parsed and parsed[0] == "aiocqhttp" and self.is_compose and len(items) > 1:
            nodes = []
            for item in items:
                try:
                    comps = await self._get_chain_components(item)
                    nodes.append(Comp.Node(uin=0, name="Astrbot", content=comps))
                except Exception as e:
                    self.logger.warning(f"rss: 组装合并转发节点失败 {item.link}: {e}")
            if not nodes:
                return False
            try:
                await self.context.send_message(
                    session_id,
                    MessageChain(chain=nodes, use_t2i_=self.t2i),
                )
                return True
            except Exception as e:
                self.logger.warning(f"rss: 合并转发推送失败，回退逐条发送: {e}")

        sent_any = False
        for item in items:
            try:
                comps = await self._get_chain_components(item)
                await self.context.send_message(
                    session_id,
                    MessageChain(chain=comps, use_t2i_=self.t2i),
                )
                sent_any = True
            except Exception as e:
                self.logger.warning(f"rss: 单条推送失败 {item.link}: {e}", exc_info=True)
        return sent_any

    async def _send_text_to_session(self, session_id: str, text: str) -> None:
        """优先通过平台实例精确发送，失败则回退 context.send_message。"""
        chain = MessageChain().message(message=text)
        parsed = self._parse_session_id(session_id)
        if parsed and MS and MessageType:
            platform_id, msg_type_str, target_id = parsed
            platforms = self.context.platform_manager.get_insts()
            target_platform = next((p for p in platforms if p.meta().id == platform_id), None)
            is_running = target_platform and (not PlatformStatus or target_platform.status == PlatformStatus.RUNNING)
            if target_platform and is_running:
                try:
                    msg_type = MessageType.GROUP_MESSAGE if "Group" in msg_type_str or "Guild" in msg_type_str else MessageType.FRIEND_MESSAGE
                    await target_platform.send_by_session(MS(platform_name=platform_id, message_type=msg_type, session_id=target_id), chain)
                    await self._persist_platform_history(session_id, chain)
                    return
                except Exception as e:
                    self.logger.warning(f"rss: 平台精确发送失败，回退核心发送: {e}")
        await self.context.send_message(session_id, chain)
        await self._persist_platform_history(session_id, chain)

    async def _persist_platform_history(self, session_id: str, chain: MessageChain) -> None:
        """尽量补写平台消息流水，方便后续主动上下文读取。"""
        if message_chain_to_storage_message_parts is None:
            return
        parsed = self._parse_session_id(session_id)
        history_mgr = getattr(self.context, "message_history_manager", None)
        db = getattr(history_mgr, "db", None) if history_mgr else None
        insert_attachment = getattr(db, "insert_attachment", None)
        if not parsed or not history_mgr or not callable(insert_attachment):
            return
        platform_id, _message_type, target_id = parsed
        try:
            message_parts = await message_chain_to_storage_message_parts(chain, insert_attachment=insert_attachment, attachments_dir=None)
            if message_parts:
                await history_mgr.insert(
                    platform_id=platform_id,
                    user_id=target_id,
                    content={"type": "bot", "message": message_parts},
                    sender_id="bot",
                    sender_name="bot",
                )
        except Exception as e:
            self.logger.debug(f"rss: 补写平台流水失败: {e}")

    def _parse_session_id(self, session_id: str) -> tuple[str, str, str] | None:
        """解析 UMO，兼容 target 中带冒号的情况。"""
        if not isinstance(session_id, str):
            return None
        for msg_type in ["FriendMessage", "GroupMessage", "PrivateMessage", "GuildMessage"]:
            marker = f":{msg_type}:"
            idx = session_id.find(marker)
            if idx != -1:
                return session_id[:idx], msg_type, session_id[idx + len(marker):]
        parts = session_id.split(":")
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        if len(parts) > 3:
            return ":".join(parts[:-2]), parts[-2], parts[-1]
        return None

    def _mark_items_seen(self, url: str, user: str, items: list[RSSItem]) -> None:
        """更新订阅者与 Feed 的去重状态。"""
        feed_data = self.data_handler.data.get(url)
        if not isinstance(feed_data, dict):
            return
        sub_info = feed_data.get("subscribers", {}).get(user)
        if not isinstance(sub_info, dict):
            return
        max_ts = max([int(sub_info.get("last_update", 0) or 0), *[item.pubDate_timestamp for item in items]])
        sub_info["last_update"] = max_ts
        sub_info["latest_link"] = items[0].link if items else sub_info.get("latest_link", "")
        seen = list(sub_info.get("seen_ids", []))
        for item in items:
            identity = item.identity()
            if identity and identity not in seen:
                seen.insert(0, identity)
        sub_info["seen_ids"] = seen[: max(10, self.history_seen_limit)]
        state = feed_data.setdefault("state", {})
        state["last_update"] = max_ts
        state["latest_link"] = sub_info["latest_link"]
        state["seen_ids"] = sub_info["seen_ids"]

    async def _add_url(
        self,
        url: str,
        interval_minutes: int | None,
        message: AstrMessageEvent | None = None,
        session_id: str | None = None,
        subscriber_kind: str = "user",
    ):
        """添加 URL 订阅的共用逻辑。"""
        user = session_id or (message.unified_msg_origin if message else "")
        if not user:
            raise ValueError("缺少订阅会话 UMO")
        url = self._normalize_url(url)
        if not self._is_url_allowed(url):
            raise ValueError("该 URL 指向 localhost/内网/保留地址，已拒绝录入")

        text = await self.parse_channel_info(url)
        if text is None:
            raise ValueError("无法获取 Feed 内容")
        title, desc = self.data_handler.parse_channel_text_info(text)
        latest_items = await self.poll_rss(url, num=self.max_items_per_poll, only_new=False)
        normalized_interval = self._normalize_interval_minutes(interval_minutes)
        sub_payload = self._new_subscription_payload(
            normalized_interval,
            subscriber_kind=subscriber_kind,
        )
        sub_payload["seen_ids"] = [item.identity() for item in latest_items if item.identity()][: self.history_seen_limit]
        if latest_items:
            sub_payload["last_update"] = max(item.pubDate_timestamp for item in latest_items)
            sub_payload["latest_link"] = latest_items[0].link

        if url not in self.data_handler.data:
            self.data_handler.data[url] = {
                "subscribers": {},
                "info": {"title": title, "description": desc},
                "state": {"seen_ids": sub_payload["seen_ids"], "last_update": sub_payload["last_update"], "latest_link": sub_payload["latest_link"]},
            }
        else:
            self.data_handler.data[url].setdefault("info", {"title": title, "description": desc})
            self.data_handler.data[url]["info"].update({"title": title, "description": desc})
            self.data_handler.data[url].setdefault("subscribers", {})
            self.data_handler.data[url].setdefault("state", {})
        self.data_handler.data[url]["subscribers"][user] = sub_payload
        self.data_handler.save_data()
        self._fresh_asyncIOScheduler()
        return self.data_handler.data[url]["info"]

    async def _get_chain_components(self, item: RSSItem):
        """组装辅助模式消息链。"""
        comps = [Comp.Plain(f"频道 {item.chan_title} 最新 Feed\n---\n标题: {item.title}\n---\n")]
        if not self.is_hide_url:
            comps.append(Comp.Plain(f"链接: {item.link}\n---\n"))
        comps.append(Comp.Plain((item.description or item.content or "") + "\n---\n"))
        if item.media_urls:
            comps.append(Comp.Plain("\n".join(item.media_urls) + "\n---\n"))
        if self.is_read_pic and item.pic_urls:
            temp_max_pic_item = len(item.pic_urls) if self.max_pic_item == -1 else self.max_pic_item
            for pic_url in item.pic_urls[:temp_max_pic_item]:
                base64str = await self.pic_handler.modify_corner_pixel_to_base64(pic_url)
                comps.append(Comp.Image.fromBase64(base64str) if base64str else Comp.Plain("图片链接读取失败\n"))
        return comps

    def _format_subscription_list(self, user: str) -> str:
        """格式化当前会话订阅列表。"""
        subs_urls = self.data_handler.get_subs_channel_url(user)
        if not subs_urls:
            return "当前没有 RSS 订阅。"
        lines = ["当前订阅的频道："]
        for idx, url in enumerate(subs_urls):
            info = self.data_handler.data[url].get("info", {})
            sub = self.data_handler.data[url].get("subscribers", {}).get(user, {})
            lines.append(f"{idx}. {info.get('title', '未知频道')} - 每 {sub.get('interval_minutes', self.default_interval_minutes)} 分钟 - {url}")
        return "\n".join(lines)

    @filter.command_group("rss", alias={"RSS"})
    def rss(self):
        """RSS 订阅插件；当前以 LLM 主导推送，命令保留为辅助入口。"""
        pass

    @rss.command("add-url")
    async def add_url_command(self, event: AstrMessageEvent, url: str, interval_minutes: int = 0):
        """直接通过 Feed URL 添加订阅。"""
        try:
            info = await self._add_url(
                url,
                interval_minutes or self.default_interval_minutes,
                event,
                subscriber_kind="user",
            )
        except Exception as e:
            yield event.plain_result(f"添加失败: {e}")
            return
        yield event.plain_result(f"添加成功。频道信息：\n标题: {info['title']}\n描述: {info['description']}")

    @rss.command("list")
    async def list_command(self, event: AstrMessageEvent):
        """列出当前所有订阅的 RSS 频道。"""
        yield event.plain_result(self._format_subscription_list(event.unified_msg_origin))

    @rss.command("remove")
    async def remove_command(self, event: AstrMessageEvent, idx: int):
        """删除一个 RSS 订阅。"""
        subs_urls = self.data_handler.get_subs_channel_url(event.unified_msg_origin)
        if idx < 0 or idx >= len(subs_urls):
            yield event.plain_result("索引越界，请使用 /rss list 查看已经添加的订阅")
            return
        url = subs_urls[idx]
        self.data_handler.data[url].get("subscribers", {}).pop(event.unified_msg_origin, None)
        self.data_handler.save_data()
        self._fresh_asyncIOScheduler()
        yield event.plain_result("删除成功")

    @rss.command("get")
    async def get_command(self, event: AstrMessageEvent, idx: int, mode: str = "latest", limit: int = 1):
        """获取指定订阅内容；mode=latest/new。"""
        subs_urls = self.data_handler.get_subs_channel_url(event.unified_msg_origin)
        if idx < 0 or idx >= len(subs_urls):
            yield event.plain_result("索引越界，请使用 /rss list 查看已经添加的订阅")
            return
        url = subs_urls[idx]
        sub = self.data_handler.data[url].get("subscribers", {}).get(event.unified_msg_origin, {})
        rss_items = await self.poll_rss(
            url,
            num=limit,
            after_timestamp=int(sub.get("last_update", 0) or 0),
            after_link=sub.get("latest_link", ""),
            seen_ids=sub.get("seen_ids", []),
            only_new=(mode == "new"),
        )
        if not rss_items:
            yield event.plain_result("没有订阅内容")
            return
        parsed = self._parse_session_id(event.unified_msg_origin)
        if parsed and parsed[0] == "aiocqhttp" and self.is_compose and len(rss_items) > 1:
            nodes = []
            for item in rss_items:
                nodes.append(
                    Comp.Node(
                        uin=0,
                        name="Astrbot",
                        content=await self._get_chain_components(item),
                    )
                )
            yield event.chain_result(nodes).use_t2i(self.t2i)
            return
        for item in rss_items:
            yield event.chain_result(await self._get_chain_components(item)).use_t2i(self.t2i)

    @filter.llm_tool(name="rss_subscribe_feed")
    async def rss_subscribe_feed(self, event: AstrMessageEvent, feed_url: str = "", interval_minutes: int = 0) -> dict:
        """订阅一个 RSS/Atom/JSON Feed。参数必须包含完整 http/https feed_url；interval_minutes 为自动拉取间隔分钟数，缺省使用全局默认值。订阅后插件会按间隔自动拉取新增内容，并将更新交给 LLM 生成最终回复后发送给当前会话。"""
        try:
            info = await self._add_url(
                feed_url,
                interval_minutes or self.default_interval_minutes,
                event,
                subscriber_kind="llm",
            )
            return {"status": "success", "message": "订阅已添加", "data": {"title": info["title"], "description": info["description"], "interval_minutes": interval_minutes or self.default_interval_minutes}}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @filter.llm_tool(name="rss_list_subscriptions")
    async def rss_list_subscriptions(self, event: AstrMessageEvent) -> dict:
        """列出当前会话已订阅的 RSS 源，用于确认订阅索引、标题、URL、拉取间隔和最近状态。"""
        user = event.unified_msg_origin
        items = []
        for idx, url in enumerate(self.data_handler.get_subs_channel_url(user)):
            feed = self.data_handler.data[url]
            sub = feed.get("subscribers", {}).get(user, {})
            items.append({"index": idx, "url": url, "title": feed.get("info", {}).get("title", "未知频道"), "description": feed.get("info", {}).get("description", ""), "interval_minutes": sub.get("interval_minutes", self.default_interval_minutes), "last_update": sub.get("last_update", 0)})
        return {"status": "success", "message": f"共 {len(items)} 个订阅", "data": {"subscriptions": items}}

    @filter.llm_tool(name="rss_remove_subscription")
    async def rss_remove_subscription(self, event: AstrMessageEvent, index: int = -1, feed_url: str = "") -> dict:
        """删除当前会话的 RSS 订阅。可传 index（来自 rss_list_subscriptions）或完整 feed_url；参数不足时拒绝删除。"""
        user = event.unified_msg_origin
        subs = self.data_handler.get_subs_channel_url(user)
        target_url = ""
        if feed_url:
            target_url = self._normalize_url(feed_url)
        elif 0 <= index < len(subs):
            target_url = subs[index]
        else:
            return {"status": "error", "message": "请提供有效 index 或 feed_url"}
        self.data_handler.data.get(target_url, {}).get("subscribers", {}).pop(user, None)
        self.data_handler.save_data()
        self._fresh_asyncIOScheduler()
        return {"status": "success", "message": "订阅已删除", "data": {"url": target_url}}

    @filter.llm_tool(name="rss_fetch_items")
    async def rss_fetch_items(
        self,
        event: AstrMessageEvent,
        feed_url: str = "",
        index: int = -1,
        limit: int = 5,
        only_new: bool = False,
        include_full_content: bool = True,
        mark_as_seen: bool = False,
    ) -> dict:
        """拉取 RSS/Atom/JSON Feed 条目并返回结构化内容给 LLM。"""
        try:
            user = event.unified_msg_origin
            subs = self.data_handler.get_subs_channel_url(user)
            url = self._normalize_url(feed_url) if feed_url else (subs[index] if 0 <= index < len(subs) else "")
            if not url:
                return {"status": "error", "message": "请提供 feed_url 或有效订阅 index"}
            sub = self.data_handler.data.get(url, {}).get("subscribers", {}).get(user, {})
            rss_items = await self.poll_rss(
                url,
                num=limit,
                after_timestamp=int(sub.get("last_update", 0) or 0),
                after_link=sub.get("latest_link", ""),
                seen_ids=sub.get("seen_ids", []),
                only_new=only_new,
            )
            await self._ensure_item_image_captions(rss_items)
            if mark_as_seen and url in self.data_handler.data and user in self.data_handler.data[url].get("subscribers", {}):
                self._mark_items_seen(url, user, rss_items)
                self.data_handler.save_data()
            return {
                "status": "success",
                "message": f"获取到 {len(rss_items)} 条内容",
                "data": {
                    "url": url,
                    "items": self._items_to_llm_payload(
                        rss_items,
                        include_full_content=include_full_content,
                    ),
                },
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @filter.llm_tool(name="rss_poll_subscriptions")
    async def rss_poll_subscriptions(
        self,
        event: AstrMessageEvent,
        only_new: bool = True,
        limit_per_feed: int = 5,
        mark_as_seen: bool = False,
    ) -> dict:
        """拉取当前会话所有订阅源的内容并直接返回给 LLM。"""
        user = event.unified_msg_origin
        results = []
        for url in self.data_handler.get_subs_channel_url(user):
            sub = self.data_handler.data[url].get("subscribers", {}).get(user, {})
            items = await self.poll_rss(
                url,
                num=limit_per_feed,
                after_timestamp=int(sub.get("last_update", 0) or 0),
                after_link=sub.get("latest_link", ""),
                seen_ids=sub.get("seen_ids", []),
                only_new=only_new,
            )
            await self._ensure_item_image_captions(items)
            if mark_as_seen:
                self._mark_items_seen(url, user, items)
            results.append(
                {
                    "url": url,
                    "title": self.data_handler.data[url].get("info", {}).get("title", "未知频道"),
                    "items": self._items_to_llm_payload(items),
                }
            )
        if mark_as_seen:
            self.data_handler.save_data()
        return {"status": "success", "message": "订阅拉取完成", "data": {"feeds": results}}

    @filter.llm_tool(name="rss_update_settings")
    async def rss_update_settings(
        self,
        event: AstrMessageEvent,
        proxy_url: str = "",
        clear_proxy: bool = False,
        default_interval_minutes: int = 0,
        max_items_per_poll: int = 0,
        max_item_chars: int = 0,
        max_total_chars: int = 0,
    ) -> dict:
        """更新 LLM 使用 RSS 所需的运行时设置。"""
        settings = self.data_handler.data.setdefault("settings", {})
        if clear_proxy:
            self.proxy_url = ""
            settings["proxy_url"] = ""
        elif proxy_url.strip():
            self.proxy_url = proxy_url.strip()
            settings["proxy_url"] = self.proxy_url
        for attr, value in (
            ("default_interval_minutes", default_interval_minutes),
            ("max_items_per_poll", max_items_per_poll),
            ("max_item_chars", max_item_chars),
            ("max_total_chars", max_total_chars),
        ):
            if value and value > 0:
                setattr(self, attr, int(value))
                settings[attr] = int(value)
        self.data_handler.save_data()
        self._fresh_asyncIOScheduler()
        return {"status": "success", "message": "RSS 设置已更新", "data": settings}