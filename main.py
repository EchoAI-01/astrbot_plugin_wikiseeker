"""Wiki Seeker.

让 LLM 在回复前主动从多种知识源检索准确信息。

支持三类检索后端（站点配置的 ``type`` 字段区分，默认 ``mediawiki``）：
- ``mediawiki``：MediaWiki Action API。
- ``http_api``：通用自定义 HTTP API（用户自配 endpoint/headers/字段映射），
  可接入 Notion、语雀、Confluence、飞书等任意返回 JSON 的搜索接口。
- ``docsite``：静态文档站（Docsify/VuePress/Docusaurus/GitBook），走
  sitemap.xml 列页 + 抓正文 + 关键词匹配。

两条触发路径：
1. 关键词预搜索注入：``on_llm_request`` 钩子在每次 LLM 请求前检测关键词，命中则
   主动检索并把结果注入 ``req.system_prompt``。该钩子覆盖所有走 request_llm 的请求，
   因此与"主动回复"类插件天然兼容。
2. LLM 自助调用：``search_wiki`` 工具，由 LLM 自行判断何时检索。

知识库（KB）按站点 ``mode`` 区分：``static``（不再更新）优先查 KB、miss 再实时搜并回存；
``live``（持续更新）直接实时搜。``save_to_kb`` 工具让 LLM 决定把页面沉淀入库。
``update_wiki_skill`` 工具让 LLM 把搜索经验持续沉淀进 SKILL.md。
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import llm_tool, logger
from astrbot.api.all import Context, Star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.provider.entities import ProviderRequest

_USER_AGENT = "AstrBot-WikiSeeker/1.0 (+https://github.com/AstrBotDevs/AstrBot)"

_DEFAULT_SKILL_MD = """---
name: wiki-search
description: 当用户询问游戏、百科、设定、版本机制等需要权威资料的问题时，使用 wiki 检索工具获取准确信息后再作答，避免凭记忆编造。
---

# Wiki 检索技能

本机器人接入了若干知识源（MediaWiki 百科、自定义 API 笔记库、静态文档站），
可在回答前检索准确信息。

## 何时检索
- 用户提到具体游戏/作品/产品的物品、角色、机制、数值、版本更新、文档说明等事实性问题。
- 你对答案不确定，或信息可能随版本变化。

## 如何检索
- 调用 `search_wiki` 工具：`query` 填核心检索词（尽量用条目/页面的标准名称），
  `site_name` 可指定站点；不确定时留空以搜索全部站点。
- 工具对不同来源（MediaWiki / 自定义 API / 文档站）的差异已封装，用法一致。
- 拿到结果后用中文总结，并标注信息来源站点。

## 检索策略
- 来源可能是 MediaWiki 百科、自定义 API 的笔记库（如 Notion/语雀/Confluence），
  或静态文档站（Docsify/VuePress/Docusaurus/GitBook）；文档站为关键词匹配，
  检索词越贴近页面用语命中越好。
- `live` 站点（持续更新）：每次实时检索，结果最新。
- `static` 站点（不再更新）：优先命中知识库缓存，速度快、省资源。

## 持续优化
- 当你摸索出某站点的有效检索套路（如标准条目命名、关键词组合）时，
  调用 `update_wiki_skill` 把经验记录到本技能，供以后参考。

## 经验沉淀
<!-- update_wiki_skill 写入的经验条目会追加到下方 -->
"""

_DOCSITE_URL_CAP = 500  # 单次 docsite 检索最多考虑的页面 URL 数
_DOCSITE_FETCH_CONCURRENCY = 5  # docsite 抓取候选页正文的并发上限


def _dig(obj: Any, path: str) -> Any:
    """按点分路径取值，数字段作为 list 索引；任一层缺失或类型不符返回 None。

    例：``_dig(data, "properties.title.0.plain_text")``。
    """
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


class _HTMLTextExtractor(HTMLParser):
    """从 HTML 抽取纯正文与标题，跳过 script/style 等噪声标签。"""

    _SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._in_title = False
        self._in_h1 = False
        self.title = ""
        self._h1 = ""
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        if self._in_h1 and not self._h1.strip():
            self._h1 += data
        text = data.strip()
        if text:
            self._chunks.append(text)

    @property
    def heading(self) -> str:
        """优先用 <title>，缺失时退回首个 <h1>。"""
        return (self.title or self._h1).strip()

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._chunks)).strip()


def _html_to_text(raw: str) -> tuple[str, str]:
    """把 HTML 转成 (标题, 纯正文)，正文已压缩空白、解码实体。"""
    parser = _HTMLTextExtractor()
    try:
        parser.feed(raw)
    except Exception:
        # 极端畸形 HTML 时退回正则去标签兜底
        stripped = re.sub(r"<[^>]+>", " ", raw)
        return "", re.sub(r"\s+", " ", html.unescape(stripped)).strip()
    return html.unescape(parser.heading), html.unescape(parser.text)


def _tokenize(query: str) -> list[str]:
    """把检索词小写后按非字母数字（含中日韩）边界切分为 token。"""
    return [t for t in re.split(r"[^0-9a-z一-鿿]+", query.lower()) if t]


def _score(text: str, tokens: list[str]) -> int:
    """统计 text（小写）中命中的 token 总次数。"""
    low = text.lower()
    return sum(low.count(t) for t in tokens)


class Main(Star):
    """Wiki Seeker 插件主类。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config: dict = config or {}
        self.sites: dict[str, dict] = {}
        self._session: aiohttp.ClientSession | None = None

        # 读取配置（带默认值）
        self.enable_llm_tool: bool = self.config.get("enable_llm_tool", True)
        self.enable_keyword_trigger: bool = self.config.get(
            "enable_keyword_trigger", True
        )
        self.max_results: int = int(self.config.get("max_search_results", 3))
        self.max_extract_chars: int = int(self.config.get("max_extract_chars", 2000))
        self.request_timeout: int = int(self.config.get("request_timeout", 10))
        self.enable_kb: bool = self.config.get("enable_kb", False)
        self.embedding_provider_id: str = self.config.get("embedding_provider_id", "")
        self.kb_chunk_size: int = int(self.config.get("kb_chunk_size", 512))
        self.enable_skill_evolution: bool = self.config.get(
            "enable_skill_evolution", True
        )

        self._skill_md = Path(__file__).parent / "skills" / "wiki-search" / "SKILL.md"

    async def initialize(self) -> None:
        """加载站点配置、校验依赖、准备 skill 与 HTTP 会话。"""
        self._parse_sites()

        # 知识库依赖 embedding provider，缺失则降级
        if self.enable_kb and not self.embedding_provider_id:
            logger.warning(
                "[WikiSeeker] enable_kb=True 但未配置 embedding_provider_id，"
                "已自动降级为不使用知识库。"
            )
            self.enable_kb = False

        # 确保 SKILL.md 存在（防止被误删）
        if not self._skill_md.exists():
            self._skill_md.parent.mkdir(parents=True, exist_ok=True)
            self._skill_md.write_text(_DEFAULT_SKILL_MD, encoding="utf-8")

        self._session = aiohttp.ClientSession(headers={"User-Agent": _USER_AGENT})

        # 按配置开关停用对应工具
        if not self.enable_llm_tool:
            self.context.deactivate_llm_tool("search_wiki")
        if not self.enable_kb:
            self.context.deactivate_llm_tool("save_to_kb")
        if not self.enable_skill_evolution:
            self.context.deactivate_llm_tool("update_wiki_skill")

        logger.info(
            f"[WikiSeeker] 已加载 {len(self.sites)} 个站点；"
            f"关键词触发={self.enable_keyword_trigger}，知识库={self.enable_kb}。"
        )

    async def terminate(self) -> None:
        """关闭 HTTP 会话。"""
        if self._session and not self._session.closed:
            await self._session.close()

    def _parse_sites(self) -> None:
        """解析 wiki_sites JSON 配置到 self.sites，逐项校验。"""
        raw = self.config.get("wiki_sites", "[]")
        try:
            items = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as e:
            logger.error(f"[WikiSeeker] wiki_sites 不是合法 JSON：{e}")
            return
        if not isinstance(items, list):
            logger.error("[WikiSeeker] wiki_sites 必须是 JSON 数组。")
            return

        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                logger.warning(f"[WikiSeeker] 跳过缺少 name 的站点：{item}")
                continue

            stype = item.get("type", "mediawiki")
            if stype not in ("mediawiki", "http_api", "docsite"):
                logger.warning(
                    f"[WikiSeeker] 站点 {name} 的 type='{stype}' 非法，跳过。"
                )
                continue

            mode = item.get("mode", "live")
            if mode not in ("live", "static"):
                logger.warning(
                    f"[WikiSeeker] 站点 {name} 的 mode='{mode}' 非法，按 live 处理。"
                )
                mode = "live"

            site = {
                "name": name,
                "type": stype,
                "description": item.get("description", ""),
                "mode": mode,
                "kb_name": item.get("kb_name", ""),
                "keywords": [k for k in item.get("keywords", []) if k],
            }

            if stype == "mediawiki":
                endpoint = item.get("api_endpoint")
                if not endpoint:
                    logger.warning(
                        f"[WikiSeeker] 站点 {name}(mediawiki) 缺少 api_endpoint，跳过。"
                    )
                    continue
                site["api_endpoint"] = endpoint
                site["language"] = item.get("language", "")
            elif stype == "http_api":
                endpoint = item.get("api_endpoint")
                if not endpoint:
                    logger.warning(
                        f"[WikiSeeker] 站点 {name}(http_api) 缺少 api_endpoint，跳过。"
                    )
                    continue
                method = str(item.get("method", "GET")).upper()
                if method not in ("GET", "POST"):
                    logger.warning(
                        f"[WikiSeeker] 站点 {name} 的 method='{method}' 非法，按 GET 处理。"
                    )
                    method = "GET"
                site.update(
                    {
                        "api_endpoint": endpoint,
                        "method": method,
                        "headers": item.get("headers") or {},
                        "query_param": item.get("query_param", "q"),
                        "extra_params": item.get("extra_params") or {},
                        "body_template": item.get("body_template", ""),
                        "results_path": item.get("results_path", ""),
                        "title_field": item.get("title_field", ""),
                        "content_field": item.get("content_field", ""),
                    }
                )
            else:  # docsite
                base_url = item.get("base_url")
                if not base_url:
                    logger.warning(
                        f"[WikiSeeker] 站点 {name}(docsite) 缺少 base_url，跳过。"
                    )
                    continue
                base_url = base_url.rstrip("/")
                site["base_url"] = base_url
                site["sitemap_url"] = (
                    item.get("sitemap_url") or f"{base_url}/sitemap.xml"
                )

            self.sites[name] = site

    async def _api_get(self, endpoint: str, params: dict[str, Any]) -> dict:
        """发起 MediaWiki Action API GET 请求并返回 JSON。

        Args:
            endpoint: 站点 api.php 地址。
            params: 查询参数。

        Returns:
            解析后的 JSON 字典。

        Raises:
            RuntimeError: 请求失败、超时或响应非 JSON 时。
        """
        assert self._session is not None
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        try:
            async with self._session.get(
                endpoint, params=params, timeout=timeout
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        except Exception as e:
            raise RuntimeError(str(e)) from e

    async def _fetch_extracts(self, endpoint: str, titles: list[str]) -> dict[str, str]:
        """批量获取页面纯文本正文。

        Args:
            endpoint: 站点 api.php 地址。
            titles: 页面标题列表。

        Returns:
            {标题: 正文} 字典，正文已按 max_extract_chars 截断。
        """
        if not titles:
            return {}
        data = await self._api_get(
            endpoint,
            {
                "action": "query",
                "prop": "extracts",
                "explaintext": "1",
                "exlimit": "max",
                "titles": "|".join(titles),
                "format": "json",
                "formatversion": "2",
            },
        )
        out: dict[str, str] = {}
        for page in data.get("query", {}).get("pages", []):
            extract = (page.get("extract") or "").strip()
            if extract:
                out[page.get("title", "")] = extract[: self.max_extract_chars]
        return out

    async def _search_site(self, site: dict, query: str) -> str:
        """按站点 type 分派到对应后端检索，返回拼接好的正文文本。"""
        stype = site.get("type", "mediawiki")
        if stype == "http_api":
            return await self._search_http_api(site, query)
        if stype == "docsite":
            return await self._search_docsite(site, query)
        return await self._search_mediawiki(site, query)

    async def _search_mediawiki(self, site: dict, query: str) -> str:
        """MediaWiki Action API 检索：先 list=search 取标题，再 prop=extracts 取正文。"""
        name = site["name"]
        endpoint = site["api_endpoint"]
        try:
            data = await self._api_get(
                endpoint,
                {
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": str(self.max_results),
                    "format": "json",
                    "formatversion": "2",
                    "utf8": "1",
                },
            )
        except RuntimeError as e:
            logger.warning(f"[WikiSeeker] 站点 {name} 搜索失败：{e}")
            return f"（{name} 检索失败：{e}）"

        hits = data.get("query", {}).get("search", [])
        if not hits:
            return f"（在 {name} 未找到与“{query}”相关的条目）"

        titles = [h["title"] for h in hits if h.get("title")]
        try:
            extracts = await self._fetch_extracts(endpoint, titles)
        except RuntimeError as e:
            logger.warning(f"[WikiSeeker] 站点 {name} 取正文失败：{e}")
            return f"（{name} 取正文失败：{e}）"

        parts = []
        for title in titles:
            text = extracts.get(title)
            if text:
                parts.append(f"## {title}\n{text}")
        if not parts:
            return f"（在 {name} 找到条目但无正文摘要）"
        return "\n\n".join(parts)

    async def _search_http_api(self, site: dict, query: str) -> str:
        """通用自定义 HTTP API 检索：按用户配置发请求并用点分路径映射结果字段。"""
        assert self._session is not None
        name = site["name"]
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        headers = dict(site.get("headers") or {})
        try:
            if site["method"] == "POST":
                # {query} 做 JSON 转义后填入请求体模板，避免引号破坏 JSON
                escaped = json.dumps(query)[1:-1]
                body = site["body_template"].replace("{query}", escaped)
                headers.setdefault("Content-Type", "application/json")
                async with self._session.post(
                    site["api_endpoint"],
                    data=body.encode("utf-8"),
                    headers=headers,
                    timeout=timeout,
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
            else:
                params = {site["query_param"]: query}
                for k, v in (site.get("extra_params") or {}).items():
                    params[k] = v.replace("{query}", query) if isinstance(v, str) else v
                async with self._session.get(
                    site["api_endpoint"],
                    params=params,
                    headers=headers,
                    timeout=timeout,
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[WikiSeeker] 站点 {name}(http_api) 请求失败：{e}")
            return f"（{name} 检索失败：{e}）"

        results = _dig(data, site["results_path"]) if site["results_path"] else data
        if not isinstance(results, list):
            return f"（{name} 响应中未按 results_path 取到结果列表）"
        if not results:
            return f"（在 {name} 未找到与“{query}”相关的条目）"

        parts = []
        for item in results[: self.max_results]:
            title = _dig(item, site["title_field"]) if site["title_field"] else ""
            content = _dig(item, site["content_field"]) if site["content_field"] else ""
            title = str(title or "").strip()
            content = str(content or "").strip()[: self.max_extract_chars]
            if title or content:
                head = f"## {title}\n" if title else ""
                parts.append(f"{head}{content}".strip())
        if not parts:
            return (
                f"（在 {name} 找到结果但 title/content 字段映射为空，请检查字段路径）"
            )
        return "\n\n".join(parts)

    async def _fetch_sitemap_urls(self, sitemap_url: str, name: str) -> list[str]:
        """抓取并解析 sitemap，返回页面 URL 列表（对 .xml 子 sitemap 递归一层）。"""
        assert self._session is not None
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        try:
            async with self._session.get(sitemap_url, timeout=timeout) as resp:
                resp.raise_for_status()
                xml = await resp.text()
        except Exception as e:
            logger.warning(f"[WikiSeeker] 站点 {name}(docsite) sitemap 获取失败：{e}")
            return []

        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, re.IGNORECASE | re.DOTALL)
        locs = [html.unescape(u.strip()) for u in locs if u.strip()]
        pages: list[str] = []
        for loc in locs:
            if loc.lower().endswith(".xml"):
                # sitemap index：递归抓一层子 sitemap
                try:
                    async with self._session.get(loc, timeout=timeout) as r2:
                        r2.raise_for_status()
                        sub = await r2.text()
                    pages += [
                        html.unescape(u.strip())
                        for u in re.findall(
                            r"<loc>\s*(.*?)\s*</loc>", sub, re.IGNORECASE | re.DOTALL
                        )
                        if u.strip() and not u.strip().lower().endswith(".xml")
                    ]
                except Exception as e:
                    logger.warning(
                        f"[WikiSeeker] 站点 {name} 子 sitemap {loc} 获取失败：{e}"
                    )
            else:
                pages.append(loc)
            if len(pages) >= _DOCSITE_URL_CAP:
                break
        return pages[:_DOCSITE_URL_CAP]

    async def _search_docsite(self, site: dict, query: str) -> str:
        """静态文档站检索：sitemap 列页 → URL 初筛 → 抓正文 → 关键词重排。"""
        assert self._session is not None
        name = site["name"]
        tokens = _tokenize(query)
        if not tokens:
            return f"（{name} 检索词为空）"

        pages = await self._fetch_sitemap_urls(site["sitemap_url"], name)
        if not pages:
            return f"（{name} 未能从 sitemap 获取页面列表）"

        # URL 路径初筛：命中 token 越多越靠前；全 0 则取前若干兜底
        ranked = sorted(pages, key=lambda u: _score(u, tokens), reverse=True)
        cand_n = max(self.max_results * 3, self.max_results)
        candidates = ranked[:cand_n]

        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        sem = asyncio.Semaphore(_DOCSITE_FETCH_CONCURRENCY)

        async def fetch(url: str) -> tuple[str, str, str] | None:
            async with sem:
                try:
                    async with self._session.get(url, timeout=timeout) as resp:
                        resp.raise_for_status()
                        raw = await resp.text()
                except Exception:
                    return None
            title, text = _html_to_text(raw)
            if not text:
                return None
            return url, title or url, text

        fetched = [
            r for r in await asyncio.gather(*(fetch(u) for u in candidates)) if r
        ]
        if not fetched:
            return f"（{name} 命中页面但无法提取正文）"

        # 用正文命中度重排，取 top max_results
        scored = sorted(fetched, key=lambda r: _score(r[2], tokens), reverse=True)
        parts = []
        for url, title, text in scored[: self.max_results]:
            if _score(text, tokens) == 0:
                continue
            parts.append(f"## {title}\n{text[: self.max_extract_chars]}\n来源：{url}")
        if not parts:
            return f"（在 {name} 未找到与“{query}”相关的内容）"
        return "\n\n".join(parts)

    async def _retrieve_kb(self, kb_name: str, query: str) -> str | None:
        """从知识库检索，返回格式化上下文文本（无命中返回 None）。"""
        kb_mgr = getattr(self.context, "kb_manager", None)
        if kb_mgr is None or not kb_name:
            return None
        try:
            res = await kb_mgr.retrieve(query, kb_names=[kb_name])
        except Exception as e:
            logger.warning(f"[WikiSeeker] 知识库 {kb_name} 检索失败：{e}")
            return None
        if res and res.get("context_text"):
            return res["context_text"]
        return None

    async def _upload_to_kb(self, kb_name: str, title: str, text: str) -> None:
        """把一段正文写入知识库（库不存在则按需创建）。"""
        kb_mgr = getattr(self.context, "kb_manager", None)
        if kb_mgr is None or not kb_name or not text:
            return
        try:
            kb = await kb_mgr.get_kb_by_name(kb_name)
            if kb is None:
                kb = await kb_mgr.create_kb(
                    kb_name,
                    embedding_provider_id=self.embedding_provider_id,
                    chunk_size=self.kb_chunk_size,
                )
            chunks = [c.strip() for c in text.split("\n\n") if c.strip()] or [text]
            await kb.upload_document(
                file_name=f"{title}.md",
                file_content=None,
                file_type="md",
                chunk_size=self.kb_chunk_size,
                pre_chunked_text=chunks,
            )
        except Exception as e:
            logger.warning(f"[WikiSeeker] 知识库 {kb_name} 入库失败：{e}")

    async def _resolve(self, site: dict, query: str) -> str:
        """按站点 mode 决定走知识库还是实时检索。"""
        if site["mode"] == "static" and self.enable_kb and site.get("kb_name"):
            hit = await self._retrieve_kb(site["kb_name"], query)
            if hit:
                return hit
            # 知识库未命中：实时检索后回存（static 站点内容稳定，可长期缓存）
            text = await self._search_site(site, query)
            await self._upload_to_kb(site["kb_name"], query, text)
            return text
        return await self._search_site(site, query)

    @filter.on_llm_request()
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """关键词命中时预检索 wiki 并注入本次 LLM 请求的 system_prompt。"""
        if not self.enable_keyword_trigger or not self.sites:
            return
        msg = (event.message_str or "").lower()
        if not msg:
            return

        injected = []
        for site in self.sites.values():
            if any(k.lower() in msg for k in site["keywords"]):
                text = await self._resolve(site, event.message_str)
                if text:
                    injected.append(f"【{site['name']} 检索结果】\n{text}")
        if injected:
            block = "\n\n".join(injected)
            req.system_prompt = (req.system_prompt or "") + (
                "\n\n以下是从 Wiki 检索到的相关资料，请优先依据这些准确信息作答，"
                f"并在必要时注明来源：\n{block}"
            )

    @llm_tool("search_wiki")
    async def search_wiki(
        self, event: AstrMessageEvent, query: str, site_name: str = ""
    ) -> str:
        """检索 Wiki 站点以获取准确信息。当用户询问游戏、百科、设定等需要权威资料的问题时调用。

        Args:
            query(string): 搜索关键词或条目名称，尽量使用 wiki 条目的标准名称。
            site_name(string): 可选，指定要搜索的站点 name；留空则搜索所有已配置站点。
        """
        if not self.sites:
            return "未配置任何 Wiki 站点。"
        if site_name:
            site = self.sites.get(site_name)
            if not site:
                return f"未找到站点 {site_name}。可用站点：{', '.join(self.sites)}"
            targets = [site]
        else:
            targets = list(self.sites.values())

        results = []
        for site in targets:
            text = await self._resolve(site, query)
            results.append(f"【{site['name']}】\n{text}")
        return "\n\n".join(results)

    @llm_tool("save_to_kb")
    async def save_to_kb(
        self, event: AstrMessageEvent, site_name: str, page_title: str
    ) -> str:
        """将某个 Wiki 页面的内容保存到知识库，供以后快速检索（适用于不再频繁更新的资料）。

        Args:
            site_name(string): 站点 name。
            page_title(string): 要保存的 Wiki 页面标题（使用条目标准名称）。
        """
        if not self.enable_kb:
            return "知识库功能未启用。"
        site = self.sites.get(site_name)
        if not site:
            return f"未找到站点 {site_name}。"
        if site.get("type", "mediawiki") != "mediawiki":
            return (
                f"站点 {site_name} 不是 MediaWiki，无法按页面标题抓取；"
                "如需沉淀，请用 search_wiki 检索（static 站点会自动回存知识库）。"
            )
        kb_name = site.get("kb_name") or site_name
        try:
            extracts = await self._fetch_extracts(site["api_endpoint"], [page_title])
        except RuntimeError as e:
            return f"获取页面失败：{e}"
        text = extracts.get(page_title) or next(iter(extracts.values()), "")
        if not text:
            return f"未能获取页面《{page_title}》的内容。"
        await self._upload_to_kb(kb_name, page_title, text)
        return f"已将《{page_title}》保存到知识库 {kb_name}。"

    @llm_tool("update_wiki_skill")
    async def update_wiki_skill(
        self, event: AstrMessageEvent, site_name: str, tip: str
    ) -> str:
        """把一条有效的 Wiki 搜索经验沉淀到技能文档，供以后参考并持续优化检索效果。

        Args:
            site_name(string): 相关站点 name，或填 general 表示通用经验。
            tip(string): 要记录的检索经验或技巧。
        """
        if not self.enable_skill_evolution:
            return "skill 进化功能未启用。"
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            entry = f"- [{ts}] ({site_name}) {tip}\n"
            existing = (
                self._skill_md.read_text(encoding="utf-8")
                if self._skill_md.exists()
                else _DEFAULT_SKILL_MD
            )
            self._skill_md.write_text(existing + entry, encoding="utf-8")
        except Exception as e:
            return f"记录经验失败：{e}"
        return "已记录该经验，后续检索会参考。"
