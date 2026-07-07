from __future__ import annotations

import json
import os
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from lxml import etree

class DataHandler:
    """RSS 插件数据读写与轻量内容清洗。"""

    RESERVED_KEYS = {"rsshub_endpoints", "settings"}

    def __init__(self, config_path="data/astrbot_plugin_rss_data.json", default_config=None):
        self.config_path = config_path
        self.default_config = default_config or {
            "rsshub_endpoints": [],
            "settings": {},
        }
        self.data = self.load_data()

    def _ensure_shape(self, data: dict) -> dict:
        """补齐新版数据结构需要的根字段。"""
        if not isinstance(data, dict):
            data = {}
        data.setdefault("rsshub_endpoints", [])
        data.setdefault("settings", {})
        return data

    def get_subs_channel_url(self, user_id) -> list:
        """获取用户订阅的频道 url 列表。"""
        subs_url = []
        for url, info in self.data.items():
            if url in self.RESERVED_KEYS or not isinstance(info, dict):
                continue
            subscribers = info.get("subscribers", {})
            if isinstance(subscribers, dict) and user_id in subscribers:
                subs_url.append(url)
        return subs_url

    def load_data(self):
        """从数据文件中加载数据。"""
        data_dir = os.path.dirname(self.config_path)
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)

        if not os.path.exists(self.config_path):
            with open(self.config_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(self.default_config, indent=2, ensure_ascii=False))
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return self._ensure_shape(json.load(f))
        except (OSError, json.JSONDecodeError):
            return self._ensure_shape(self.default_config.copy())

    def save_data(self):
        """保存数据到数据文件。"""
        data_dir = os.path.dirname(self.config_path)
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def parse_channel_text_info(self, text):
        """解析 RSS/Atom 频道信息。"""
        parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
        root = etree.fromstring(text, parser=parser)
        title = self._first_xpath_text(
            root,
            [
                "/*[local-name()='rss']/*[local-name()='channel']/*[local-name()='title']/text()",
                "/*[local-name()='feed']/*[local-name()='title']/text()",
                "//*[local-name()='channel']/*[local-name()='title']/text()",
                "//*[local-name()='title']/text()",
            ],
        )
        description = self._first_xpath_text(
            root,
            [
                "/*[local-name()='rss']/*[local-name()='channel']/*[local-name()='description']/text()",
                "/*[local-name()='feed']/*[local-name()='subtitle']/text()",
                "//*[local-name()='channel']/*[local-name()='description']/text()",
                "//*[local-name()='description']/text()",
            ],
        )
        return title or "未知频道", description or ""

    def _first_xpath_text(self, node, paths: list[str]) -> str:
        """按顺序读取第一个非空 XPath 文本。"""
        for path in paths:
            try:
                values = node.xpath(path)
            except Exception:
                continue
            for value in values:
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    return text
        return ""

    def strip_html_pic(self, html, base_url: str = "") -> list[str]:
        """解析 HTML 内容，提取图片地址。"""
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        ordered_content = []

        for img in soup.find_all('img'):
            img_src = img.get('src') or img.get('data-src')
            if img_src:
                ordered_content.append(urljoin(base_url, img_src))

        return ordered_content

    def strip_html_media(self, html, base_url: str = "") -> list[str]:
        """提取暂不直接注入 LLM 上下文的音视频链接。"""
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        media_urls = []
        for tag_name, label in (("audio", "音频"), ("video", "视频")):
            for tag in soup.find_all(tag_name):
                src = tag.get("src")
                if src:
                    media_urls.append(f"[{label}] {urljoin(base_url, src)}")
                for source in tag.find_all("source"):
                    source_src = source.get("src")
                    if source_src:
                        media_urls.append(f"[{label}] {urljoin(base_url, source_src)}")
        return media_urls

    def strip_html(self, html):
        """去除 HTML 标签并压缩空行。"""
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n")
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def get_root_url(self, url):
        """获取 URL 的根域名。"""
        parsed_url = urlparse(url)
        return f"{parsed_url.scheme}://{parsed_url.netloc}"
