# 小红书接口与页面结构说明

在已打开的 `www.xiaohongshu.com` 搜索页/笔记页上，用 **DOM 采集**（不用页内 fetch）。

> **v1.7.0 数据源优先级**：详情/搜索联想/用户资料**主源改为读页面内嵌 `window.__INITIAL_STATE__`**（`evaluate` + `.value` 解包，Vue ref，禁整体 stringify 用 WeakSet 避循环），**DOM 提取器（CARD_JS/DETAIL_JS/USER_PROFILE_JS）降为兜底**；图片**主源为 `urlDefault` 直连**，CDP 抓包兜底。字段与机制详见文末 **§六**。以下 §一–§五的 DOM 口径仍作为兜底路径有效。

## 一、搜索

### 搜索页 URL
```
https://www.xiaohongshu.com/search_result?keyword={urlencoded}
```
导航后 URL 会自动追加 `&type=51`（综合）。

### 真实搜索接口（仅了解，勿依赖）
```
POST https://so.xiaohongshu.com/api/sns/web/v2/search/notes
```
- 跨域（so 域名）+ 需 x-s/x-t 签名（由 `as.xiaohongshu.com/api/sec/v1/ds` 生成），**页内 fetch 报 `TypeError: Failed to fetch`**。
- 页内不可用；`network start` 抓包可读到浏览器真实请求的完整响应体（含 22 条/页的 note 卡片、has_more），但缓冲易失效，不作为核心采集路径。
- 每次搜索页面通常发 2 个 POST（综合 + 实时/其他排序），返回不同笔记集合。

### 卡片 DOM（核心采集路径）
```
<section class="note-item" data-note-id="{24位hex}">
  <a class="cover mask" href="/search_result/{note_id}?xsec_token={笔记token}&xsec_source=">
  <a class="title" href="/search_result/{note_id}?xsec_token={笔记token}"><span>标题</span></a>
  <div class="card-bottom-wrapper">
    <a class="author" href="/user/profile/{user_id}?...&xsec_token={作者token}">作者</a>
    ... 点赞数（卡片文本最后一个数字）
  </div>
</section>
```
- **笔记 id**：`data-note-id` 或 `/search_result/{id}`；格式有 **24 位 hex** 与 **UUID** 两种，
  正则须同时兼容：`/([0-9a-f]{24}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i`。
- **⚠️ 搜索结果里混有「大家都在搜」推荐位**（约每 10 张一次，实测落在位置 6/16/28）：
  `data-note-id` 形如 `<uuid>#<时间戳>`、**`href` 为空、无 xsec_token、无标题**。
  它不是笔记；打开 `/explore/<uuid>#ts?xsec_token=`（空 token）必然得到空页。
  **判定 `has_token` 为假就必须跳过，记 `status:"no_token"`，绝不能记 `empty`。**
  （旧版本把它们记成 `empty`，这是"每个关键词第 7 条恒失败"的真正原因——不是随机，不是超时。）
- **笔记自身 xsec_token**：卡片链接 `/search_result/{id}?xsec_token={token}`（`&amp;` 是 HTML 转义，JS 里 `getAttribute('href')` 返回真实 `&`）。
- **作者 token** 在 `/user/profile/{id}?xsec_token={作者token}`，用于作者主页，**不是**打开笔记用的 token。
- 标题：`a.title span`；作者+日期：`a.author` innerText（换行分隔）；点赞：卡片 innerText 最后一个数字。

## 二、笔记详情页

### 详情 URL（必须带笔记 token）
```
https://www.xiaohongshu.com/explore/{note_id}?xsec_token={笔记token}&xsec_source=pc_search
```
- **不带 token 或带错 token（用了作者 token）→ 404 风控页**（error_code 300031 "当前笔记暂时无法浏览"，跳 `/404?source=/404/sec_...`）。
- 用正确的笔记 token 即可正常渲染（无需额外签名，登录态浏览器内直接看）。

### 正文 DOM
- 正文选择器：`#detail-desc` → `innerText`（含正文与 `#标签`）。
- 标题：`.title`；另有页面级交互数据（点赞/收藏/评论）在详情页 DOM。

### 图片 DOM 与下载
- **笔记轮播图**：`.swiper-slide img` 的 `src`（swiper 会复制 slide，需按 URL 去重）。
- **图片 URL 不可直接 curl/页内 fetch 下载**（CDN `sns-webpic-qc.xhscdn.com` 返回 403，需登录上下文；页内 fetch 报 `Failed to fetch`）。
- **可靠下载**（`xhs_bridge.py` 的 `Capture` 已封装，**勿自行拼顺序，顺序错了全链路 502**）：
  1. **先 `navigate` 绑定 tab**（或用 `ensure_tab()` 探活）—— 这一步在最前面。
  2. `cdp Network.enable` + `cdp Network.setCacheDisabled {cacheDisabled:true}` —— 否则图片走磁盘缓存不产生网络请求。
  3. `network start` → 等 ~1.2s。
  4. **再** `navigate` 到详情页（同 tab）。
  5. 页内用 `new Image(); im.src = url + '?f=...'` 强制请求全部轮播图（绕开懒加载）。
  6. `network list`（无 filter，Python 端按 URL 含 `webpic` 过滤）→ 按 **URL 原始下标**匹配
     → `network detail` 取 `base64Encoded` body → 先写 `.part` 再 `os.replace` 落盘。
- **混合内容**：个别图 URL 是 `http://`（非 https），`Image()` 强制加载前必须 `url.replace('http://','https://')`，否则被 https 页面拦截、请求不发出。
- **无 token 也能下载**：笔记图是公开 CDN URL，在登录态搜索页直接 `new Image()` 强制请求即可触发抓包
  （旧脚本 `xhs_batch_notoken_download.py` 已废弃；现由 `xhs_cli.py repair` 统一处理：
  重新搜索取 token → 带 token 导航详情页 → 抓包下载）。
- **`network detail` 读 body**：在**正确顺序**下 **100% 可用**（实测 25/25、14/14，266KB base64 亦成功）。
  空 body 不是随机缺陷，而是"顺序错了"或"tab 已丢失"的症状 —— 先 `ensure_tab()` 再重取 1 次即可。
  旧文案"有概率为空，不要死磕"是误判，它让人放弃修真正的 bug。
- 网络抓包缓冲有限且会被覆盖；`list`/`detail` 返回 `ok:false` 时 **必须抛出结构化异常**，
  **绝不能吞成 `(0, [])`** —— 否则"抓包失败"与"真的是 0 条"无法区分（旧版 `net_list` 的缺陷）。

## 三、分页与数量

- **网页端搜索无分页**：`btn-next` 是筛选标签（综合/实时等），不是"下一页"；滚动不触发更多加载。实测每关键词约渲染 27 张卡片（≈1 页 API 22 条 + 相关）。
- 需要更多结果：换关键词、换筛选词（如 `codex 教程`、`codex 避坑`）、或组合多个关键词后去重。
- 搜索接口响应含 `has_more: true` 但网页 UI 不暴露翻页；`network` 抓包可拿到最多 2 批（综合+实时）约 40 条，但无笔记 token（token 只在 DOM），无法直接打开。

## 四、守护进程稳定性（实测重要）

### ⚠️ "502" 是被复用的状态码，不等于同一种故障

守护进程把业务错误 `{"ok":false,"error":{"code":"tool_error","message":"..."}}` 以 **HTTP 502** 返回，
**真实原因在响应体里**。必须 `e.read()` 读 body 再解析 `error.code` / `error.message`；
**禁止**按异常字符串里是否含 `'502'` 来判断重试 —— 旧脚本正是这么写的，导致真实原因被吞成
一句 `HTTP Error 502: Bad Gateway`，进而得出"通道整体退化"的错误结论。

**最常见的 502 = 会话未绑定 tab**，两种文案：

- `session "X" has no tab — navigate or find_tab first`
- `session "X" tab was closed — navigate first to recreate`

此时 `cdp` / `network start` / `list` / `detail` **全部 502**，但 **`navigate` 仍返回 200 且会重建 tab**。
→ **自愈办法是 `navigate`，不是重启守护进程。**
（旧文档误判为"通道被打挂，只能重启"，这正是"重启后仍未恢复"的真正原因：
重启清空了会话表，脚本不重新 navigate 就继续调用，于是每次都 502。）

**`cdp_disabled` 也不是版本问题**：tab 绑定后 `Network.enable` / `Network.setCacheDisabled` 均返回 ok。
遇到它先查 tab 绑定，不要去折腾版本。

### 守护进程进程本身会退出（另两类故障）

1. **Edge 关闭后守护进程随之退出**（日志末条常是 `[session] ... stale tab ... removing from session`）。
2. **由 agent 的 shell 启动时会被父进程 job 回收**：表现为 `start` 打印
   `daemon started (pid N)` 并在 1 秒内进程消失，`/status` 随即 connection refused。
   → **启动时要挂一个长驻父进程**（如后台执行 `kimi-webbridge start; sleep 3600`），否则根本起不来。
   日志停在 `[agent-extension-daemon] listening on 127.0.0.1:10086` 没有下文，就是被回收了。

重启仍须谨慎：`& "$env:USERPROFILE\.kimi-webbridge\bin\kimi-webbridge.exe" restart`；
重启后检查 `/status` 的 `extension_id` 仍为 Edge（`bnlffdbcfnanfbknnlaflhlhkocccckg`），Chrome 可能抢槽。

**应对**：大采集分段跑，用 `--resume` 续跑；每帖前 `tab_alive()` 探活（1 条命令），
失效则 `ensure_tab()` 在同 session 内重建；连续 3 帖致命失败即中止，已落盘成果完整保留。

## 五、登录态

- 未登录访问搜索页 = 登录墙（body 极短、多处"登录"按钮、无结果）。
- 依赖 Edge 浏览器登录态；打开详情页需登录。

## 六、v1.7.0 一级 API 数据源与字段口径（实测代码 `[可验证]`）

依赖方式不变：**kimi-webbridge daemon（127.0.0.1:10086）驱动真实 Edge、复用登录态**；纯 `evaluate`/DOM/CDP 抓包，**零签名逆向**。v1.7.0 只是把"读 `__INITIAL_STATE__`"提为主源。

### 6.1 详情富字段（`read_note_state` → `note.noteDetailMap[id].note`；collect 默认产出，只加可选键）
| 键 | 来源(state) | 说明 |
|---|---|---|
| `likes_int/collects/comments_count/shares` | `interactInfo.{liked,collected,comment,share}Count` | **shareCount 仅 state 有、DOM 不显示** |
| `publish_ts/publish_date/publish_iso/update_ts` | `time`/`lastUpdateTime`(epoch ms) | 精确时间；**卡片 date 常与 state.time 不一致，以 state 为准** |
| `ip` | `ipLocation` | 笔记 IP 属地 |
| `note_type` | `type` | normal/video |
| `hashtags` | `tagList[]`→`{id,name,type}` | **结构化话题带 topic_id** |
| `image_meta` | `imageList[]`→`{w,h,urlDefault,urlPre,scenes[WB_PRV/WB_DFT]}` | 原图 + 尺寸 |
| `at_users` / `author_xsec_token` | `atUserList` / `user.xsecToken` | @提及 / 作者级 token |
| `video` | `video.capa.duration` + `video.image.firstFrameFileid/thumbnailFileid` | 时长(秒) + 封面 fileid |

`DETAIL_JS` 命中 state 时降为兜底；`content/images` state 优先、`status` 判定沿用旧逻辑（回归安全）。

### 6.2 评论（`collect_comments`，`--with-comments` opt-in）
评论首屏即在 `note.noteDetailMap[id].comments{list,cursor,hasMore,loading}`。翻页 = **滚动容器 `.note-scroller` 触发站点懒加载后重读 state**（window/#noteContainer/.comments-el 均不可滚→假到底），`hasMore=false` 为权威到底。子回复 = 点 `.show-more`"展开N条回复"，站点回填 `subComments`（实机 1→3、可补齐到 `comments_count` 全量）。每条 `{cid,author,author_id,content,ip,create_time→date,likes,is_author,replies[]}`。

### 6.3 用户资料卡（`read_user_profile`，`search --mode users --with-profile`）
`.user-info` → `user_profile{red_id, ip_location, desc, gender, region, follows, fans, gets_and_collects}`。数值"万/亿"缩写经 `norm_count` 归一；获赞与**收藏为合并计数**（非分开）。

### 6.4 选题词源（`suggest`，三家开源仓库均无此层）
读 `search.suggestions`（联想词，进页即在 state）；热搜须**触发**——`#search-input` focus + 原生 setter 清空 + 派 `input` 事件，才填 `queryTrendingInfo`：`queries[]`（`type` 含 `q2qRelatedAggQuery`/`trendingQueryEntityHot` 溯源，按 type 分桶取相关词）+ `aiWords[]`（AI 问句）+ `hintWord`。`oneboxInfo/searchHotSpots` 常规图文域常空，不纳入。

### 6.5 图片下载（`download_images_dc`）
主：`image_meta.urlDefault` **自带签名，`urllib` 直连即 200/webp，免 cookie/referer**（去签名 base 才 403），按 `Content-Type` 定扩展名。兜底：直连失败(403/过期)才回退 `download_images` 的 CDP 抓包四要素（§二）。

### 6.6 归一化 / 解包规则
- state 值是 Vue ref，须 `.value`；部分键循环引用，**逐键 unwrap + WeakSet + 白名单叶子，禁整体 `JSON.stringify`**（否则 `Converting circular structure to JSON`）。
- `norm_count`：`'4,305'→4305`、`'1.6万'→16000`、`'10.2万'→102000`、`''/占位→None`。
- `epoch_ms_to_iso`：ms→`+08:00` ISO；`publish_date` 取前 10 位。

### 6.7 CLI 面（`xhs_cli.py`，向后兼容）
`probe / smoke / collect / search / suggest / repair / batch / verify-sync / report`。新增：`collect|search|batch` 加 `--with-comments [--max-comments N]`；`search --mode users` 加 `--with-profile`；新子命令 `suggest --query X`。不带新参数时产物与 v1.6.2 逐字段一致。

### 6.8 不可得 / 不做（实测）
搜索卡上的收藏/评论数（卡片只有点赞）、评论排序（web 无 UI）、视频字幕轨、视频文件 stream 直下（`media.stream.EF4~7` 开页空数组、需播放器 init/媒体签名）、话题参与数（type=54 落地无额外头部）、onebox/热点（常规域空）。与"零签名"原则一致，维持不走逆向 API。

### 6.9 版本与双副本
`MODULE_VERSION` 1.6.2 → **1.7.0**。真身两处独立文件：项目根 `XHS_P` 与 豆包 skill `scripts/`（QwenWork skill 目录是 junction→豆包，自动同步）；改 `.py` 后 `verify-sync` 须两处 sha 一致。
