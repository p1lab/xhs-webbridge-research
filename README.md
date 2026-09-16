# xhs-webbridge-research

小红书（Xiaohongshu / RedNote）调研采集 —— 用 **Kimi WebBridge 驱动真实浏览器（复用登录态）**，按关键词搜索并采集笔记：卡片、正文、**内嵌 `__INITIAL_STATE__` 富字段**、评论（含楼中楼）、用户资料、选题词源（联想/热搜），并下载图片。

> 面向个人研究/学习。非官方，依赖站点结构，改版可能失效。

## 前置依赖
- **[Kimi WebBridge](https://www.kimi.com/products/kimi-webbridge)** 守护进程（本机 `127.0.0.1:10086`）+ 其浏览器扩展。本工具**不自带**该 daemon，需另行安装。
- 一个**已登录小红书的真实浏览器**（推荐 Edge；daemon 同刻只绑一个扩展）。
- Python 3（标准库为主，图片直连下载用 `urllib`）。

## 设计要点（核心结论）
- 小红书是图文平台，**只采正文会损失一半信息**，必须同时采图片。
- **不要页内 fetch 调私有接口**（跨域 + `x-s` 签名会 `Failed to fetch`）。可靠路径 = **读页面内嵌 `window.__INITIAL_STATE__`（主）+ DOM 兜底 + 图片 CDN 直连/抓包**。
- **零签名逆向**：复用登录态浏览器，不破解接口签名。

## 能力（`xhs_cli.py` 子命令）
`probe` / `smoke` / `collect` / `search` / `suggest` / `repair` / `batch` / `verify-sync` / `report`
- `collect --query 关键词 [--limit] [--filter] [--search-type 51|54] [--with-comments] [--with-trend]`
- 详情富字段（默认，向后兼容）：`shares/collects/comments_count`、`publish_ts/publish_date`(精确时间戳)、`ip`、`hashtags[{id}]`(结构化话题)、`image_meta`(原图)、`author_xsec_token`、视频 `note_type/video`
- `suggest --query`：联想词 + 猜你想搜/热搜（带来源 `type`）+ AI 问句
- `search --mode users --with-profile`：用户资料卡
- `--with-comments`：滚动 state 翻页 + 展开子回复，含逐条 IP

## 目录
```
SKILL.md  references/  scripts/{xhs_bridge.py(核心·唯一真源), xhs_cli.py(薄壳)}
```
`xhs_bridge.py` 若在多处部署须字节一致：`python xhs_cli.py verify-sync --other <另一份 xhs_bridge.py>`。

## 合规
仅个人研究/学习；遵守目标站 ToS 与 robots；内容为社区用户原创、不背书真实性、不得用于商业/批量抓取；请勿用于骚扰或侵犯隐私。采集数据（`json/`、`imgs/`）默认被 `.gitignore` 排除、不入库。

## 许可
MIT（见 LICENSE）。
