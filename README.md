# astrbot_plugin_free_search

免费联网搜索插件 —— 默认引擎零 API Key、零第三方依赖，可选配 Tavily。

## 功能

- **AI 自动搜索**：注册 `web_search` LLM 工具，AI 在聊天中遇到需要实时信息的问题会自动调用，并以自然口语转述结果。
  - 可选参数：`site`（限定站点）、`time_range`（day/week/month/year 只看近期）、`max_results`（1-10 条）。
- **手动搜索**：发送 `/搜索 关键词`，结果同样由 AI 转述而非机械罗列。
- **网页阅读**：注册 `web_read` LLM 工具，AI 能打开链接读正文（用户分享的文章、搜索结果来源页）。

## 引擎

| 引擎 | 说明 |
| --- | --- |
| bing | 必应网页抓取，国内直连最稳 |
| ddg | DuckDuckGo HTML，需网络可达（有代理可用） |
| tavily | Tavily 官方 API，质量最好；需在插件配置填 `tavily_api_key`（app.tavily.com 免费注册，每月 1000 次额度）；`tavily_depth` 可选 advanced（2 倍额度）；可开 `tavily_include_answer` 直接返回综合答案 |
| auto | 配了 Tavily 密钥：tavily → bing → ddg；未配：bing → ddg（默认） |

思路移植自 dsh-free-search（DeepSeek Harness 插件）的免费引擎方案。
需要代理才能用 ddg / Tavily 时，填插件配置里的 `proxy`（如 `http://127.0.0.1:7890`）。

## 上下文保护（重要）

Tavily 的 `content` 返回的是**整页正文**，单条常在 1000+ 字，一条搜索就能塞进去几千 token，
而且工具结果会永久留在对话历史里反复发送。为此加了几道闸门，全部可在管理面板调整：

| 配置项 | 默认 | 作用 |
| --- | --- | --- |
| `snippet_max_chars` | 220 | 单条摘要字数上限（主闸门） |
| `max_total_chars` | 3500 | 单次搜索返回的总字数上限 |
| `max_calls_per_minute` | 8 | 同一会话每分钟最多搜几次 |
| `session_max_chars` | 15000 | 同一会话累计注入对话的字数上限（搜索 + 网页阅读） |
| `session_budget_idle_minutes` | 30 | 会话空闲这么久后自动重置累计字数预算 |
| `max_web_read_per_minute` | 5 | 同一会话每分钟最多读几个网页 |
| `web_read_max_chars` | 6000 | 单次网页阅读返回字数上限 |
| `web_read_max_bytes` | 2000000 | 单次网页下载字节上限（流式读取，超出截断） |

累计达到 `session_max_chars` 后不再返回新结果，而是提示 AI 用已有信息收尾作答；
空闲超过 `session_budget_idle_minutes` 后预算自动清零，不会出现"某个群永久搜不了"的问题。

另外做了**正文降噪 `_denoise`** 与**字符清洗 `_sanitise`**：
前者剔除百度百科信息框、B站推荐列表这类"满屏短行"的导航噪声，避免截断时截到一堆无关菜单
（Tavily 的 `chunks_per_source` 参数本可以解决，但它会打乱结果排序，实测不可用，故改为本地处理）；
后者清掉 `U+FFFD`、控制字符与孤立代理项——中文结果页的 emoji 常解成 `\ufffd` 或半个代理对，
不清理就是 `真身曝光!<乱码>` 混进上下文。成对 emoji 会先合成再判断，不会被误删。
（两处的思路移植自 [dsh-keyless-search](../dsh-keyless-search) 的 `sanitiseText` / `dateFromSnippet`）

`_peel_date` 会把摘要开头的发布时间（如 `2025年2月28日 · …`、`4天前 - …`）剥出来标在标题后面，
既让模型能判断时效性（减少它为求新而重复搜索），正文里也不再重复占位。

## 网页阅读的安全与质量

- **SSRF 防护**：`web_read` 只允许 http/https；对每一跳重定向都重新校验目标主机，
  并对域名做 DNS 解析、逐个 IP 判断是否为公网地址（拒绝 loopback / 内网 / 链路本地 /
  保留地址 / IPv4-mapped IPv6），防止被诱导读取本机或内网服务。
- **正文提取**：优先取 `<article>` / `<main>` 里的正文，跳过 `script/style/nav/footer/aside`；
  提取后再走 `_denoise` 降噪，减少菜单、推荐位占用额度。
- **编码兜底**：按响应头 / `<meta charset>` 解码，失败再退 utf-8（宽容）与 GB18030，
  中文老站（GBK）不再乱码。
- **流式限流**：边下边读，超过 `web_read_max_bytes` 立即截断，避免超大页面打爆内存。

## 安装

放到 `AstrBot/data/plugins/astrbot_plugin_free_search/`，管理面板重载插件即可。

## 测试

纯逻辑部分（清洗、降噪、日期剥离、IP 判定、会话闸门、正文提取等）有标准库单测：

```bash
cd AstrBot/data/plugins/astrbot_plugin_free_search
python -m unittest discover -s tests -v
```

## 版本

- **1.3.0**：修复 `web_read` 重定向 SSRF；`web_read` 纳入频率/字数闸门并降噪；流式下载 + 大小上限 + GBK 编码兜底；会话预算空闲重置；缓存 key 纳入参数；结果按 URL 去重；支持 `proxy`；`web_search` 支持 `site` / `time_range` / `max_results`；Tavily 支持 `include_answer`；补充单测。
- 1.2.0：上下文闸门、正文降噪、字符清洗、日期剥离。
