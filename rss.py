from __future__ import annotations

from dataclasses import dataclass, field


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
        body = self.content if include_full_content and self.content else self.description
        if max_chars and max_chars > 0 and len(body) > max_chars:
            body = body[:max_chars].rstrip() + "..."

        return {
            "id": self.identity(),
            "channel_title": self.chan_title,
            "title": self.title,
            "url": self.link,
            "author": self.author,
            "published": self.pubDate,
            "published_timestamp": self.pubDate_timestamp,
            "summary": self.description,
            "content": body,
            "tags": self.tags,
            "images": self.pic_urls,
            "image_captions": self.image_captions,
            "media": self.media_urls,
        }

    def __str__(self):
        return f"{self.title} - {self.link} - {self.description} - {self.pubDate}"