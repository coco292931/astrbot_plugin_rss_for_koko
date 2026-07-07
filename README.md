# astrbot-plugin-rss

面向 LLM 的 RSS / Atom / JSON Feed 订阅插件。

本插件从原来的“RSS 拉取后直接发给用户”，改为“RSS 拉取后先交给 LLM，由 LLM 结合人格、会话上下文和 RSS 内容生成最终回复，再发送给订阅会话”。用户命令仍保留为辅助入口，核心能力面向 LLM tool 调用。

## 主要能力

- **LLM-first 推送**：订阅源有更新时，插件将结构化 RSS 内容交给 LLM，最终由 LLM 决定如何向用户说明。
- **Tool 调用**：LLM 可直接添加、删除、列出订阅，也可拉取单个 Feed 或当前会话全部订阅。
- **间隔拉取**：使用 `interval_minutes` 按固定分钟间隔自动拉取，不再要求手动填写 cron 表达式。
- **历史去重**：每个订阅会话维护 `seen_ids`，避免重复把同一条 RSS 内容投喂给 LLM。
- **格式扩展**：支持 RSS / RDF / Atom XML，以及 JSON Feed。
- **代理支持**：支持全局 `proxy_url`，代理失败后可回退直连。
- **上下文安全**：图片/GIF 会尽量调用 `astrbot_plugin_toolbox_for_koko` 的 `tool_image_caption` 转述，并保留原图链接；音频、视频等多媒体不会直接附加到 LLM 上下文，只以 `[音频]`、`[视频]` 或链接占位。
- **轻量发送链路**：保留简化发送逻辑，优先平台会话发送，失败时回退 AstrBot 核心发送。

## 工作流

```text
RSS/Atom/JSON Feed
  -> 拉取与解析
  -> 标准化 RSSItem
  -> 历史去重 seen_ids
  -> 构造 LLM prompt
  -> 调用当前会话 LLM Provider
  -> 发送 LLM 最终回复
  -> 写入 AstrBot 对话历史
```

如果 LLM 调用失败，并且启用了 `send_user_fallback_on_llm_error`，插件会回退为直接发送 RSS 文本摘要，避免完全漏推。

## 安装

参考 AstrBot 插件安装方式。

依赖见 `requirements.txt`：

```text
aiohttp
apscheduler
beautifulsoup4
lxml
python-dateutil
pillow
```

## 用户辅助命令

这些命令保留给用户手动管理订阅。推荐主要通过 LLM tool 使用插件。

### 添加订阅

```text
/rss add-url <feed_url> [interval_minutes]
```

示例：

```text
/rss add-url https://example.com/feed.xml 30
```

含义：当前会话订阅该 Feed，每 30 分钟自动拉取一次。

### 列出订阅

```text
/rss list
```

输出当前会话订阅的 Feed 列表、索引和拉取间隔。

### 删除订阅

```text
/rss remove <idx>
```

`idx` 来自 `/rss list`。

### 手动获取内容

```text
/rss get <idx> [latest|new] [limit]
```

- `latest`：获取最新内容，不强制只看新增。
- `new`：只获取相对当前会话订阅状态的新增内容。
- `limit`：最多返回条数。

示例：

```text
/rss get 0 latest 3
/rss get 0 new 5
```

## LLM Tools

### `rss_subscribe_feed`

订阅一个 RSS / Atom / JSON Feed。

参数：

- `feed_url`：完整 Feed URL，必须是 `http://` 或 `https://`。
- `interval_minutes`：自动拉取间隔分钟数；为空或 0 时使用全局默认值。

返回：

```json
{
  "status": "success",
  "message": "订阅已添加",
  "data": {
    "title": "...",
    "description": "...",
    "interval_minutes": 60
  }
}
```

说明：通过该工具创建的订阅会记录 `subscriber_kind=llm`，自动触发 LLM 时会在提示词中告诉模型“这是由 LLM 工具订阅的 RSS”。

### `rss_list_subscriptions`

列出当前会话的订阅。

返回字段包括：

- `index`
- `url`
- `title`
- `description`
- `interval_minutes`
- `last_update`

### `rss_remove_subscription`

删除当前会话的订阅。

参数二选一：

- `index`：来自 `rss_list_subscriptions`。
- `feed_url`：完整 Feed URL。

参数不足或索引无效时会拒绝删除。

### `rss_fetch_items`

拉取单个 Feed 的结构化条目，直接返回给 LLM，不在工具内部总结。

参数：

- `feed_url`：直接拉取指定 Feed。
- `index`：拉取当前会话订阅列表中的某个 Feed。
- `limit`：最多返回条数。
- `only_new`：是否只返回新增条目。
- `include_full_content`：是否尽量返回正文内容。
- `mark_as_seen`：是否把本次返回内容标记为已读。

### `rss_poll_subscriptions`

拉取当前会话所有订阅源。

参数：

- `only_new`：是否只返回新增内容。
- `limit_per_feed`：每个 Feed 最多返回条数。
- `mark_as_seen`：是否把返回条目标记为已读。

适合 LLM 主动问：“我有哪些 RSS 更新？”时调用。

### `rss_update_settings`

更新 RSS 插件运行时设置。该设置会写入数据文件 `settings`，插件重启后继续生效。

参数：

- `proxy_url`：全局代理地址，例如 `http://127.0.0.1:7890`。
- `clear_proxy`：清空当前代理。
- `default_interval_minutes`：默认订阅拉取间隔。
- `max_items_per_poll`：每个订阅自动拉取时最多返回条数。
- `max_item_chars`：单个 post 返回给 LLM 的最大字数。
- `max_total_chars`：单次返回给 LLM 的总字数上限。

## 配置项

### LLM 主导推送

#### `llm_mode_enabled`

- 类型：`bool`
- 默认值：`true`
- 说明：开启后，自动拉取到新 RSS 内容时会先交给 LLM 生成最终回复；关闭后使用辅助文本推送。

#### `llm_prompt_template`

- 类型：`text`
- 说明：RSS 更新触发 LLM 时使用的提示词模板。

可用占位符：

- `{{session_id}}`：订阅会话 UMO。
- `{{subscriber_kind}}`：订阅来源，`llm` 或 `user`。
- `{{feed_title}}`：订阅源标题。
- `{{feed_url}}`：订阅源 URL。
- `{{current_time}}`：当前时间。
- `{{item_count}}`：本次更新条目数。
- `{{rss_items}}`：结构化 RSS JSON 内容。

#### `preserve_reasoning_in_history`

- 类型：`bool`
- 默认值：`true`
- 说明：如果模型返回 `reasoning_content`，写入 AstrBot 对话历史时会以 `think` 内容保存。

#### `image_caption_prompt`

- 类型：`text`
- 默认值：`请用中文简洁转述这张 RSS 内容中的 {image_type}。只描述图片实际内容，不要输出图片占位符。`
- 说明：RSS 图片/GIF 调用 `astrbot_plugin_toolbox_for_koko` 的 `tool_image_caption` 时使用的提示词。
- 可用占位符：`{image_type}`、`{index}`、`{total}`、`{source}`。
- 留空时传空值

#### `send_user_fallback_on_llm_error`

- 类型：`bool`
- 默认值：`true`
- 说明：LLM 调用失败时，回退为直接发送 RSS 文本摘要。

### 拉取间隔与数量限制

#### `default_interval_minutes`

- 类型：`int`
- 默认值：`60`
- 说明：新增订阅未指定间隔时使用。

#### `max_items_per_poll`

- 类型：`int`
- 默认值：`5`
- 说明：每个订阅每次自动拉取的最大条目数。

#### `hard_max_items_per_fetch`

- 类型：`int`
- 默认值：`50`
- 说明：单次抓取硬上限，防止 LLM tool 或异常 Feed 一次返回过多内容。

#### `max_item_chars`

- 类型：`int`
- 默认值：`1200`
- 说明：单个 post 返回给 LLM 的最大字数。

#### `max_total_chars`

- 类型：`int`
- 默认值：`8000`
- 说明：单次返回给 LLM 的总字数上限。

#### `history_seen_limit`

- 类型：`int`
- 默认值：`500`
- 说明：每个订阅保留的历史去重 ID 数量。

### 代理与安全

#### `proxy_url`

- 类型：`string`
- 默认值：空
- 说明：全局代理地址。配置后优先走代理。

#### `trust_env`

- 类型：`bool`
- 默认值：`true`
- 说明：是否读取系统环境代理。

#### `proxy_timeout_fallback`

- 类型：`bool`
- 默认值：`true`
- 说明：配置代理时，如果代理请求失败或超时，是否回退直连。

#### `allow_private_network`

- 类型：`bool`
- 默认值：`false`
- 说明：是否允许访问 localhost、内网、保留地址。默认关闭以降低 SSRF 风险。

#### `max_response_bytes`

- 类型：`int`
- 默认值：`2097152`
- 说明：Feed 响应体最大字节数，默认 2 MB。

### 辅助文本推送配置

以下配置只影响非 LLM 模式或 LLM 失败时的辅助推送：

- `title_max_length`
- `description_max_length`
- `compose`
- `t2i`
- `is_hide_url`
- `pic_config.is_read_pic`
- `pic_config.is_adjust_pic`
- `pic_config.max_pic_item`

## 数据结构

数据文件默认仍位于：

```text
data/astrbot_plugin_rss_data.json
```

新版结构大致如下：

```json
{
  "rsshub_endpoints": [],
  "settings": {
    "proxy_url": "http://127.0.0.1:7890",
    "default_interval_minutes": 60
  },
  "https://example.com/feed.xml": {
    "info": {
      "title": "Example Feed",
      "description": "..."
    },
    "state": {
      "last_update": 0,
      "latest_link": "",
      "seen_ids": []
    },
    "subscribers": {
      "default:FriendMessage:123456": {
        "interval_minutes": 60,
        "last_update": 0,
        "latest_link": "",
        "seen_ids": [],
        "enabled": true,
        "subscriber_kind": "llm",
        "created_at": 0
      }
    }
  }
}
```

旧版 `cron_expr` 订阅会在插件初始化时尽量迁移为 `interval_minutes`。

## 多媒体策略

当前不会把音频、视频等多媒体直接放入 LLM 上下文。图片/GIF 会尽量跨插件调用 `toolbox` 的 `tool_image_caption` 做转述。

处理方式：

- 图片/GIF：保留原始链接到 `images` 字段；如果 `astrbot_plugin_toolbox_for_koko` 可用，会额外写入 `image_captions`，格式为 `{url, caption}`。转述失败时只保留链接，不额外写入 `[图片]` / `[GIF]` 占位符。
- 音频：用 `[音频] <url>` 占位。
- 视频：用 `[视频] <url>` 占位。
- 其他附件：用 `[媒体] <url>` 占位。

这能避免把大文件或不稳定多媒体内容直接塞进 LLM 上下文。

## 与旧版差异

- 移除主要流程中的 cron 表达式输入，改为 `interval_minutes`。
- RSSHub endpoint 管理不再作为主入口；如需 RSSHub，请直接订阅完整 RSSHub URL。
- RSS 内容不再默认直接推给用户，而是优先交给 LLM 处理。
- Feed 格式从原 RSS 2.0 扩展到 RSS / RDF / Atom / JSON Feed。
- 新增代理、SSRF 防护、响应体大小限制、历史去重和 LLM 字数限制。

## 注意事项

- 插件需要在 AstrBot 运行环境内加载，独立目录下 VS Code 可能会提示 `astrbot` 相关导入无法解析。
- 当前发送链路保持轻量化，没有完整复刻主动消息插件的 TTS、分段、装饰钩子链路。
- 自动触发 LLM 需要当前会话有可用的聊天模型 Provider。
- `allow_private_network` 默认关闭，如果你的 RSSHub 部署在内网，需要手动开启。

## 验证建议

1. 在 AstrBot 中加载插件，确认 LLM tools 注册成功。
2. 调用 `rss_subscribe_feed` 订阅一个真实 Feed。
3. 调用 `rss_fetch_items` 确认结构化条目可返回给 LLM。
4. 手动触发一次 `cron_task_callback` 或等待定时任务，确认能调用 LLM 并发送最终回复。
5. 查看 AstrBot 对话历史，确认主动 RSS 回合已落盘。
