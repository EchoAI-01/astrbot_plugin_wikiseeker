# Wiki Seeker

> 让 AstrBot 的 LLM 在回复前主动从多种知识源检索准确信息，避免凭记忆编造。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.0.0-blue)](https://github.com/AstrBotDevs/AstrBot)
![version](https://img.shields.io/badge/version-1.0.0-green)

支持 **MediaWiki 百科**、**任意「API + 密钥」的笔记型 Wiki**（Notion / 语雀 / Confluence / 飞书文档…）以及 **静态文档站**（Docusaurus / VuePress / VitePress / GitBook）。检索结果可沉淀进知识库与 Skill，并随使用持续优化。

## 特性

- **三类检索后端**，靠站点配置的 `type` 字段区分，互不影响：
  - `mediawiki` —— MediaWiki Action API。
  - `http_api` —— 通用自定义 HTTP API，用户自配 endpoint、请求头（含密钥）、请求参数与响应字段映射，**任何返回 JSON 的搜索接口都能接入**。
  - `docsite` —— 静态文档站，走 `sitemap.xml` 列页 → 抓正文 → 关键词匹配。
- **两条触发路径，各自可开关**：
  - 关键词预搜索注入：命中站点关键词时，在 LLM 请求前自动检索并注入上下文。
  - LLM 自助调用：`search_wiki` 工具，由 LLM 自行判断何时检索。
- **无需指令触发**，与「主动回复」类插件天然兼容（凡走 `request_llm` 的请求都会被预搜索增强）。
- **知识库沉淀**：可把不再更新的资料沉淀进 AstrBot 知识库；`static` 站点首次实时检索后自动回存，二次走知识库缓存。
- **Skill 自进化**：LLM 可通过 `update_wiki_skill` 把有效检索经验持续写入 `SKILL.md`。

## 安装

**方式一：WebUI**
在 AstrBot WebUI 的「插件市场 / 安装插件」中填入本仓库地址安装。

**方式二：手动**
```bash
cd AstrBot/data/plugins
git clone https://github.com/EchoAI-01/astrbot_plugin_wikiseeker.git
```
重启 AstrBot 或在 WebUI 重载插件。依赖（`aiohttp`）会自动安装；HTML 解析使用 Python 标准库，无额外依赖。

## 配置

在 WebUI 插件配置页填写，或编辑插件配置文件。

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `wiki_sites` | string(JSON) | 含 1 个 Minecraft 示例 | 站点列表，JSON 数组，详见下文 |
| `enable_llm_tool` | bool | `true` | 启用 `search_wiki` 工具（LLM 自助检索） |
| `enable_keyword_trigger` | bool | `true` | 启用关键词预搜索注入 |
| `max_search_results` | int | `3` | 每个站点取回的结果数量上限 |
| `max_extract_chars` | int | `2000` | 单个页面正文截断字符数 |
| `request_timeout` | int | `10` | 网络请求超时（秒） |
| `enable_kb` | bool | `false` | 启用知识库功能（需配 embedding provider） |
| `embedding_provider_id` | string | `""` | 知识库使用的 Embedding Provider ID |
| `kb_chunk_size` | int | `512` | 知识库入库分块大小 |
| `enable_skill_evolution` | bool | `true` | 启用 `update_wiki_skill` 工具 |

### `wiki_sites` 站点配置

JSON 数组，每个站点是一个对象。**通用字段**（所有 type 共用）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✅ | 站点唯一标识 |
| `type` | | `mediawiki`（默认）/ `http_api` / `docsite` |
| `description` | | 站点用途说明，供 LLM 判断何时检索哪个站 |
| `mode` | | `live`（持续更新，实时检索）/ `static`（不再更新，优先走知识库） |
| `kb_name` | | 关联的知识库名（`static` 必填，`live` 选填） |
| `keywords` | | 命中即触发预搜索的关键词数组 |

#### type = `mediawiki`

| 字段 | 必填 | 说明 |
|---|---|---|
| `api_endpoint` | ✅ | MediaWiki `api.php` 地址 |
| `language` | | 语言标记 |

```jsonc
{
  "name": "minecraft-zh",
  "type": "mediawiki",
  "api_endpoint": "https://zh.minecraft.wiki/api.php",
  "description": "中文 Minecraft 百科：方块、物品、生物、合成、机制等",
  "mode": "live",
  "keywords": ["我的世界", "minecraft", "mc"]
}
```

#### type = `http_api`（通用 API + 密钥，兼容 Notion / 语雀 / Confluence / 飞书 等）

| 字段 | 必填 | 说明 |
|---|---|---|
| `api_endpoint` | ✅ | 搜索接口 URL |
| `method` | | `GET`（默认）/ `POST` |
| `headers` | | 对象，放认证及其他请求头，如 `{"Authorization": "Bearer xxx"}` |
| `query_param` | | **GET** 时检索词放入的 URL 参数名（默认 `q`） |
| `extra_params` | | **GET** 的额外固定参数；值中可用 `{query}` 占位 |
| `body_template` | | **POST** 请求体 JSON 模板，用 `{query}` 占位 |
| `results_path` | | 响应 JSON 中结果数组的点分路径，如 `data`、`data.hits` |
| `title_field` | | 每个结果项里标题字段的点分路径（支持数字索引，如 `properties.title.0.plain_text`） |
| `content_field` | | 每个结果项里正文/摘要字段的点分路径 |

> 点分路径支持嵌套对象与数组索引，例如 `a.b.0.c`。`{query}` 在 POST 请求体中会自动做 JSON 转义。

```jsonc
// 语雀（GET + X-Auth-Token）
{
  "name": "yuque-team",
  "type": "http_api",
  "mode": "live",
  "api_endpoint": "https://www.yuque.com/api/v2/search",
  "method": "GET",
  "headers": { "X-Auth-Token": "<你的 token>" },
  "query_param": "q",
  "extra_params": { "type": "doc" },
  "results_path": "data",
  "title_field": "title",
  "content_field": "summary",
  "keywords": ["语雀"]
}

// Notion（POST + Bearer + Notion-Version）
{
  "name": "notion-kb",
  "type": "http_api",
  "mode": "live",
  "api_endpoint": "https://api.notion.com/v1/search",
  "method": "POST",
  "headers": {
    "Authorization": "Bearer <token>",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
  },
  "body_template": "{\"query\": \"{query}\", \"page_size\": 5}",
  "results_path": "results",
  "title_field": "properties.title.title.0.plain_text",
  "content_field": "url",
  "keywords": ["notion"]
}
```

#### type = `docsite`（静态文档站）

| 字段 | 必填 | 说明 |
|---|---|---|
| `base_url` | ✅ | 站点根地址，如 `https://docusaurus.io` |
| `sitemap_url` | | sitemap 地址，默认 `{base_url}/sitemap.xml` |

```jsonc
{
  "name": "docusaurus-docs",
  "type": "docsite",
  "mode": "live",
  "base_url": "https://docusaurus.io",
  "keywords": ["docusaurus"]
}
```

## LLM 工具

插件向 LLM 注册三个工具，由 LLM 按需自助调用：

| 工具 | 作用 | 开关 |
|---|---|---|
| `search_wiki(query, site_name="")` | 在指定站点（留空则全部）检索并返回正文 | `enable_llm_tool` |
| `save_to_kb(site_name, page_title)` | 把某个 MediaWiki 页面正文沉淀进知识库 | `enable_kb` |
| `update_wiki_skill(site_name, tip)` | 把一条有效检索经验追加进 `SKILL.md` | `enable_skill_evolution` |

## 工作原理

```
用户消息
  └─ on_llm_request 钩子：命中站点 keywords → 预检索 → 注入 system_prompt
        └─ LLM 推理
              └─ 需要更多信息时自助调用 search_wiki / save_to_kb / update_wiki_skill
```

- 关键词触发覆盖**所有**走 `request_llm` 的请求，因此主动回复场景也会先检索再回复。
- `static` 站点的检索流程：先查知识库 → 未命中则实时检索 → 结果回存知识库。

## 已知限制

- **`http_api` 为单步检索**：直接使用搜索接口返回的字段。对于搜索结果不含正文的 API（如 Notion search 仅返回页面对象），只能拿到标题/链接/摘要级信息；如需全文需用户配置带摘要的字段或自行扩展二次取详情。
- **`docsite` 为关键词匹配近似**（非语义检索），且依赖站点提供 `sitemap.xml`：
  - 对 **Docusaurus / VuePress / VitePress / GitBook** 等静态生成站（SSG）有效。
  - 对 **Docsify** 这类纯客户端渲染、且默认不生成 sitemap 的站点，可能取不到正文。
  - sitemap 过大时有抓取数量上限（默认 500）与并发限制，可能漏页；抓取的正文可能含少量导航文字。
- **知识库**需先在 AstrBot 服务提供商中配置 Embedding 类型供应商并填写 `embedding_provider_id`，否则插件会自动降级为不使用知识库。

## 许可

随主项目 AstrBot 协议。
