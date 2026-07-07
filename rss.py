from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta


CST = timezone(timedelta(hours=8))


def _format_cst(ts: int) -> str:
    """将 UNIX 时间戳转为 +8 区可读字符串。"""
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(ts, tz=CST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return ""


def _embed_images_into_body(
    body: str,
    pic_urls: list[str],
    image_captions: list[dict[str, str]],
) -> str:
    """将图片转述嵌入正文对应位置。

    策略：
    1. 对 pic_urls 中每张图，在 body 内查找其 URL 并用转述标记替换。
    2. 如果 body 中找不到对应 URL，则在末尾附加该图片的转述。
    3. 转述失败时回退到 URL（LLM 可自行尝试读取）。
    """
    caption_map: dict[str, str] = {}
    for cap in image_captions:
        caption_map[cap.get("url", "")] = cap.get("caption", "")

    # 按 URL 从长到短排序，避免短 URL 被部分匹配
    sorted_urls = sorted(pic_urls, key=lambda u: -len(u))
    replaced = set()
    for url in sorted_urls:
        caption = caption_map.get(url, "")
        if caption:
            marker = f"[图片转述: {caption}]"
        elif url:
            marker = f"[图片: {url}]"
        else:
            continue
        if url and url in body:
            body = body.replace(url, marker)
            replaced.add(url)

    # 未嵌入正文的图片追加到末尾
    remaining = [url for url in pic_urls if url and url not in replaced]
    if remaining:
        extras = []
        for url in remaining:
            caption = caption_map.get(url, "")
            if caption:
                extras.append(f"[图片转述: {caption}]")
            else:
                extras.append(f"[图片: {url}]")
        body = body.rstrip() + "\n\n" + "\n".join(extras)

    return body


@dataclass
class RSSItem:
    """标准化后的 RSS / Atom / JSON Feed 条目。"""

    chan_title: str
    title: str
    link: str
    description: str
    pubDate: str
    pubDate_timestamp: int
    pic_urls: list[str]
    guid: str = ""
    author: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    media_urls: list[str] = field(default_factory=list)
    image_captions: list[dict[str, str]] = field(default_factory=list)
    source_url: str = ""
    content_hash: str = ""

    def identity(self) -> str:
        """返回用于历史去重的稳定标识。"""
        return self.guid or self.link or self.content_hash

    def to_dict(
        self,
        *,
        include_full_content: bool = False,
        max_chars: int = 0,
    ) -> dict:
        """转换为 LLM 友好的结构化字典。"""
        body = (
            self.content if include_full_content and self.content else self.description
        )
        if max_chars and max_chars > 0 and len(body) > max_chars:
            body = body[:max_chars].rstrip() + "..."

        # 将图片转述嵌入正文
        body = _embed_images_into_body(body, self.pic_urls, self.image_captions)

        return {
            "id": self.identity(),
            "channel_title": self.chan_title,
            "title": self.title,
            "url": self.link,
            "author": self.author,
            "published": self.pubDate,
            "published_timestamp": self.pubDate_timestamp,
            "published_cst": _format_cst(self.pubDate_timestamp),
            "summary": self.description,
            "content": body,
            "tags": self.tags,
            "media": self.media_urls,
        }

    def __str__(self):
        return f"{self.title} - {self.link} - {self.description} - {self.pubDate}"
