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


def _embed_media_into_body(body: str, media_urls: list[str]) -> str:
    """将媒体引用（音频/视频）嵌入正文末尾。"""
    if not media_urls:
        return body
    extras = [m for m in media_urls if m.strip()]
    if extras:
        body = body.rstrip() + "\n\n" + "\n".join(extras)
    return body


def _embed_images_into_body(
    body: str,
    pic_urls: list[str],
    image_captions: list[dict[str, str]],
) -> str:
    """将图片转述嵌入正文对应位置。

    策略：
    1. 每张图先尝试在 body 中查找其 URL，找到则用 ``[图片转述: caption]`` 替换。
    2. 未嵌入正文的图片，在末尾以有序列表 ``n. [图片转述: caption]`` 追加。
    3. 转述失败的图片回退为 ``[图片: url]``。
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

    # 未嵌入正文的图片追加到末尾，带序号方便 LLM 定位
    remaining = [url for url in pic_urls if url and url not in replaced]
    if remaining:
        extras = []
        for idx, url in enumerate(remaining, 1):
            caption = caption_map.get(url, "")
            if caption:
                extras.append(f"{idx}. [图片转述: {caption}]")
            else:
                extras.append(f"{idx}. [图片: {url}]")
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
        """转换为 LLM 友好的结构化字典。

        注意：channel_title 等 feed 级元数据由调用方在顶层提供，
        item 层不重复携带以避免浪费 token。
        """
        body = (
            self.content if include_full_content and self.content else self.description
        )
        if max_chars and max_chars > 0 and len(body) > max_chars:
            body = body[:max_chars].rstrip() + "\n\n[...已截断]"

        # 将图片转述嵌入正文；媒体引用也嵌入正文，不设独立字段
        body = _embed_images_into_body(body, self.pic_urls, self.image_captions)
        body = _embed_media_into_body(body, self.media_urls)

        result: dict[str, object] = {
            "id": self.identity(),
            "title": self.title,
            "link": self.link,
            "published_cst": _format_cst(self.pubDate_timestamp),
            "content": body,
        }
        if self.author:
            result["author"] = self.author
        tags = [t for t in self.tags if t]
        if tags:
            result["tags"] = tags
        return result

    def __str__(self):
        return f"{self.title} - {self.link} - {self.description} - {self.pubDate}"
