---
name: xhs-webbridge-research
description: 使用 Kimi WebBridge 控制用户真实浏览器（Edge，含小红书登录态）调研小红书：搜索关键词并从搜索页提取笔记卡片（id/xsec_token/标题/作者/日期/点赞/封面），再逐条打开笔记详情页提取正文与笔记图片（图文采集，可把图片下载到本地）。适用于：用户给出关键词要求"搜索/收集/整理小红书笔记"、"看看小红书怎么说/怎么用"、按笔记为单位采集小红书内容（含图文）。触发词：小红书、xhs、xiaohongshu、小红书笔记、采集小红书、小红书调研、小红书图文。
---

# Xhs (小红书) Webbridge Research

用 Kimi WebBridge 驱动用户真实浏览器（复用小红书登录态），从小红书搜索并收集笔记（note）正文。

## 核心结论（先读）

**小红书是图文平台，只采正文会损失一半以上信息**（教程截图/效果图/步骤图都在图片里），必须同时采图片。

**不要用页内 fetch 调小红书接口** —— 搜索接口 `so.xiaohongshu.com`（跨域+CORS 拦截）和笔记详情都需要 x-s 签名（由 `as.xiaohongshu.com` 生成），页内 fetch 直接 `TypeError: Failed to fetch`。**可靠路径是纯 DOM 采集 + 网络抓包**：

1. **搜索**：打开 `search_result?keyword=...`，从 `.note-item` 卡片 DOM 提取 `data-note-id` 和链接 `/search_result/{id}?xsec_token={token}` 中的 **笔记自身 xsec_token**（打开详情页必需）。
2. **正文**：逐条导航到 `/explore/{id}?xsec_token={token}&xsec_source=pc_search`，用 `#detail-desc` 取正文（含标签）。
3. **图片**：详情页 `.swiper-slide img` 是笔记轮播图（swiper 复制 slide 需去重）；图片 URL 直接 curl/页内 fetch 下载会 403（CDN 需登录上下文），可靠下载 = **CDP 禁用缓存 + `network` 抓包读响应体 base64**。

## 前置：WebBridge 操作要点

- 守护进程未运行则先启动：Windows `& "$env:USERPROFILE\.kimi-webbridge\bin\kimi-webbridge.exe" start`。
- **必须用 Edge 浏览器扩展**（不是 Chrome）。守护进程同一时刻只接一个扩展；若 status 显示 `extension_id` 为 Chrome 的 `fldmhceldgbpfpkbgopacenieobmligc`，需用户重启守护进程让 Edge 的 `bnlffdbcfnanfbknnlaflhlhkocccckg` 接管（日志 `<HOME>\.kimi-webbridge\logs\daemon.log(.prev)` 可见"slot held by"）。检查：`curl.exe -s http://127.0.0.1:10086/status` 看 `extension_id`。
- Windows 下禁止用 shell 内联 JSON 发请求（PowerShell 破坏中文与引号）：用文件工具写唯一临时 JSON 文件，再 `curl.exe -s -X POST http://127.0.0.1:10086/command -H "Content-Type: application/json" --data-binary "@<file>"`，用后删除。
- 每个任务固定一个 `session` 名，首次 `navigate` 设用户语言 `group_title`。
- 详见 `references/webbridge-operations.md`。

## 工作流

### 0. 执行目录与数据落点（必读，防止数据散落）

- **脚本一律用绝对路径执行**（本机安装位置，路径含空格须加引号）：
  `python "<SKILL_DIR>/xhs-webbridge-research/scripts\xhs_cli.py" ...`
- **数据根恒定 = 脚本所在目录**：`collect/search/batch/repair` 的 json/imgs 默认落在
  `...\xhs-webbridge-research\scripts\json|imgs`（CLI 内部 HERE 锚定，与调用方工作目录无关）。
- **禁止把 scripts 复制到当前会话/项目目录执行**——复制会让 HERE 指向错误位置，
  采集数据就散落进无辜的项目空间（曾发生，勿再犯）。
- **只有调研报告/交付物（MD/HTML）写当前会话工作目录**；原始数据 json 不落 cwd，
  需要引用时从上面的数据根取。

### 1. 确认登录态
- `navigate` 到小红书后，evaluate 检查页面是否登录（无"登录"按钮、有"消息/我"导航即为登录态）。未登录会看到登录墙，需用户先登录。

### 2. 搜索并提取笔记卡片
没有现成笔记链接、只有调研主题时，先搜索（`CLI` = 上面的绝对路径缩写）：

```bash
CLI collect --query <关键词> --limit 8 [--resume] [--known-ids 清单.tsv] [--filter '{"sort":"最新","note_type":"图文"}']
CLI batch   --keywords-file keywords.txt --limit 8
CLI repair  --json "<数据根>\json\<关键词>.json"
CLI probe | smoke --query <词> | report
CLI verify-sync --other <另一处 xhs_bridge.py 绝对路径>
```

- 流程：导航搜索页 → **登录态校验** → 轮询 `.note-item` 提取卡片（id / **token** / 标题 / 作者 / 日期 / 点赞 / 封面）
  → 逐条打开详情页取 `#detail-desc` 正文 + `.swiper-slide img` 图片 → 抓包下载图片 → **每帖增量落盘**。
- 默认同时下载图片到 `imgs/{关键词}/{note_id}/img_NN.webp`（走 CDP 禁缓存 + network 抓包 base64；绕开 CDN 403）。
- `--resume` 只跳过已 `ok` 的笔记（`empty` / `error` 会重试）。JSON **保存 xsec_token**，补图无需重搜。
- **采集时去重（`--known-ids`）**：清单每行 `note_id<TAB>kw1,kw2`（由探索台在任务启动前从库导出）。
  已知笔记**不打开详情页、不下载图片**：本词下已有 → 静默跳过；跨词出现 → 写 `status:"dedup"`
  精简记录（入库方只补 note_keywords 边）。**`--limit` 只计新笔记**：已知笔记不占配额，
  会继续向搜索页深处找新的，翻完仍不足则提前结束。无此参数时行为向后兼容。
- 默认抓全部渲染的卡片（实测约 29 个 `.note-item`，其中 2 个是「大家都在搜」推荐位，会被自动跳过）；
  `--limit` 控制数量。
- **跑批量前先过冒烟门禁**：`verify-sync` → `probe` → `smoke` → `repair` 各 1 次，全绿再上量。
- **筛选（2026-09-03 新增）**：`collect` 支持 `--filter`，利用搜索页「筛选」面板（5 维度）。
  值可用 UI 文本或英文别名：`sort`(综合/最新/最多点赞/最多评论/最多收藏, 别名 latest/max_like/...)、
  `note_type`(不限/图文/视频, 别名 image/video)、`time`(不限/一天内/一周内/半年内, 别名 1d/1w/6m)、
  `range`(不限/已看过/未看过/已关注)、`pos`(不限/同城/附近, 别名 city/nearby)。
  实机验证：`--filter '{"sort":"最新","note_type":"图文"}'` 后卡片集合确实变化（默认->筛选不同）。
  实现：`xhs_bridge.apply_filters`（点开 `.filter` -> 逐维度点 `.tags` -> 等刷新），
  卡片 DOM 结构不变，`CARD_JS` 无需改动；每次 navigate 后筛选重置，采集时自动重新点选。
- **检索语义（2026-09-04 新增）**：`collect --search-type 51|54` 与 `search --mode notes|topic|users`。
  实机结论（见 `_shots/channel_mapping.json`）：
  - `type=51` 笔记全文检索（默认）；`type=54` **话题模式**（点正文 `#话题` 的落点 = `search_result?keyword=<词>&type=54`，
    DOM 与 51 一致，返回话题聚焦笔记）→ 话题通道 = `collect --search-type 54` 或 `search --mode topic`，零新 DOM。
  - **用户实体通道**：搜索页点 `.channel` 文本「用户」（前端 state，URL 不变）→ `a[href*="/user/profile/"]`
    实体卡片（含**用户级 xsec_token**）→ 进 `/user/profile/{uid}?xsec_token=...` 主页，`.note-item` 与搜索页同结构，
    但**笔记 token 藏在 `/user/profile/{uid}/{nid}?xsec_token=...` 链接**（`USER_NOTES_CARD_JS` 已处理）；无 token 打开详情必 404(300031)。
  - 用法：`search --query <词> --mode users [--limit-users N] [--fetch-notes] [--limit N]`；
    用户主页笔记清单（id/token/title）已可直接用于后续批量采正文/图。
- 字段口径与坑点见 `references/xhs-api.md`（**尤其"502 的正确归因"一节**）。

### 3. 清洗与合规
- 笔记为社区用户原创内容（社区来源，非权威事实），整理时不背书真实性，交付时标注。
- 个别笔记可能打开失败（404/风控/空正文），脚本会记 `status: empty/error`，如实说明即可，不强行凑数。
- 遇违规内容（如 NSFW 教程）不收录正文，交付物标注原因。

### 4. 整理与交付
- 按主题归类笔记（安装教程/账号额度/接入第三方/使用技巧/避坑经验/官方动态等）。
- 产出可阅读交付物（HTML/Markdown）：关键词概况、高赞 Top N、主题导航、每条含作者/点赞/日期/全文。
- **交付物写当前会话工作目录**；原始采集 JSON 留在数据根（`scripts\json\`），引用时给绝对路径。
- 用 `present_files` 交付；附原始 JSON 路径。

## 资源

- **`scripts/xhs_bridge.py`**：核心模块（唯一真源）。结构化错误解析、`ensure_tab` 探活与重建、
  `Capture` 抓包上下文（正确顺序）、图片下载（稳定下标 + 原子写）、`NoteWriter` 增量落盘、采集/补图编排。
  skill 目录与项目目录各存一份，**必须字节级一致**（用 `verify-sync` 校验 sha256）。
- **`scripts/xhs_cli.py`**：命令行薄壳（子命令 `probe` / `smoke` / `collect` / `search` / `repair` / `batch` /
  `verify-sync` / `report`）。不含任何网络与解析逻辑，两处可完全一致。
- `references/xhs-api.md`：DOM 结构、xsec_token 机制、图片下载原理与坑点
  （**含"502 的正确归因"与"守护进程进程退出"两节，务必先读**）。
- `references/webbridge-operations.md`：Kimi WebBridge 启动、Edge/Chrome 扩展归属、tab 定位、Windows 文件体请求。
- `_deprecated/`：旧版 4 个脚本（`xhs_collect_notes.py` / `xhs_repair_imgs.py` /
  `xhs_batch_notoken_download.py` / `xhs_collect_notes_v2.py`），**已废弃，只读留档，禁止再拷贝**。

## 已知坑（务必遵守）

1. **xsec_token 有两种**：`note_card.user.xsec_token` 是**作者**的 token（用于作者主页），打开笔记要用**笔记自身** token（在卡片链接 `/search_result/{id}?xsec_token=...` 里）。用错 token 打开详情页会 404 风控（error 300031）。
2. **`/explore/{id}` 不带 token 必 404**；必须带 `?xsec_token={token}&xsec_source=pc_search`。
3. **页内 fetch 小红书接口/图片一律失败**（CORS/签名/混合内容），走 DOM + network 抓包。
4. **图片下载四要素，顺序不能变**（旧文档的顺序是错的，会导致全链路 502）：
   ① `navigate` 绑定 tab（或 `ensure_tab()` 探活）→ ② `cdp Network.enable` + `Network.setCacheDisabled`
   （否则图片走磁盘缓存不产生请求，抓包为空）→ ③ `network start` 后等 ~1.2s
   → ④ **再** `navigate` 到详情页 → ⑤ 页内新建 `Image()` 强制请求全部轮播图（绕开懒加载，只有可见 slide 才会自然加载）。
   **`network start` 放在 `navigate` 之前是全链路 502 的头号原因。**
5. **抓包失败必须抛异常，绝不能吞**：缓冲有限且会覆盖；`list`/`detail` 返回 `ok:false` 时若吞成 `(0, [])`，
   "抓包失败"与"真的是 0 条"就无法区分（旧版 `net_list` 的缺陷，曾导致图片静默为 0 且无法诊断）。
   缓冲仅在抓包窗口内有效，不要依赖它做长窗口采集。
6. **"502" 不等于"该重启了"**：守护进程把业务错误 `{"ok":false,"error":{...}}` 以 **HTTP 502** 返回，
   **真实原因在响应体 `error.message` 里**，必须 `e.read()` 读 body 解析，
   **禁止**按异常字符串是否含 `'502'` 判断重试（旧脚本因此把真实原因吞掉了）。
   绝大多数 502 是「会话未绑定 tab」（`has no tab` / `tab was closed`），此时 **`navigate` 即可自愈**，
   重启反而清空会话表让情况更糟——这正是"重启后仍未恢复"的真正原因。
   真正的进程级故障只有两类：① Edge 关闭导致守护进程退出；② 由 agent 的 shell 启动时被父进程 job 回收
   （`start` 打印 pid 后 1 秒内进程消失，须挂长驻父进程如 `start; sleep 3600`）。
   **大采集必须分段跑 + 每帖 `tab_alive()` 探活**，用 `--resume` 续跑。
7. **`network detail` 在正确顺序下 100% 可用**（实测 25/25、14/14，266KB base64 亦成功）。
   旧文案"读 body 有概率为空，不要死磕"是误判。空 body = 顺序错或 tab 丢失，
   `ensure_tab()` 后重取 1 次即可；不要盲重试，也不要放弃。
8. **混合内容**：个别图片 URL 是 `http://`（非 https），在 https 页面新建 `Image()` 会被浏览器拦截；强制加载前先 `url.replace('http://','https://')`。
9. **网页端搜索无分页控件**（`btn-next` 是筛选标签），每关键词约 29 个 `.note-item`
   （其中约 27 条真笔记 + 2 个「大家都在搜」推荐位，见坑 13）；如需更多结果换关键词/筛选词补充。
10. **evaluate 里 `const` 跨调用重复声明报错**，包装 `(() => {...})()`。
11. **同 tab 导航**（不加 newTab）保持上下文。**全程复用单一 session 的单一 tab**；
    tab 丢失时只在同一 session 内 `navigate` 重建，**绝不新建 session、绝不多开 tab**。
12. **CDP `Input.dispatchMouseEvent`（滚轮）可能挂起**，不要用来触发滚动加载。
13. **搜索结果混有「大家都在搜」推荐位**（约每 10 张一次，实测落在位置 6/16/28）：
    `data-note-id` 形如 `<uuid>#<时间戳>`、**`href` 为空、无 xsec_token、无标题**。
    **判定无 token 就必须跳过并记 `status:"no_token"`，绝不能记 `empty`。**
    旧版本把它们当笔记打开（空 token → 风控空页），这正是"每个关键词第 7 条恒失败"的真正原因
    ——不是随机、不是超时、也不是笔记真的打不开。

## v1.7.0 采集能力增强（2026-09-15，实测）

核心：详情采集主线从"抓 DOM"升级为**读页面内嵌 `window.__INITIAL_STATE__`**（Vue ref 须 `.value`、禁整体 stringify 用 WeakSet 避循环）——零额外导航、零签名逆向，字段更全更稳。

- **详情富字段（collect 默认产出，向后兼容只加可选键）**：`shares/collects/comments_count`（`shareCount` 仅 state 有、DOM 无）、`publish_ts/publish_date`（精确时间戳；**卡片 date 常与 state.time 不一致，以 state 为准**）、`ip`、`hashtags[{id,name}]`（结构化话题带 topic_id）、`image_meta`（原图 WB_DFT+尺寸）、`author_xsec_token`、视频 `note_type/video{duration,cover_fileid}`。`DETAIL_JS` 降为 state 不可用时兜底。
- **`suggest --query X`（新子命令，三家开源仓库均无此层）**：联想词 `suggestions` + 猜你想搜 `queryTrendingInfo.queries`（`type` 含 `q2qRelatedAggQuery`/`trendingQueryEntityHot` 溯源，按 type 分桶取相关词）+ `aiWords` + `hintWord`。热搜须 `#search-input` 聚焦+原生 setter 清空+派 input 事件触发。
- **`--with-comments [--max-comments N]`（collect/search/batch，opt-in）**：评论在 `noteDetailMap[id].comments{list,cursor,hasMore}`，**滚动重读 state 翻页、`hasMore=false` 即到底**（滚动容器 `.note-scroller`，非 window/#noteContainer/.comments-el）；点父评论 `.show-more`"展开N条回复"回填 `subComments` 补齐全量；含逐条 IP/点赞。
- **`search --mode users --with-profile`**：用户资料卡 `user_profile{red_id,ip_location,desc,gender,region,follows,fans,gets_and_collects}`（数值"万"缩写归一，获赞与收藏合并计数）。
- **图片直连为主**：`image_meta.urlDefault` 自带签名，urllib 直连即 200（免 cookie/referer），CDP 抓包降为 403/过期兜底。

> 详细字段口径、三仓代码级调研、设计契约见项目 `D:\Data\Documents\XHS_P\docs\`（探测矩阵 v0 / 竞品调研 / 规范化设计 v1 / 同步文档 v1.7.0）。本 skill 的 `references/xhs-api.md` 尚未并入这些细节。

> 版本：`MODULE_VERSION` 1.6.2 → **1.7.0**；改 `xhs_bridge.py`/`xhs_cli.py` 仍须双副本 `verify-sync` 字节一致（本 skill 与项目根各一份）。
