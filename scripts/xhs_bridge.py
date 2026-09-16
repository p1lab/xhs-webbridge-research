# -*- coding: utf-8 -*-
"""
xhs_bridge.py -- 小红书 WebBridge 采集核心模块（唯一真源）

本模块在两处字节级一致：
  A. skill: <HOME>\\AppData\\Local\\AGENT_HOME\\User Data\\Default\\<AGENT_SKILLS>\\
            agent_mode\\workspace\\.user_skills\\xhs-webbridge-research\\
  B. project: D:\\Data\\Documents\\XHS_P\\
用 `xhs_cli.py verify-sync --other <另一处 xhs_bridge.py>` 校验 sha256。

设计前提（均为 2026-09-03 实机探针结论，改动前请先读 `诊断发现_2026-09-03.md`）：
  F1 守护进程把业务错误以 HTTP 502 返回，真实原因在响应体 error.code / error.message。
     按异常字符串含 '502' 判断重试会吞掉真实原因 —— 本模块禁止这种写法。
  F2 会话未绑定 tab 时 cdp / network start / list / detail 全部 502；
     但 navigate 仍返回 200 且会重建 tab。文案：'has no tab' / 'tab was closed'。
  F3 正确调用顺序：ensure_tab -> cdp enable/setCacheDisabled -> network start -> navigate 详情页。
  F4 network detail 在正确顺序下 100% 可用（实测 25/25、14/14，266KB base64 亦成功）。
  F5 卡片 id 有 24-hex 与 UUID 两种；仅匹配 24-hex 会导致 token 为空 -> 空 token 风控页 -> 误记 empty。
"""
import base64
import datetime
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

MODULE_VERSION = "1.7.1"

# ---------------------------------------------------------------- 筛选（2026-09-03）
# 小红书 PC 搜索页筛选面板：5 个维度（顺序固定）。filters dict 的值用 **UI 文本**
# （与面板 .tags 文本一致），apply_filters 按文本点击选项；parse_filters 会把英文别名
# 归一为 UI 文本（二级程序/命令行可用别名，也可直接用中文）。
FILTER_DIMS = {
    "sort":      {"title": "排序依据", "default": "综合", "options": ["综合", "最新", "最多点赞", "最多评论", "最多收藏"]},
    "note_type": {"title": "笔记类型", "default": "不限", "options": ["不限", "视频", "图文"]},
    "time":      {"title": "发布时间", "default": "不限", "options": ["不限", "一天内", "一周内", "半年内"]},
    "range":     {"title": "搜索范围", "default": "不限", "options": ["不限", "已看过", "未看过", "已关注"]},
    "pos":       {"title": "位置距离", "default": "不限", "options": ["不限", "同城", "附近"]},
}

# 英文/请求体 tags 别名 -> UI 文本（请求体 tags 值见 _shots/filter_mapping.json 实测）
FILTER_VALUE_ALIAS = {
    "sort":      {"general": "综合", "latest": "最新", "time_descending": "最新",
                  "popularity_descending": "最多点赞", "max_like": "最多点赞",
                  "comment_descending": "最多评论", "max_comment": "最多评论",
                  "collect_descending": "最多收藏", "max_collect": "最多收藏"},
    "note_type": {"all": "不限", "image": "图文", "普通笔记": "图文",
                  "video": "视频", "视频笔记": "视频"},
    "time":      {"all": "不限", "1d": "一天内", "1w": "一周内", "6m": "半年内"},
    "range":     {"all": "不限", "seen": "已看过", "unseen": "未看过", "followed": "已关注"},
    "pos":       {"all": "不限", "city": "同城", "nearby": "附近"},
}

DEFAULT_BASE = "http://127.0.0.1:10086/command"
DEFAULT_SESSION = "xhs-research"

# F2 两种 tab 未绑定文案
NO_TAB_PATTERNS = ("has no tab", "tab was closed")
# 连接类瞬时错误
CONN_PATTERNS = ("refused", "10054", "10061", "timed out", "timeout", "connection",
                 "reset by peer", "temporarily unavailable")
# Windows 文件名非法字符（'肉腿/肉肉腿' 含 '/'）
ILLEGAL_NAME_CHARS = r'[\\/:*?"<>|]'

VERBOSE = True


def log(*a):
    if VERBOSE:
        print("[%s]" % time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def safe_name(s):
    """清洗成合法 Windows 文件名（'/'' 等 -> '_'），空格保留。"""
    return re.sub(ILLEGAL_NAME_CHARS, "_", (s or "").strip())


# ---------------------------------------------------------------- 错误模型

class BridgeError(Exception):
    """守护进程/客户端错误。kind 决定是否可自愈。"""

    def __init__(self, code, message, kind, http_status=None, raw=""):
        super().__init__("%s [%s] %s" % (code, kind, message))
        self.code = code
        self.message = message
        self.kind = kind          # no_tab | transient | fatal | rate_limited
        self.http_status = http_status
        self.raw = raw


def classify(code, message):
    """按 F1/F2 把错误归类。no_tab 与 transient 可自愈，fatal 一次即抛。"""
    m = (message or "").lower()
    if any(p in m for p in NO_TAB_PATTERNS):
        return "no_tab"
    if any(p in m for p in CONN_PATTERNS):
        return "transient"
    if code in ("tool_error", "internal", "internal_error", "busy"):
        return "transient"
    if code in ("rate_limited", "forbidden", "risk_control"):
        return "rate_limited"
    return "fatal"


# ---------------------------------------------------------------- Bridge

class Bridge(object):
    """对守护进程的薄封装：结构化错误 + tab 生命周期管理。"""

    def __init__(self, base=DEFAULT_BASE, session=DEFAULT_SESSION):
        self.base = base
        self.session = session
        self.cmd_count = 0

    # -- 核心 ----------------------------------------------------------
    def post(self, action, args=None, timeout=90, retries=3):
        payload = {"session": self.session, "action": action, "args": args or {}}
        body = json.dumps(payload).encode("utf-8")
        last = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(
                    self.base, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                data = json.loads(raw)
                if not data.get("ok"):
                    err = data.get("error") or {}
                    code = err.get("code") or "unknown"
                    msg = err.get("message") or raw[:200]
                    raise BridgeError(code, msg, classify(code, msg), 200, raw[:200])
                self.cmd_count += 1
                return data.get("data") or {}

            except BridgeError as e:
                last = e
            except urllib.error.HTTPError as e:
                raw = ""
                try:
                    raw = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                code, msg, parsed = "http_%s" % e.code, raw[:300], False
                if raw:
                    try:
                        err = (json.loads(raw).get("error") or {})
                        code = err.get("code") or code
                        msg = err.get("message") or msg
                        parsed = True
                    except Exception:
                        pass
                # F1：真实原因在响应体里；解析不出来才退回按状态码判断
                kind = (classify(code, msg) if parsed
                        else ("transient" if e.code in (502, 503, 504) else "fatal"))
                last = BridgeError(code, msg, kind, e.code, raw[:200])
            except Exception as e:
                msg = str(e)
                kind = "transient" if any(p in msg.lower() for p in CONN_PATTERNS) else "fatal"
                last = BridgeError("client_error", msg, kind, None)

            # 仅可自愈类才重试
            if last is not None and last.kind in ("no_tab", "transient") and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        raise last

    def evaluate(self, code, timeout=90):
        d = self.post("evaluate", {"code": code}, timeout=timeout)
        v = d.get("value")
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return v
        return v

    def cdp(self, method, params=None, fatal=False):
        try:
            return self.post("cdp", {"method": method, "params": params or {}},
                             timeout=30, retries=2)
        except BridgeError as e:
            if fatal:
                raise
            log("  [warn] cdp %s 失败（%s/%s）: %s"
                % (method, e.code, e.kind, e.message[:70]))
            return None

    def navigate(self, url, wait_selector=None, timeout=12):
        self.post("navigate", {"url": url}, timeout=60)
        if wait_selector:
            self.wait_for_selector(wait_selector, timeout)

    def wait_for_selector(self, selector, timeout=12, interval=0.8):
        js = "(function(){var e=document.querySelector(%s);return e?1:0;})()" % json.dumps(selector)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(js, timeout=15) == 1:
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    # -- tab 生命周期（F2/F3） ------------------------------------------
    def tab_alive(self):
        try:
            self.post("evaluate", {"code": "1+1"}, timeout=15, retries=1)
            return True
        except Exception:
            return False

    def ensure_tab(self, bind_url=None):
        """tab 存活则返回 False；否则用 navigate 在同 session 内重建并返回 True。
        绝不新建 session、绝不开第二个 tab。"""
        if self.tab_alive():
            return False
        if not bind_url:
            return False
        log("  tab 丢失，重建中（同 session %s）..." % self.session)
        self.post("navigate", {"url": bind_url}, timeout=60, retries=2)
        time.sleep(2.5)
        return True

    def check_login(self):
        """返回 (is_login, info)。"""
        js = ("(function(){var t=document.body?document.body.innerText:'';"
              "return JSON.stringify({len:t.length,"
              "hasWall:/请登录|登录后查看|登录并查看/.test(t),"
              "hasMine:/我的|消息|我 /.test(t),"
              "notes:document.querySelectorAll('.note-item').length});})()")
        try:
            d = self.evaluate(js, timeout=20)
            if isinstance(d, dict):
                return (not d.get("hasWall")) and (d.get("hasMine") or d.get("notes", 0) > 0), d
        except Exception as e:
            return False, {"err": str(e)[:80]}
        return False, {}


class Capture(object):
    """抓包窗口。构造时完成 F3 正确顺序，提供 list()/detail()。"""

    def __init__(self, bridge, url, bind_url=None, wait_selector="#detail-desc"):
        self.b = bridge
        bridge.ensure_tab(bind_url or url)
        bridge.cdp("Network.enable")
        bridge.cdp("Network.setCacheDisabled", {"cacheDisabled": True})
        try:
            bridge.post("network", {"cmd": "start"}, timeout=60)
        except BridgeError as e:
            if e.kind == "no_tab":
                bridge.ensure_tab(bind_url or url)
                bridge.post("network", {"cmd": "start"}, timeout=60)
            else:
                raise
        time.sleep(1.2)
        bridge.navigate(url, wait_selector=wait_selector, timeout=12)

    def list(self):
        """不再吞异常（原 net_list 会把失败伪装成 0 条）。"""
        d = self.b.post("network", {"cmd": "list"}, timeout=60, retries=2)
        return d.get("requests") or []

    def detail(self, request_id):
        d = self.b.post("network", {"cmd": "detail", "requestId": request_id},
                        timeout=60, retries=2)
        body = d.get("body")
        if not body:
            return None, d.get("mimeType") or ""
        if d.get("base64Encoded"):
            return base64.b64decode(body), d.get("mimeType") or ""
        return (body.encode("latin1") if isinstance(body, str) else body), d.get("mimeType") or ""


# ---------------------------------------------------------------- 提取 JS

CARD_JS = r"""
(() => {
  const cards = [];
  const seen = new Set();
  // F5：兼容 24-hex 与 UUID 两种 note id
  const RE = /\/search_result\/([0-9a-f]{24}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\?xsec_token=([^&]+)/i;
  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const HEX24_RE = /^[0-9a-f]{24}$/i;
  document.querySelectorAll('.note-item').forEach(card => {
    const link = card.querySelector('a[href*="/search_result/"]');
    const href = link ? (link.getAttribute('href') || '') : '';
    const m = href.match(RE);
    const rawId = card.getAttribute('data-note-id') || '';
    // 推荐位（"大家都在搜"）的 data-note-id 形如 '<uuid>#<ts>'，且 href 为空、无 token
    const cleanId = rawId.split('#')[0];
    const id = cleanId || (m ? m[1] : '');
    if (!id || seen.has(id)) return;
    seen.add(id);
    const token = m ? m[2] : '';
    const txt = (card.innerText || '').replace(/\s+/g, ' ');
    const titleEl = card.querySelector('a.title span') || card.querySelector('.title');
    const authorEl = card.querySelector('a.author') || card.querySelector('.author');
    const cardText = (card.innerText || '').replace(/\s+/g, ' ').trim();
    const nums = cardText.match(/\d[\d,]*/g);
    const likeCount = nums ? nums[nums.length - 1].replace(/,/g, '') : '';
    const authorText = (authorEl ? authorEl.innerText : '').trim();
    const parts = authorText.split('\n').map(s => s.trim()).filter(Boolean);
    // 作者结构化提取（2026-09-04 v1.5.1 放宽）：uid 必取、token 可选 ——
    // 实测搜索页笔记卡作者链接可能不带 xsec_token（用户 channel 卡才带）；
    // authorEl 非 <a> 时用 closest('a') 兜底。缺 token 由 collect_user_notes
    // 按昵称走用户 channel 搜索自愈。
    const URE = /\/user\/profile\/([0-9a-f]{24}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\?xsec_token=([^&]+))?/i;
    const authorA = authorEl ? (authorEl.tagName === 'A' ? authorEl : authorEl.closest('a')) : null;
    const aHref = authorA ? (authorA.getAttribute('href') || '') : '';
    const am = aHref.match(URE);
    const avatarImg = card.querySelector('.author img');
    const coverImg = card.querySelector('a.cover img, img[src*="webpic"]');
    cards.push({
      id: id,
      raw_id: rawId,
      token: token,
      has_token: !!token,
      is_note: !!m,
      is_trending: !m && /大家都在搜/.test(txt),
      id_kind: UUID_RE.test(id) ? 'uuid' : (HEX24_RE.test(id) ? 'hex24' : 'other'),
      title: (titleEl ? titleEl.innerText : '').trim(),
      author: parts[0] || '',
      author_id: am ? am[1] : '',
      author_token: am ? am[2] : '',
      avatar: avatarImg ? (avatarImg.getAttribute('src') || avatarImg.getAttribute('data-src') || '') : '',
      date: parts[1] || '',
      likes: likeCount,
      cover: coverImg ? coverImg.src : ''
    });
  });
  return JSON.stringify({count: cards.length, cards: cards});
})()
"""

DETAIL_JS = r"""
(() => {
  const out = {url: location.href.split('?')[0]};
  const t = document.querySelector('.title');
  out.title = t ? t.innerText.trim() : '';
  const d = document.querySelector('#detail-desc');
  out.hasDesc = !!d;
  out.desc = d ? d.innerText.trim() : '';
  const imgs = [];
  const seen = new Set();
  document.querySelectorAll('.swiper-slide img').forEach(i => {
    let s = i.src || i.getAttribute('data-src') || '';
    if (!s) return;
    if (s.indexOf('http://') === 0) s = 'https://' + s.slice(7);
    const clean = s.split('!')[0].split('?')[0];
    if (seen.has(clean)) return;
    seen.add(clean);
    imgs.push(s);
  });
  out.images = imgs;
  out.bodyLen = document.body ? document.body.innerText.length : 0;
  return JSON.stringify(out);
})()
"""

# ---------------------------------------------------------------- 详情内嵌状态（v1.7.0 step1）
# 小红书详情页把接口返回 SSR 注入 window.__INITIAL_STATE__（.note.noteDetailMap[id].note）。
# 一次 evaluate 即可拿到 DOM 未渲染的富字段：分享数/精确时间戳/IP/结构化话题(含topic_id)/
# 原图(WB_DFT)/视频时长/作者token。三家外部仓库(mcp/downloader)已验证此路线可行。
# 零额外导航（详情页本已加载）、零签名逆向。读不到时静默降级到 DETAIL_JS。

NOTE_STATE_JS = r"""
(() => {
  const ID = "__ID__";
  const uw = v => (v && typeof v === 'object' && ('__v_isRef' in v) && ('value' in v)) ? v.value : v;
  try {
    const S = window.__INITIAL_STATE__;
    if (!S) return JSON.stringify({ok:false, reason:'no_state'});
    const note = uw(S.note);
    let map = uw(note && note.noteDetailMap);
    if (!map) return JSON.stringify({ok:false, reason:'no_map'});
    let entry = uw(map[ID]);
    if (!entry) { const ks = Object.keys(map); entry = uw(map[ks[ks.length-1]]); }
    const n = uw(entry && entry.note);
    if (!n) return JSON.stringify({ok:false, reason:'no_note'});
    const ii = uw(n.interactInfo) || {};
    const imgList = uw(n.imageList) || [];
    const images = imgList.map(im => { im = uw(im); return im.urlDefault || im.urlPre || ''; }).filter(Boolean);
    const image_meta = imgList.map(im => { im = uw(im);
      return {w:im.width, h:im.height, urlDefault:im.urlDefault||'', urlPre:im.urlPre||'',
              scenes:(uw(im.infoList)||[]).map(x=>uw(x).imageScene)}; });
    const tags = (uw(n.tagList)||[]).map(t => { t = uw(t); return {id:t.id, name:t.name, type:t.type}; });
    const at = (uw(n.atUserList)||[]).map(u => { u = uw(u); return {id:u.userId||u.id||'', name:u.nickname||u.name||''}; });
    const usr = uw(n.user) || {};
    const vid = uw(n.video);
    const capa = vid ? uw(vid.capa) : null;
    const vimg = vid ? uw(vid.image) : null;
    return JSON.stringify({ok:true, note_type:n.type, title:n.title||'', desc:n.desc||'',
      liked:ii.likedCount, collected:ii.collectedCount, comment:ii.commentCount, share:ii.shareCount,
      time:n.time, last_update:n.lastUpdateTime, ip:n.ipLocation||'',
      tags:tags, at_users:at, images:images, image_meta:image_meta,
      author:{id:usr.userId||'', nickname:usr.nickname||'', token:usr.xsecToken||'', avatar:usr.avatar||''},
      video: vid ? {duration:(capa&&capa.duration)||null,
                    cover:(vimg&&(vimg.firstFrameFileid||vimg.thumbnailFileid))||''} : null,
      xsec_token:n.xsecToken||''});
  } catch(e) { return JSON.stringify({ok:false, reason:String(e).slice(0,140)}); }
})()
"""


def read_note_state(bridge, note_id):
    """详情页读内嵌 state。失败/无 state → {}（不抛，交回 DETAIL_JS 兜底）。"""
    try:
        js = NOTE_STATE_JS.replace("__ID__", str(note_id))
        r = bridge.evaluate(js, timeout=60)
    except Exception:
        return {}
    return r if isinstance(r, dict) else {}


def read_detail_and_state(bridge, note_id):
    """B2：一次 evaluate 同取 DETAIL_JS 结果 + note state，省一次 webbridge 往返。
    返回 (detail_dict, state_dict)；combined 解析失败则分别回退单独 evaluate。"""
    try:
        js = "(() => { const d = %s; const s = %s; return JSON.stringify({detail: d, state: s}); })()" % (
            DETAIL_JS.strip(), NOTE_STATE_JS.replace("__ID__", str(note_id)).strip())
        r = bridge.evaluate(js, timeout=60)
    except Exception:
        r = None
    det = ns = None
    if isinstance(r, dict):
        det, ns = r.get("detail"), r.get("state")
        if isinstance(det, str):
            try:
                det = json.loads(det)
            except Exception:
                det = None
        if isinstance(ns, str):
            try:
                ns = json.loads(ns)
            except Exception:
                ns = None
    if not isinstance(det, dict):
        det = bridge.evaluate(DETAIL_JS, timeout=60) or {}
    if not isinstance(ns, dict):
        ns = read_note_state(bridge, note_id)
    return (det if isinstance(det, dict) else {}), (ns if isinstance(ns, dict) else {})


def norm_count(v):
    """小红书计数字符串归一为 int：''/占位→None；'4,305'→4305；'1.6万'→16000；'10.2万'→102000。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in ("赞", "收藏", "回复"):
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)(万|亿)?", s)
    if not m:
        return None
    num = float(m.group(1))
    mult = {"万": 10000, "亿": 100000000}.get(m.group(2), 1)
    return int(num * mult)


def epoch_ms_to_iso(ms):
    """epoch 毫秒 → ISO8601（Asia/Shanghai +08:00）。非法/0 → None。"""
    try:
        ms = int(ms)
        if ms <= 0:
            return None
        tz = datetime.timezone(datetime.timedelta(hours=8))
        return datetime.datetime.fromtimestamp(ms / 1000.0, tz).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    except Exception:
        return None


def merge_state_into_rec(rec, ns):
    """把 read_note_state 结果里的富字段以【新可选键】写入记录；绝不改既有键。
    取不到的键不写（向后兼容）。content/images 由调用点单独处理（state 优先、DETAIL 兜底）。"""
    if not ns or not ns.get("ok"):
        return
    for src, dst in (("liked", "likes_int"), ("collected", "collects"),
                     ("comment", "comments_count"), ("share", "shares")):
        n = norm_count(ns.get(src))
        if n is not None:
            rec[dst] = n
    ts = ns.get("time")
    if ts:
        rec["publish_ts"] = int(ts)
        iso = epoch_ms_to_iso(ts)
        if iso:
            rec["publish_date"] = iso[:10]
            rec["publish_iso"] = iso
    lu = ns.get("last_update")
    if lu:
        try:
            rec["update_ts"] = int(lu)
        except Exception:
            pass
    if ns.get("ip"):
        rec["ip"] = ns["ip"]
    if ns.get("note_type"):
        rec["note_type"] = ns["note_type"]
    if ns.get("tags"):
        rec["hashtags"] = ns["tags"]
    if ns.get("at_users"):
        rec["at_users"] = ns["at_users"]
    a = ns.get("author") or {}
    if a.get("token"):
        rec["author_xsec_token"] = a["token"]
    if ns.get("image_meta"):
        rec["image_meta"] = ns["image_meta"]
    vid = ns.get("video")
    if ns.get("note_type") == "video" or vid:
        rec["video"] = {"duration": (vid or {}).get("duration"),
                        "cover_fileid": (vid or {}).get("cover")}


# ------------------------------------------------ 评论采集（v1.7.0 step4，走 state）
# 评论首屏在 state noteDetailMap[id].comments={list,cursor,hasMore,loading}；滚动触发站点
# 自身懒加载后重读 state 累积。hasMore=false 即到底权威信号（比猜 docH 稳，免假到底）。

COMMENTS_STATE_JS = r"""
(() => {
  const ID = "__ID__";
  const uw = v => (v && typeof v === 'object' && ('__v_isRef' in v) && ('value' in v)) ? v.value : v;
  const map = c => { c = uw(c) || {}; const u = uw(c.userInfo) || {}; return {
      cid: c.id || c.commentId || '', author: u.nickname || '', author_id: u.userId || '',
      content: c.content || '', ip: c.ipLocation || '', create_time: c.createTime || null,
      likes: String(c.likeCount || ''), is_author: !!c.isAuthor }; };
  try {
    const m = uw(window.__INITIAL_STATE__.note.noteDetailMap);
    const e = uw(m && m[ID]) || uw(m && m[Object.keys(m)[0]]);
    const cm = uw(e && e.comments);
    if (!cm) return JSON.stringify({ok:false, list:[], hasMore:false});
    const list = (uw(cm.list) || []).map(raw => {
      const o = map(raw);
      o.replies = (uw(raw.subComments) || []).map(s => map(s));
      return o;
    });
    return JSON.stringify({ok:true, list:list, hasMore:!!uw(cm.hasMore), cursor:cm.cursor || ''});
  } catch(e) { return JSON.stringify({ok:false, list:[], hasMore:false, err:String(e).slice(0,120)}); }
})()
"""

COMMENT_SCROLL_JS = r"""
(() => {
  const sels = ['.note-scroller', '#noteContainer', '.interaction-container', '.note-container', '.comments-el'];
  let sc = null;
  for (const s of sels) { const e = document.querySelector(s); if (e && e.scrollHeight > e.clientHeight + 30) { sc = e; break; } }
  const step = 2000;
  if (sc) { sc.scrollTop = Math.min(sc.scrollTop + step, sc.scrollHeight); }
  else { const se = document.scrollingElement || document.documentElement; se.scrollTop = Math.min(se.scrollTop + step, se.scrollHeight); }
  return 1;
})()
"""

# 点第一个"展开N条回复"按钮：站点会把该楼中楼写回 state.subComments（实机坐实 1→3）
EXPAND_ONE_JS = r"""
(() => {
  const btns = [...document.querySelectorAll('.show-more,[class*=show-more]')];
  const b = btns.find(x => /^展开\s*\d*\s*条?回复/.test((x.innerText || '').trim()) || /展开.*条回复/.test((x.innerText || '').trim()));
  if (!b) return JSON.stringify({clicked:false});
  b.click();
  return JSON.stringify({clicked:true, text:(b.innerText || '').trim()});
})()
"""


def _comment_normalize(c):
    """评论时间戳(ms)→date；保留 create_time 原值。"""
    ts = c.get("create_time")
    if ts:
        iso = epoch_ms_to_iso(ts)
        if iso:
            c["date"] = iso[:10]
    return c


def collect_comments(bridge, note_id, max_comments=200, max_rounds=40,
                     no_gain_limit=6, delay=1.2, expand_replies=True, max_expands=60):
    """滚动重读 state 累积评论 + 展开子回复。返回 (comments_list, has_more)。"""
    seen = {}

    def grab():
        r = bridge.evaluate(COMMENTS_STATE_JS.replace("__ID__", str(note_id)), timeout=60) or {}
        for c in (r.get("list") or []):
            cid = c.get("cid")
            if cid:
                seen[cid] = _comment_normalize(c)  # 总是刷新，令展开后 replies 增长生效
        return bool(r.get("hasMore")), r.get("err") or ""

    def front():
        try:
            bridge.cdp("Page.bringToFront")
        except Exception:
            pass

    has_more, _ = grab()
    rounds = 0
    nogain = 0
    last = len(seen)
    while has_more and len(seen) < max_comments and rounds < max_rounds:
        rounds += 1
        front()
        try:
            bridge.evaluate(COMMENT_SCROLL_JS, timeout=20)
        except Exception:
            pass
        time.sleep(delay)
        has_more, err = grab()
        if len(seen) == last:
            nogain += 1
            if nogain >= no_gain_limit:
                break
        else:
            nogain = 0
            last = len(seen)

    if expand_replies:
        for _ in range(max_expands):
            front()
            try:
                r = bridge.evaluate(EXPAND_ONE_JS, timeout=20) or {}
            except Exception:
                r = {}
            if not r.get("clicked"):
                break
            time.sleep(delay)
            grab()
        grab()
    return list(seen.values()), has_more


def image_key(url):
    u = (url or "").split("!")[0].split("?")[0]
    return u.replace("https://", "").replace("http://", "")


# ---------------------------------------------------------------- 落盘

class NoteWriter(object):
    """增量落盘：每帖 upsert 后立即 tmp 写 + os.replace，中途失败不丢成果。"""

    def __init__(self, path, query=None):
        self.path = path
        self.data = {"query": query, "count": 0, "notes": []}
        self._idx = {}
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                if query:
                    self.data["query"] = query
            except Exception as e:
                log("  读取已有 JSON 失败(%s)，从空开始" % str(e)[:60])
        for i, n in enumerate(self.data.get("notes") or []):
            if n.get("id"):
                self._idx[n["id"]] = i

    def upsert(self, note, flush=True):
        nid = note.get("id")
        if nid and nid in self._idx:
            self.data["notes"][self._idx[nid]] = note
        else:
            if nid:
                self._idx[nid] = len(self.data["notes"])
            self.data["notes"].append(note)
        self.data["count"] = len(self.data["notes"])
        if flush:
            self.flush()

    def flush(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        # Windows：目标文件被其它进程短暂持有时 os.replace 抛 WinError 5
        # （二级探索台每 1s 轮询读本文件、杀毒实时扫描都会触发）。短退避重试，
        # 避免一次碰撞就终止整条任务；最终仍失败则记日志，数据仍在内存待下次 flush。
        for k in range(6):
            try:
                os.replace(tmp, self.path)
                return
            except OSError as e:
                if k == 5:
                    log("  落盘失败（重试 6 次仍被占用）: %s" % str(e)[:90])
                    return
                time.sleep(0.15 * (k + 1))

    def done_ids(self):
        return set(self._idx.keys())

    def reusable_ids(self):
        """断点续跑：仅 ok 可复用；empty/error 需要重试。"""
        return set(i for i, n in ((n.get("id"), n) for n in self.data.get("notes") or [])
                   if i and n.get("status") == "ok")

    def set_stats(self, stats):
        """把本次采集的统计（页面卡片总数、各状态计数）落盘，供二级做覆盖率分析。"""
        self.data["stats"] = stats
        self.flush()


def _atomic_write(path, data):
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _ext_of(mime, data):
    if "webp" in mime:
        return "webp"
    if "jpeg" in mime or "jpg" in mime:
        return "jpg"
    if "png" in mime:
        return "png"
    if data[:4] == b"RIFF":
        return "webp"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return "webp"


# ---------------------------------------------------------------- 图片下载

def _existing_indices(node_dir):
    """目录中已有图片的下标集合（img_NN.ext -> NN-1），0 字节文件忽略。"""
    have = set()
    if not os.path.isdir(node_dir):
        return have
    for f in os.listdir(node_dir):
        m = re.match(r"^img_(\d+)\.", f)
        if m and os.path.getsize(os.path.join(node_dir, f)) > 0:
            have.add(int(m.group(1)) - 1)
    return have


# ------------------------------------------------ 图片直连下载（v1.7.0 step7，抓包兜底）
# 实机坐实：state image_meta 的 urlDefault 是自带签名的 CDN URL，urllib 直连即 200（webp），
# 无需 cookie/referer；去签名才 403。故"直连为主、CDP 抓包仅兜底 403/过期"。
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _ext_from_mime(mime):
    m = (mime or "").split(";")[0].strip()
    return {"image/webp": "webp", "image/jpeg": "jpg", "image/jpg": "jpg",
            "image/png": "png", "image/heic": "heic", "image/heif": "heic"}.get(m, "jpg")


def _direct_save(url, target_base):
    """urllib 直连下载单图到 target_base.<ext>；成功返回 ext，失败返回 None。"""
    if not url:
        return None
    u = url if url.startswith("http") else "https:" + url
    try:
        req = urllib.request.Request(u, headers={"User-Agent": UA_BROWSER,
                                                 "Referer": "https://www.xiaohongshu.com/"})
        resp = urllib.request.urlopen(req, timeout=20)
        data = resp.read()
        mime = resp.headers.get("Content-Type") or ""
    except Exception:
        return None
    if not data or ("image" not in mime and "octet" not in mime):
        return None
    ext = _ext_from_mime(mime)
    try:
        _atomic_write(target_base + "." + ext, data)
        return ext
    except Exception:
        return None


def download_images_dc(bridge, capture, note_id, urls, out_dir):
    """直连为主、CDP 抓包兜底的下标稳定命名下载。返回与 download_images 同构 stats。"""
    urls = [u if u.startswith("https://") else u.replace("http://", "https://") for u in (urls or [])]
    seen, uniq = set(), []
    for u in urls:
        k = image_key(u)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(u)
    node_dir = os.path.join(out_dir, safe_name(note_id))
    os.makedirs(node_dir, exist_ok=True)
    total = len(uniq)
    if not total:
        return {"saved": 0, "failed": 0, "skipped": 0, "total": 0}

    have = _existing_indices(node_dir)
    missing = [i for i in range(total) if i not in have]
    if not missing:
        return {"saved": total, "failed": 0, "skipped": total, "total": total, "note": "all-present"}

    def _save_one(i):
        return bool(_direct_save(uniq[i], os.path.join(node_dir, "img_%02d" % (i + 1))))
    if len(missing) <= 1:
        direct_ok = sum(1 for i in missing if _save_one(i))
    else:
        with ThreadPoolExecutor(max_workers=min(6, len(missing))) as ex:
            direct_ok = sum(ex.map(_save_one, missing))
    final = _existing_indices(node_dir) & set(range(total))
    still = [i for i in range(total) if i not in final]
    if still:
        download_images(bridge, capture, note_id, uniq, out_dir)  # 抓包只补余下缺失
        final = _existing_indices(node_dir) & set(range(total))
    return {"saved": len(final), "failed": total - len(final), "skipped": direct_ok,
            "total": total, "note": "direct%d%s" % (direct_ok, "+capture" if still else "")}


def download_images(bridge, capture, note_id, urls, out_dir, skip_nonempty=True):
    """按 urls 原始下标稳定命名。**增量式**：已存在的下标不覆盖、不重复下载，
    只补缺失下标 —— 旧版部分下载的目录也能安全补齐（旧版是整体跳过）。"""
    urls = [u if u.startswith("https://") else u.replace("http://", "https://") for u in (urls or [])]
    # 去重保序
    seen, uniq = set(), []
    for u in urls:
        k = image_key(u)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(u)

    node_dir = os.path.join(out_dir, safe_name(note_id))
    os.makedirs(node_dir, exist_ok=True)

    have = _existing_indices(node_dir)
    total = len(uniq)
    if not total:
        return {"saved": 0, "failed": 0, "skipped": 0, "total": 0}

    missing = [i for i in range(total) if i not in have]
    if not missing:
        return {"saved": total, "failed": 0, "skipped": total, "total": total,
                "note": "all-present"}

    force = ("(function(){var urls=%s;urls.forEach(function(u,i){"
             "var im=new Image();im.src=u+(u.indexOf('?')>=0?'&':'?')+'f='+i;});return 'ok';})()"
             % json.dumps(uniq))
    try:
        bridge.evaluate(force)
        time.sleep(2.0)
    except Exception as e:
        log("  强制请求失败: %s" % str(e)[:70])

    try:
        reqs = capture.list()
    except BridgeError as e:
        return {"saved": len(have & set(range(total))), "failed": len(missing),
                "skipped": total - len(missing), "total": total,
                "note": "list-fail:%s" % e.kind}

    webpic = [r for r in reqs if "webpic" in (r.get("url") or "")]
    key2idx = {image_key(u): i for i, u in enumerate(uniq)}
    need = set(missing)

    def _target(idx):
        return os.path.join(node_dir, "img_%02d" % (idx + 1))

    def _target_exists(idx):
        return any(os.path.isfile(_target(idx) + "." + e) for e in ("webp", "jpg", "png"))

    got, failed = set(), set()
    for rr in webpic:
        idx = key2idx.get(image_key(rr.get("url", "")))
        if idx is None or idx not in need or idx in got:
            continue
        if _target_exists(idx):          # 双保险：绝不覆盖已有文件
            got.add(idx)
            continue
        try:
            data, mime = capture.detail(rr["requestId"])
            if not data:
                continue
            _atomic_write(_target(idx) + "." + _ext_of(mime, data), data)
            got.add(idx)
        except Exception as e:
            log("  图 %d 失败: %s" % (idx + 1, str(e)[:70]))
        time.sleep(0.2)

    # 空 body 的单图重取一次
    for rr in webpic:
        idx = key2idx.get(image_key(rr.get("url", "")))
        if idx is None or idx not in need or idx in got or idx in failed:
            continue
        try:
            data, mime = capture.detail(rr["requestId"])
            if data:
                _atomic_write(_target(idx) + "." + _ext_of(mime, data), data)
                got.add(idx)
            else:
                failed.add(idx)
        except Exception:
            failed.add(idx)
        time.sleep(0.3)

    final_have = _existing_indices(node_dir) & set(range(total))
    return {"saved": len(final_have), "failed": total - len(final_have),
            "skipped": total - len(missing), "total": total,
            "note": "+%d(补)/已有%d" % (len(got), total - len(missing))}


# ---------------------------------------------------------------- 搜索/取 token

def parse_filters(s):
    """解析 --filter 参数为筛选 dict：支持 JSON 对象或 k=v,k=v；值做别名归一为 UI 文本。
    返回 None 表示不筛选；空/非法输入返回 {}（等价不筛选，不中断采集）。"""
    if not s:
        return None
    s = s.strip()
    d = {}
    try:
        if s.startswith("{"):
            raw = json.loads(s)
            if isinstance(raw, dict):
                d = raw
        else:
            for pair in s.split(","):
                pair = pair.strip()
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    d[k.strip()] = v.strip()
    except Exception:
        log("  --filter 解析失败: %s" % s[:60])
        return {}
    out = {}
    for k, v in d.items():
        if k not in FILTER_DIMS:
            log("  忽略未知筛选维度: %s" % k)
            continue
        out[k] = FILTER_VALUE_ALIAS[k].get(str(v), str(v))
    return out or None


def apply_filters(bridge, filters):
    """在已打开的搜索页上按 filters(dict) 逐维度点击筛选面板选项（值须为 UI 文本）。
    只点击非默认项；每维度点击后等待其重新请求/渲染，最后等卡片刷新。
    返回 {"applied":[{dim,label,ok},...]}。失败不抛（面板打不开/选项缺失时降级为不筛选）。"""
    if not filters:
        return {"applied": []}
    try:
        bridge.evaluate("(function(){var f=document.querySelector('.filter');"
                        "if(f){f.click();return 1;}return 0;})()")
        time.sleep(0.8)
    except Exception as e:
        log("  打开筛选面板失败: %s" % str(e)[:60])
        return {"applied": [], "err": "open_fail"}
    applied = []
    for key, label in filters.items():
        dim = FILTER_DIMS.get(key)
        if not dim:
            continue
        if label == dim["default"]:   # 默认值即当前态，无需点击
            continue
        js = ("(function(){var fs=document.querySelectorAll('.filters');"
              "for(var i=0;i<fs.length;i++){"
              "if(fs[i].textContent.indexOf(%s)<0)continue;"
              "var tags=fs[i].querySelectorAll('.tags');"
              "for(var j=0;j<tags.length;j++){"
              "if((tags[j].textContent||'').trim()===%s){tags[j].click();"
              "return JSON.stringify({ok:true});}}}"
              "return JSON.stringify({ok:false});})()"
              % (json.dumps(dim["title"]), json.dumps(label)))
        try:
            r = bridge.evaluate(js, timeout=30)
            ok = bool(isinstance(r, dict) and r.get("ok"))
        except Exception as e:
            ok = False
            log("  筛选点击异常 %s/%s: %s" % (key, label, str(e)[:50]))
        applied.append({"dim": key, "label": label, "ok": ok})
        log("  筛选 %s = %s -> %s" % (key, label, "ok" if ok else "miss"))
        time.sleep(1.0)   # 等待该维度触发重新请求/渲染
    # 等卡片刷新
    bridge.wait_for_selector(".note-item", timeout=10)
    time.sleep(1.0)
    return {"applied": applied}


def search_url(query, type=51):
    """搜索页 URL。type=51 笔记全文检索（默认）；type=54 话题模式（点 #话题 的落点，
    返回话题聚焦笔记，DOM 与 type=51 一致——2026-09-04 实测）。"""
    if type:
        return ("https://www.xiaohongshu.com/search_result?keyword="
                + urllib.parse.quote(query) + "&type=%d" % type)
    return "https://www.xiaohongshu.com/search_result?keyword=" + urllib.parse.quote(query)


def note_url(note_id, token):
    return ("https://www.xiaohongshu.com/explore/%s?xsec_token=%s&xsec_source=pc_search"
            % (note_id, urllib.parse.quote(token or "")))


def goto_search(bridge, query, wait=6.0, type=51):
    bridge.ensure_tab(search_url(query, type))
    bridge.navigate(search_url(query, type), wait_selector=".note-item", timeout=15)
    time.sleep(max(0, wait - 3.0))


def fetch_cards(bridge):
    d = bridge.evaluate(CARD_JS, timeout=60)
    if isinstance(d, dict):
        return d.get("cards") or []
    return []


# ------------------------------------------------ 搜索选题词源（v1.7.0 step2）
# 联想词 suggestions 进搜索页即在 state；热搜/猜你想搜 queryTrendingInfo 需"聚焦+清空"
# 触发下拉才填充（实测）。三仓库均无此层，是探索台"决策要搜什么"的扩展词源。

SEARCH_STATE_JS = r"""
(() => {
  const uw = v => (v && typeof v === 'object' && ('__v_isRef' in v) && ('value' in v)) ? v.value : v;
  try {
    const S = window.__INITIAL_STATE__;
    const search = uw(S && S.search);
    if (!search) return JSON.stringify({ok:false, reason:'no_search'});
    const sug = (uw(search.suggestions) || []).map(x => { x = uw(x) || {};
      return {text:x.text||'', search_type:x.searchType||'', type:x.type||'', highlight:(x.highlightFlags||[])}; });
    const hw = uw(search.hintWord) || {};
    const qt = uw(search.queryTrendingInfo);
    let trending = null;
    if (qt && (uw(qt.queries) || uw(qt.aiWords))) {
      trending = {
        title: qt.title || '',
        queries: (uw(qt.queries) || []).map(x => { x = uw(x) || {};
          return {title:x.title||'', search_word:x.searchWord||'', text:x.text||'', type:x.type||'', desc:x.desc||''}; }),
        ai_words: (uw(qt.aiWords) || []).map(x => { x = uw(x) || {};
          return {search_word:x.searchWord||'', title:x.title||'', desc:x.desc||''}; }),
        hint_word: (function(h){ h = uw(h) || {}; return {title:h.title||'', search_word:h.searchWord||'', type:h.type||''}; })(qt.hintWord)
      };
    }
    return JSON.stringify({ok:true, suggestions:sug,
      hint_word:{title:hw.title||'', search_word:hw.searchWord||'', type:hw.type||''}, trending:trending});
  } catch(e) { return JSON.stringify({ok:false, reason:String(e).slice(0,140)}); }
})()
"""

TRIGGER_DISCOVER_JS = r"""
(() => {
  const el = document.querySelector('#search-input');
  if (!el) return JSON.stringify({ok:false, reason:'no_input'});
  try {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    el.focus(); setter.call(el, '');
    el.dispatchEvent(new Event('input', {bubbles:true}));
    return JSON.stringify({ok:true});
  } catch(e) { return JSON.stringify({ok:false, reason:String(e).slice(0,120)}); }
})()
"""


def suggest(bridge, query, out_path=None, wait=8.0):
    """采集某关键词的选题词源：联想词 + 猜你想搜/热搜 + AI 问句。产物独立，不混进 collect notes。"""
    payload = {"query": query,
               "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
               "suggestions": [], "hint_word": {}, "trending": None,
               "stats": {"suggestions": 0, "trending_queries": 0, "ai_words": 0}}
    goto_search(bridge, query, wait=wait, type=51)
    r = bridge.evaluate(SEARCH_STATE_JS, timeout=60) or {}
    if isinstance(r, dict) and r.get("ok"):
        payload["suggestions"] = r.get("suggestions") or []
        payload["hint_word"] = r.get("hint_word") or {}
    # 触发"搜索发现/猜你想搜"面板：进搜索页时 queryTrendingInfo 为空，须聚焦+清空才拉
    try:
        bridge.evaluate(TRIGGER_DISCOVER_JS, timeout=30)
    except Exception:
        pass
    time.sleep(2.0)
    r2 = bridge.evaluate(SEARCH_STATE_JS, timeout=60) or {}
    if isinstance(r2, dict) and r2.get("ok") and r2.get("trending"):
        payload["trending"] = r2["trending"]
    st = payload["stats"]
    st["suggestions"] = len(payload["suggestions"])
    if payload["trending"]:
        st["trending_queries"] = len(payload["trending"].get("queries") or [])
        st["ai_words"] = len(payload["trending"].get("ai_words") or [])
    if out_path:
        _atomic_write(out_path, json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"))
    return payload


def fetch_token_map(bridge, query):
    """补图用：重新搜索取 id->token（历史 JSON 未保存 token）。"""
    goto_search(bridge, query)
    cards = fetch_cards(bridge)
    return {c["id"]: c["token"] for c in cards if c.get("token")}, cards


# ---------------------------------------------------------------- 用户通道（2026-09-04）

# 用户 channel 实体卡片：a[href*="/user/profile/"]，含用户级 xsec_token
USER_CARD_JS = r"""
(() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('a[href*="/user/profile/"]').forEach(a => {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/user\/profile\/([0-9a-f]{24}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\?xsec_token=([^&]+)/i);
    if (!m) return;
    if (seen.has(m[1])) return;
    seen.add(m[1]);
    const txt = (a.innerText || '').replace(/\s+/g, ' ').trim();
    let nick = (txt.split('小红书号')[0] || '').replace(/[・·]\s*$/, '').trim();
    nick = nick.replace(/\s*\d+(小时|分钟|天)前更新\s*$/, '').trim();
    const xhs = (txt.match(/小红书号[：:]\s*([^\s]+)/) || [])[1] || '';
    const fans = (txt.match(/粉丝[・·]?\s*([0-9.]+万?)/) || [])[1] || '';
    const notes = (txt.match(/笔记[・·]?\s*([0-9]+)/) || [])[1] || '';
    out.push({user_id: m[1], token: m[2], nickname: nick, xhs_id: xhs,
              fans: fans, notes_count: notes,
              profile_url: "https://www.xiaohongshu.com/user/profile/" + m[1]
                           + "?xsec_token=" + m[2] + "&xsec_source=pc_search"});
  });
  return JSON.stringify({users: out});
})()
"""

# 用户主页笔记卡片：token 藏在 /user/profile/{uid}/{nid}?xsec_token=... 链接里
USER_NOTES_CARD_JS = r"""
(() => {
  const cards = [];
  const seen = new Set();
  document.querySelectorAll('.note-item').forEach(card => {
    const id = card.getAttribute('data-note-id') || '';
    if (!id || seen.has(id)) return;
    seen.add(id);
    let token = '';
    card.querySelectorAll('a').forEach(a => {
      const href = a.getAttribute('href') || '';
      const m = href.match(/\/(?:explore|user\/profile\/[^/]+)\/([0-9a-f]{24}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\?xsec_token=([^&]+)/i);
      if (m && m[1] === id) token = m[2];
    });
    const titleEl = card.querySelector('a.title span') || card.querySelector('.title');
    const txt = (card.innerText || '').replace(/\s+/g, ' ');
    const nums = txt.match(/\d[\d,]*/g);
    cards.push({id: id, token: token, has_token: !!token,
                title: (titleEl ? titleEl.innerText : '').trim(),
                likes: nums ? nums[nums.length - 1].replace(/,/g, '') : ''});
  });
  return JSON.stringify({cards: cards});
})()
"""


def open_user_search(bridge, query, wait=8.0):
    """搜索 -> 点击「用户」channel（前端 state，URL 不变）。"""
    goto_search(bridge, query, wait=wait)
    try:
        bridge.evaluate("(function(){var h=null;document.querySelectorAll('.channel').forEach(function(el){"
                        "if(h)return;if((el.innerText||'').trim()==='用户'){el.click();h=1;}});"
                        "return h?'ok':'miss';})()", timeout=30)
        time.sleep(2.0)
    except Exception as e:
        log("  切用户 channel 失败: %s" % str(e)[:60])


def fetch_user_cards(bridge):
    d = bridge.evaluate(USER_CARD_JS, timeout=60)
    if isinstance(d, dict):
        return d.get("users") or []
    return []


def goto_user_profile(bridge, user_id, user_token, wait=6.0):
    url = ("https://www.xiaohongshu.com/user/profile/%s?xsec_token=%s&xsec_source=pc_search"
           % (user_id, urllib.parse.quote(user_token or "")))
    bridge.ensure_tab(url)
    bridge.navigate(url, wait_selector=".note-item", timeout=15)
    try:
        bridge.cdp("Page.bringToFront")   # 置前台：后台 tab 节流会拖慢加载，首屏可能 0 卡
    except Exception:
        pass
    time.sleep(max(0, wait - 3.0))
    return url


def fetch_user_notes(bridge):
    d = bridge.evaluate(USER_NOTES_CARD_JS, timeout=60)
    if isinstance(d, dict):
        return d.get("cards") or []
    return []


# ------------------------------------------------ 用户资料卡（v1.7.0 step3）
# .user-info 实测字段稳定（探针坐实）：昵称/小红书号/IP属地/简介/性别/常驻地/关注/粉丝/获赞与收藏。
# 注意：获赞与"收藏"合并计数（非分开）；数值带"万"缩写，norm_count 归一。

USER_PROFILE_JS = r"""
(() => {
  const t = e => e ? (e.innerText || '').replace(/\s+/g, ' ').trim() : '';
  const info = document.querySelector('.user-info');
  if (!info) return JSON.stringify({ok:false, reason:'no_info'});
  const q = s => info.querySelector(s);
  const red = q('.user-redId'), ip = q('.user-IP');
  const inter = {};
  info.querySelectorAll('.user-interactions > div').forEach(d => {
    const c = d.querySelector('.count'), s = d.querySelector('.shows');
    if (c && s) inter[t(s)] = t(c);
  });
  const use = info.querySelector('.gender svg use');
  let gender = use ? (use.getAttribute('href') || use.getAttribute('xlink:href') || '') : '';
  let region = '';
  info.querySelectorAll('.tag-item').forEach(el => {
    if (!el.querySelector('.gender') && t(el)) { region = t(el); }
  });
  return JSON.stringify({ok:true,
    nickname: t(q('.user-name')),
    red_id: red ? t(red).replace(/^小红书号[:：]/, '') : '',
    ip_location: ip ? t(ip).replace(/^IP属地[:：]/, '') : '',
    desc: t(q('.user-desc')),
    gender_raw: gender,
    region: region,
    follows: inter['关注'] || '', fans: inter['粉丝'] || '',
    gets_and_collects: inter['获赞与收藏'] || ''});
})()
"""


def read_user_profile(bridge):
    p = bridge.evaluate(USER_PROFILE_JS, timeout=60)
    if not isinstance(p, dict) or not p.get("ok"):
        return {}
    g = (p.get("gender_raw") or "").lower()
    gender = "男" if "male" in g else ("女" if "female" in g else "")
    return {
        "nickname": p.get("nickname") or None,
        "red_id": p.get("red_id") or None,
        "ip_location": p.get("ip_location") or None,
        "desc": p.get("desc") or None,
        "gender": gender or None,
        "region": p.get("region") or None,
        "follows": norm_count(p.get("follows")),
        "fans": norm_count(p.get("fans")),
        "gets_and_collects": norm_count(p.get("gets_and_collects")),
    }


# 主页无限流滚动（v1.6.0 v5，实机结论）：
# ① 关键前提——tab 必须在浏览器前台（visibilityState=visible）。后台 tab 会被浏览器
#    节流冻结定时器/rAF/IntersectionObserver，虚拟列表停在"30 卡紧凑占位"（docH≈winH、
#    window 滚不动），滚动无法加载更多。Page.bringToFront 置前台后页面自动展开
#    （实测 docH 777→3091），window 变可滚。调用方（_scroll_collect_cards）先做前台化。
# ② 展开后滚动容器为 window（document.scrollingElement），虚拟列表监听 scroll 事件。
#    用 STEP=2400 分段增量滚动（对齐 probe_virtual_ids 验证参数：scrollTop+=2400 +
#    sleep 3s 稳定累积）；直接赋值 scrollTop 会产生原生 scroll 事件。
# ③ 底部抖动：scrollTop 已到底（≥maxTop-20）时，先反向回弹 800px 再滚回底，
#    制造连续 scroll 事件强制虚拟列表重新触发"加载下一页"——实测"到底后最后一批
#    加载"存在随机性，不做抖动会在 62 篇左右误判到底，做了可稳定到 109+。
# ④ 内层 .tab-content-item 仅在内容不足一屏（少量笔记博主）时兜底。
# ⑤ 渐进滚动（v1.6.2）：普通滚动目标 = docH - 1.0 屏（保留缓冲），不一步触底——
#    二次深度采集曾出现"scrollTop 一步跳到 docH 底部 → 加载器不再响应 → 假到底"
#    （docH 冻结但内容未完）。渐进式让"接近底部 → 触发加载下一页"每次生效；
#    仅在 docH 停止增长且 scrollTop 抵近缓冲边界时才进入底部抖动做到底验证。
SCROLL_JS = r"""
(() => {
  const sc = document.scrollingElement || document.documentElement;
  const STEP = 2400;
  const vh = window.innerHeight;
  if (sc.scrollHeight > vh) {
    // 渐进目标：滚向 docH - 1.0 屏（保留缓冲，避免一步触底）
    const target = Math.min(sc.scrollTop + STEP, Math.max(0, sc.scrollHeight - vh * 1.0));
    if (target > sc.scrollTop) {
      sc.scrollTop = target;
    } else {
      // 已抵缓冲边界（接近底部）：底部抖动强制触发加载器
      sc.scrollTop = Math.max(0, sc.scrollTop - 800);   // 反向回弹
      sc.scrollTop = sc.scrollHeight;                    // 立即再到底
    }
    return JSON.stringify({mode: 'window', top: sc.scrollTop, h: sc.scrollHeight});
  }
  let tab = document.querySelector('.tab-content-item');
  let tabTop = -1;
  if (tab && tab.scrollHeight > tab.clientHeight) {
    tab.scrollTop = Math.min(tab.scrollTop + STEP, tab.scrollHeight);
    tab.dispatchEvent(new WheelEvent('wheel', {deltaY: STEP, bubbles: true, cancelable: true}));
    tab.dispatchEvent(new Event('scroll', {bubbles: true}));
    tabTop = tab.scrollTop;
  }
  const docH = sc.scrollHeight;
  const mode = docH > vh ? 'window' : (tab && tabTop >= 0 ? 'div' : 'none');
  return JSON.stringify({mode: mode, top: sc.scrollTop, h: docH, tabTop: tabTop});
})()
"""


def _scroll_to_bottom(bridge):
    """滚动到页面底部（window/div 双兜底）。失败不抛，交由累积逻辑重试。"""
    try:
        return bridge.evaluate(SCROLL_JS, timeout=30) or {}
    except Exception as e:
        log("  滚动失败: %s" % str(e)[:60])
        return {}


def _scroll_collect_cards(bridge, target, max_scrolls=40, no_gain_limit=3, known_ids=None):
    """滚动主页无限流，累积去重卡片（v1.6.0；v1.6.1 known 卡不占配额）。
    - 首屏先取一次；随后循环：组合滚动（window 增量 + 内层容器增量 + 合成事件，
      展开前由内层滚动触发、展开后由 window 滚动）→ 随机 sleep(2.5~4s) 防抖且
      等页面加载 → 重提卡片去重累积；
    - 终止（三选一先满足）：**非 known 卡数** >= target（v1.6.1：已知卡不占
      target 名额，与搜索页「limit 只计新笔记」语义一致——否则已采过首屏的
      博主 target=30 时 0 滚动空转、0 新笔记，深度采集永远失效）；滚动次数达
      max_scrolls（预算防失控）；连续 no_gain_limit 次"无新增且 docH 不再增长"
      判定到底（exhausted）——v1.6.2：判定前先做"底部抖动 + 长等待 + 再提取"
      强制验证，防二次深度采集的假到底（见 SCROLL_JS 注释 ⑤）；
    - 返回 (cards, scrolls, exhausted)：cards = 滚动累积去重后的**全部**卡片
      （含 known，skip/dedup 由详情循环处理）；exhausted = 是否判定到底。"""
    known = set(known_ids or ())
    cards, seen = [], set()

    def fresh():
        """未在 known-ids 清单里的卡片数（= 本次滚动采集的配额计数口径）。"""
        return sum(1 for c in cards if c.get("id") not in known)

    def ingest(batch):
        added = 0
        for c in batch or []:
            nid = c.get("id")
            if nid and nid not in seen:
                seen.add(nid)
                cards.append(c)
                added += 1
        return added

    def doc_state():
        try:
            return bridge.evaluate(
                "(function(){var sc=document.scrollingElement||document.documentElement;"
                "return JSON.stringify({docH:sc.scrollHeight,winH:window.innerHeight,"
                "top:sc.scrollTop,vis:document.visibilityState});})()", timeout=15) or {}
        except Exception:
            return {}

    # —— 前台化（关键前提，最先做）——
    # 实机结论：浏览器 tab 处于后台（visibilityState=hidden）时，页面定时器/rAF/
    # IntersectionObserver 被浏览器节流冻结：首屏 .note-item 加载被拖慢甚至 0 卡，
    # 虚拟列表停在"30 卡紧凑占位"（docH≈winH，window 滚不动），滚动无法加载更多，
    # 会被误判到底。Page.bringToFront 把 tab 置前台后，页面恢复异步加载并自动展开
    # （实测 docH 777→3091，window 变可滚）。
    # v1.6.2 前台化持久化：bringToFront 非一劳永逸——实测后 8s visibilityState 会
    # 自动退回 hidden（焦点被抢），页面再次冻结。故展开等待/滚动循环每轮都补一次，
    # 反复提醒浏览器"页面一直在用"。
    def front():
        try:
            bridge.cdp("Page.bringToFront")
        except Exception:
            pass

    front()

    # 首屏卡片轮询：页面加载/展开需要时间，等待 .note-item 出现（≤20s）
    deadline = time.time() + 20.0
    while time.time() < deadline and not cards:
        front()
        ingest(fetch_user_notes(bridge))
        if not cards:
            time.sleep(2.0)
    log("  用户主页首屏笔记卡片 %d 张（新 %d，滚动目标 %d）" % (len(cards), fresh(), target))
    if not cards:
        return cards, 0, False

    # 展开等待：window 不可滚（docH≈winH）时，等页面异步展开
    st = doc_state()
    if st and st.get("docH", 0) <= st.get("winH", 0) + 10:
        log("  tab 置前台，等待页面展开（docH=%s≈winH=%s）..." % (st.get("docH"), st.get("winH")))
        deadline = time.time() + 20.0
        while time.time() < deadline:
            front()
            time.sleep(2.0)
            st = doc_state()
            if st and st.get("docH", 0) > st.get("winH", 0) + 10:
                log("  页面已展开（docH=%s）" % st.get("docH"))
                break
        else:
            log("  等待 20s 仍未展开，按内容不足处理")

    scrolls, no_gain, last_doc_h = 0, 0, -1
    while fresh() < target and scrolls < max_scrolls:
        front()   # 每轮滚动前确保前台（防 tab 被切回后台冻结加载器）
        st = _scroll_to_bottom(bridge)
        scrolls += 1
        time.sleep(random.uniform(2.5, 4.0))   # 防抖 + 等页面加载/虚拟列表换内容
        before = len(cards)
        ingest(fetch_user_notes(bridge))
        added = len(cards) - before
        mode = st.get("mode") if isinstance(st, dict) else "?"
        doc_h = st.get("h") if isinstance(st, dict) else -1
        log("  scroll %d: cards += %d（累计 %d，新 %d）mode=%s docH=%s"
            % (scrolls, added, len(cards), fresh(), mode, doc_h))
        if added == 0:
            # 无新增不一定到底：docH 仍在增长说明页面还在加载/展开，不累计 no_gain
            if doc_h <= last_doc_h:
                no_gain += 1
                if no_gain >= no_gain_limit:
                    # v1.6.2 到底前强制验证：防"提取窗口错过 + 加载器暂歇"的假到底。
                    # 触发前再做一次底部抖动 + 长等待 + 再提取；有新增则重置继续，
                    # 仍无新增才确认到底（exhausted）。二次深度采集曾在此误判。
                    front()
                    _scroll_to_bottom(bridge)
                    time.sleep(6.0)
                    before_v = len(cards)
                    ingest(fetch_user_notes(bridge))
                    if len(cards) > before_v:
                        log("  到底判定前强制验证发现新增 %d 卡，继续滚动"
                            % (len(cards) - before_v))
                        no_gain = 0
                        last_doc_h = -1
                        continue
                    break
            else:
                no_gain = 0
        else:
            no_gain = 0
        last_doc_h = doc_h if doc_h >= 0 else last_doc_h
    exhausted = no_gain >= no_gain_limit
    if fresh() < target:
        reason = "已到底" if exhausted else "滚动预算耗尽"
        log("  主页新笔记不足目标（新 %d/%d，累计 %d），%s"
            % (fresh(), target, len(cards), reason))
    return cards, scrolls, exhausted


def collect_user(bridge, query, out_path, limit_users=0, fetch_notes=False,
                 notes_limit=0, with_profile=False, delay=1.0):
    """用户通道采集：搜索 -> 用户 channel -> 用户实体列表。
    fetch_notes=True 时逐用户进主页取笔记卡片清单（id/token/title，含可开详情的 token；
    正文/图可在后续用这些 token 批量采集）。with_profile=True 时进主页读资料卡写 u['profile']。
    返回统计 dict。"""
    open_user_search(bridge, query)
    users = fetch_user_cards(bridge)
    if limit_users and limit_users > 0:
        users = users[:limit_users]
    result = {"query": query, "users": users, "notes": []}
    if fetch_notes or with_profile:
        for i, u in enumerate(users):
            try:
                goto_user_profile(bridge, u["user_id"], u["token"])
                if with_profile:
                    prof = read_user_profile(bridge)
                    if prof:
                        u["profile"] = prof
                if fetch_notes:
                    notes = fetch_user_notes(bridge)
                    if notes_limit and notes_limit > 0:
                        notes = notes[:notes_limit]
                    u["notes"] = notes
                    result["notes"].extend(notes)
                log("  [%d/%d] %s profile=%s 笔记=%d"
                    % (i + 1, len(users), (u.get("nickname") or "")[:12],
                       "y" if u.get("profile") else "n", len(u.get("notes", []))))
            except Exception as e:
                log("  用户主页失败 %s: %s" % (str(u.get("user_id"))[:10], str(e)[:60]))
                if fetch_notes:
                    u["notes"] = []
            time.sleep(delay)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    return {"query": query, "users": len(users), "note_cards": len(result["notes"]),
            "profiles": sum(1 for u in users if u.get("profile")), "out": out_path}


def resolve_user_token(bridge, user_id, author_label):
    """用户级 uid/token 自愈（v1.5.2 放宽）：支持仅凭昵称解析（旧笔记无 uid）。
    走用户搜索通道：open_user_search（搜索→点「用户」channel）→ USER_CARD_JS。
    匹配顺序：uid 精确 → 昵称精确 → 昵称去空格包含；取首个命中。
    返回 (uid, token, candidates)；未命中返回 ("", "", candidates)，
    candidates = [(uid, nickname), ...] 供错误文案提示重名。"""
    open_user_search(bridge, author_label or user_id)
    cands = [(u.get("user_id") or "", u.get("token") or "", (u.get("nickname") or ""))
             for u in fetch_user_cards(bridge)]

    def norm(s):
        return (s or "").replace(" ", "")

    label_n = norm(author_label)
    checks = (
        lambda uid, tok, nick: bool(user_id) and uid == user_id,
        lambda uid, tok, nick: bool(author_label) and nick == author_label,
        lambda uid, tok, nick: bool(label_n) and label_n in norm(nick),
    )
    for pred in checks:
        for uid, tok, nick in cands:
            if pred(uid, tok, nick):
                return uid, tok, cands
    return "", "", cands


def collect_user_notes(bridge, user_id, user_token, out_path, limit=5,
                       download_dir=None, delay=1.0, max_fatal_run=3,
                       resume=True, known_ids_file=None, author_label="",
                       user_target=0, max_scrolls=40,
                       with_comments=False, max_comments=200):
    """按用户采集（2026-09-04）：进用户主页 → 笔记卡片清单 → 逐条采正文/图。
    产物与 collect_keyword 完全同构（{query, notes, stats}），二级入库零改动。
    - user_target>0（v1.6.0）：滚动主页无限流至目标条数，累计采集前 user_target 篇；
      滚动容器 window/div 双兜底，连续 3 次无新增判到底（exhausted），预算 40 次防失控；
      user_target=0（缺省）完全向后兼容：仅首屏，不滚动；
    - limit = 采集配额：滚动模式以 user_target 为准（clamp 1..200）；
      仅首屏模式 clamp 1..30（实测主页首屏 30 张/页）；
    - known_ids_file 去重照常（本词已有 skip / 跨词 dedup 补边，不重开详情）；
    - 卡片无作者行，author 统一回填 author_label；author_id/author_token 填本用户；
    - user_token 缺失时按昵称走用户 channel 搜索自愈（resolve_user_token）；
    - goto_user_profile 失败（token 过期/守护未运行）→ aborted:"goto_profile" 优雅中止。"""
    scrolling = bool(user_target and int(user_target) > 0)
    if scrolling:
        limit = max(1, min(int(user_target), 200))   # 滚动模式：target 即采集配额（二级 UI 上限 200）
    else:
        limit = max(1, min(int(limit or 5), 30))     # 仅首屏：实测主页首屏 30 张/页（v1.5.3 修正）
    query = "用户:%s" % (author_label or user_id)
    writer = NoteWriter(out_path, query=query)
    done = writer.reusable_ids() if resume else set()
    kw_name = safe_name(query)
    known = load_known_ids(known_ids_file)
    if download_dir:
        download_dir = os.path.join(download_dir, kw_name)

    try:
        if not user_id or not user_token:
            # uid/token 双缺自愈（v1.5.2）：仅凭昵称走用户搜索通道解析，
            # 解析到的 uid 回填 —— 采回的笔记从此带真实 author_id
            user_id, user_token, cands = resolve_user_token(bridge, user_id, author_label)
            if user_id:
                log("  已从用户搜索解析到用户 %s（%s）"
                    % (user_id[:12], (author_label or "")[:12]))
        if not (user_id and user_token):
            hint = "；候选: %s" % "、".join(nick for _, _, nick in cands[:3]) if cands else ""
            st = {"query": query, "aborted": "user_lookup",
                  "error": "无法解析用户（昵称搜不到该用户，或未登录）%s" % hint}
            log("  " + st["error"])
            writer.set_stats(st)
            return st
        last_url = goto_user_profile(bridge, user_id, user_token)
    except BridgeError as e:
        st = {"query": query, "aborted": "goto_profile",
              "error": "%s/%s: %s" % (e.code, e.kind, str(e.message)[:160])}
        log("  打开用户主页失败（token 可能过期或守护未运行）：%s" % str(e.message)[:80])
        writer.set_stats(st)
        return st
    # —— 卡片清单：滚动累积（v1.6.0，user_target>0）或仅首屏（向后兼容）——
    if scrolling:
        # v1.6.1：known 卡不占 target 名额（配额语义与搜索页一致），
        # 否则已采过首屏的博主 target=30 时 0 滚动空转、0 新笔记
        cards, scrolls, exhausted = _scroll_collect_cards(
            bridge, limit, max_scrolls=max_scrolls,
            known_ids=set(known.keys()) if known else set())
    else:
        cards = fetch_user_notes(bridge)
        scrolls, exhausted = 0, False
    log("  用户主页笔记卡片 %d 张（配额 %d）" % (len(cards), limit))
    if not cards:
        st = {"query": query, "aborted": "no_cards",
              "scrolls": scrolls, "exhausted": exhausted}
        writer.set_stats(st)
        return st

    new_count = 0
    stats = {"query": query, "ok": 0, "no_token": 0, "empty": 0, "error": 0,
             "dedup": 0, "new_ok": 0, "img_total": 0, "img_saved": 0,
             "skipped": 0, "untouched": 0, "cards": len(cards),
             "scrolls": scrolls, "exhausted": exhausted, "notes": []}
    fatal_run = 0

    def make_rec(card, idx, status):
        """与 collect_keyword 的流水记录同构；author 统一回填，author_* 填本用户。"""
        nid = card.get("id")
        return {"id": nid, "token": card.get("token", ""), "id_kind": card.get("id_kind"),
                "is_note": card.get("is_note"), "is_trending": card.get("is_trending"),
                "title": card.get("title", ""), "author": author_label,
                "author_id": user_id, "author_token": user_token, "avatar": "",
                "date": card.get("date", ""), "likes": card.get("likes", ""),
                "cover": card.get("cover", ""), "content": "", "images": [],
                "images_downloaded": 0, "status": status,
                "pos": idx + 1, "pos_total": len(cards),
                "url": note_url(nid, card["token"]) if card.get("token") else ""}

    for i, c in enumerate(cards):
        nid = c.get("id")
        if resume and nid in done:
            stats["skipped"] += 1
            writer.upsert(make_rec(c, i, "skipped_resume"))
            log("  [%d/%d] 复用已采集 %s" % (i + 1, len(cards), (c.get("title") or "")[:16]))
            continue
        if nid in known:
            if kw_name in known[nid]:
                stats["skipped"] += 1
                writer.upsert(make_rec(c, i, "skipped_known"))
                log("  [%d/%d] 去重跳过（本词已有） %s" % (i + 1, len(cards), nid[:10]))
            else:
                stats["dedup"] += 1
                writer.upsert(make_rec(c, i, "dedup"))
                log("  [%d/%d] 去重 %s（库内已有，仅记跨词待补边）"
                    % (i + 1, len(cards), nid[:10]))
            continue
        if not c.get("token"):
            stats["no_token"] += 1
            writer.upsert(make_rec(c, i, "no_token"))
            log("  [%d/%d] 无 token，跳过 %s" % (i + 1, len(cards), nid[:10]))
            continue
        if new_count >= limit:
            stats["untouched"] = len(cards) - i
            log("  配额已满（%d/%d），剩余 %d 张记为 untouched"
                % (new_count, limit, len(cards) - i))
            for j in range(i, len(cards)):
                writer.upsert(make_rec(cards[j], j, "untouched"), flush=False)
            writer.flush()   # 批量写：避免连续 os.replace 撞上二级轮询读文件（WinError 5）
            break
        new_count += 1

        rec = make_rec(c, i, "")
        url = note_url(nid, c["token"])
        try:
            if not bridge.tab_alive():
                bridge.ensure_tab(last_url)
            cap = Capture(bridge, url, bind_url=last_url)
            det, ns = read_detail_and_state(bridge, nid)
            _st = bool(ns.get("ok"))
            rec["content"] = (ns.get("desc") if _st else "") or det.get("desc") or ""
            rec["images"] = (ns.get("images") if _st else []) or det.get("images") or []
            rec["status"] = "ok" if det.get("hasDesc") else ("no_desc" if rec["images"] else "empty")
            merge_state_into_rec(rec, ns)
            if with_comments:
                _cmts, _more = collect_comments(bridge, nid, max_comments=max_comments)
                rec["comments"] = _cmts
                rec["comments_collected"] = len(_cmts)
                rec["comments_more"] = bool(_more)
            if download_dir and rec["images"]:
                r = download_images_dc(bridge, cap, nid, rec["images"], download_dir)
                rec["images_downloaded"] = r["saved"]
                rec["img_result"] = r
                stats["img_total"] += r["total"]
                stats["img_saved"] += r["saved"]
            last_url = url
            fatal_run = 0
        except BridgeError as e:
            rec["status"] = "error:%s:%s" % (e.kind, str(e.message)[:80])
            stats["error"] += 1
            fatal_run += 1
            log("  [%d/%d] 异常 %s/%s: %s" % (i + 1, len(cards), e.code, e.kind, e.message[:70]))
        except Exception as e:
            rec["status"] = "error:%s" % str(e)[:80]
            stats["error"] += 1
            fatal_run += 1

        stats[rec["status"] if rec["status"] in stats else "error"] = \
            stats.get(rec["status"] if rec["status"] in stats else "error", 0) + 1
        if rec["status"] == "ok":
            stats["new_ok"] += 1
        stats["notes"].append({"id": nid[:12], "status": rec["status"],
                               "imgs": len(rec["images"]), "dl": rec["images_downloaded"]})
        writer.upsert(rec)
        log("  [%d/%d] %s | %s | %d字 %d图 下%d"
            % (i + 1, len(cards), nid[:10], rec["status"],
               len(rec["content"]), len(rec["images"]), rec["images_downloaded"]))

        if fatal_run >= max_fatal_run:
            log("  连续 %d 帖失败，中止本用户（已落盘成果保留）" % fatal_run)
            stats["aborted"] = "fatal_run"
            stats["untouched"] = len(cards) - i - 1
            for j in range(i + 1, len(cards)):
                writer.upsert(make_rec(cards[j], j, "untouched"), flush=False)
            writer.flush()
            break
        time.sleep(delay)

    writer.set_stats(stats)
    return stats


# ---------------------------------------------------------------- 采集编排

def load_known_ids(path):
    """读取采集去重清单：每行 `note_id<TAB>kw1,kw2` -> {note_id: set(kw)}。
    由调用方（探索台）在任务启动前从库内导出；无文件则返回空 dict（=不去重）。"""
    known = {}
    if not path or not os.path.isfile(path):
        return known
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            nid = parts[0].strip()
            if not nid:
                continue
            kws = set()
            if len(parts) > 1 and parts[1].strip():
                kws = set(k.strip() for k in parts[1].split(",") if k.strip())
            known[nid] = kws
    return known


def collect_keyword(bridge, query, out_path, limit=8, download_dir=None,
                    resume=True, delay=1.0, max_fatal_run=3, notes_out=None,
                    known_ids_file=None, filters=None, search_type=51,
                    with_comments=False, max_comments=200):
    """单关键词采集。返回统计 dict。

    采集时去重（2026-09-03 需求）：known_ids_file 给出库内已有
    note_id -> 已属关键词集合。已知笔记**不打开详情页、不下载图片**：
    本词下已存在的整体跳过；跨词出现仅写 status="dedup" 精简记录，
    入库时只补一条 note_keywords 边（tag 首采时已提取，零网络成本）。
    配额 limit 只计新笔记的采集动作；翻完当前搜索页仍不足则提前结束。

    筛选（2026-09-03 新增）：filters 为 dict（key=FILTER_DIMS 维度，value=UI 文本），
    在 goto_search 后逐维度点击筛选面板选项，再提取卡片；卡片 DOM 结构不变。

    检索语义（2026-09-04 新增）：search_type=51 笔记全文检索；=54 话题模式
    （话题聚焦，复用同一条链路）。"""
    writer = NoteWriter(out_path, query=query)
    done = writer.reusable_ids() if resume else set()
    kw_name = safe_name(query)
    known = load_known_ids(known_ids_file)
    # 图片落在 imgs/<关键词>/<note_id>/ ，与既有数据结构一致
    if download_dir:
        download_dir = os.path.join(download_dir, kw_name)

    try:
        goto_search(bridge, query, type=search_type)
    except BridgeError as e:
        # 扩展未连接 / 守护未起时不必让整条任务崩成 traceback：
        # 记录原因并优雅中止（二级会把原因显示在任务行）
        st = {"query": query, "aborted": "goto_search",
              "error": "%s/%s: %s" % (e.code, e.kind, str(e.message)[:160])}
        log("  打开搜索页失败（扩展未连接或守护进程未运行）：%s" % str(e.message)[:80])
        writer.set_stats(st)
        return st
    ok_login, info = bridge.check_login()
    if not ok_login:
        log("  登录态异常: %s" % info)
        st = {"query": query, "aborted": "login", "info": info}
        writer.set_stats(st)
        return st

    filter_result = None
    if filters:
        filter_result = apply_filters(bridge, filters)
    try:
        cards = fetch_cards(bridge)
    except BridgeError as e:
        st = {"query": query, "aborted": "fetch_cards",
              "error": "%s/%s: %s" % (e.code, e.kind, str(e.message)[:160])}
        log("  取卡片失败：%s" % str(e.message)[:80])
        writer.set_stats(st)
        return st
    log("  搜索到卡片 %d 张，有 token %d 张"
        % (len(cards), sum(1 for c in cards if c.get("token"))))
    if not cards:
        st = {"query": query, "aborted": "no_cards"}
        writer.set_stats(st)
        return st

    quota = limit if (limit and limit > 0) else None
    new_count = 0
    stats = {"query": query, "ok": 0, "no_token": 0, "empty": 0, "error": 0,
             "dedup": 0, "new_ok": 0, "img_total": 0, "img_saved": 0,
             "skipped": 0, "untouched": 0, "cards": len(cards), "notes": []}
    if filter_result:
        stats["filter"] = filter_result
    fatal_run = 0
    last_url = search_url(query, search_type)

    def make_rec(card, idx, status):
        """构造一条流水记录：每张卡片都留痕（位置 pos/pos_total + 原帖 url），
        未触及（untouched）与跳过（skipped_*）也记，便于事后追溯"为什么没采到"。"""
        nid = card.get("id")
        return {"id": nid, "token": card.get("token", ""), "id_kind": card.get("id_kind"),
                "is_note": card.get("is_note"), "is_trending": card.get("is_trending"),
                "title": card.get("title", ""), "author": card.get("author", ""),
                "author_id": card.get("author_id", ""), "author_token": card.get("author_token", ""),
                "avatar": card.get("avatar", ""),
                "date": card.get("date", ""), "likes": card.get("likes", ""),
                "cover": card.get("cover", ""), "content": "", "images": [],
                "images_downloaded": 0, "status": status,
                "pos": idx + 1, "pos_total": len(cards),
                "url": note_url(nid, card["token"]) if card.get("token") else ""}

    for i, c in enumerate(cards):
        nid = c.get("id")
        if resume and nid in done:
            stats["skipped"] += 1
            writer.upsert(make_rec(c, i, "skipped_resume"))
            log("  [%d/%d] 复用已采集 %s" % (i + 1, len(cards), (c.get("title") or "")[:16]))
            continue

        # —— 采集时去重：库内已知笔记不重开详情页、不重下图（不占配额） ——
        if nid in known:
            if kw_name in known[nid]:
                stats["skipped"] += 1
                writer.upsert(make_rec(c, i, "skipped_known"))
                log("  [%d/%d] 去重跳过（本词已有） %s" % (i + 1, len(cards), nid[:10]))
            else:
                stats["dedup"] += 1
                writer.upsert(make_rec(c, i, "dedup"))
                log("  [%d/%d] 去重 %s（库内已有，仅记跨词待补边）"
                    % (i + 1, len(cards), nid[:10]))
            continue

        # F5：无 token 不打开详情页，记 no_token 而非 empty（推荐位，不占配额）
        if not c.get("token"):
            stats["no_token"] += 1
            writer.upsert(make_rec(c, i, "no_token"))
            log("  [%d/%d] %s -> no_token（id_kind=%s，跳过不打开）"
                % (i + 1, len(cards), nid[:10], c.get("id_kind")))
            continue

        # 配额只计新笔记：翻完当前搜索页仍不足则提前结束（不翻页）
        if quota is not None and new_count >= quota:
            stats["untouched"] = len(cards) - i
            log("  新笔记配额已满（%d/%d），停止；剩余 %d 张记为 untouched"
                % (new_count, quota, len(cards) - i))
            for j in range(i, len(cards)):
                writer.upsert(make_rec(cards[j], j, "untouched"), flush=False)
            writer.flush()   # 批量写：避免连续 os.replace 撞上二级轮询读文件（WinError 5）
            break
        new_count += 1

        rec = make_rec(c, i, "")

        url = note_url(nid, c["token"])
        try:
            if not bridge.tab_alive():
                bridge.ensure_tab(last_url)
            cap = Capture(bridge, url, bind_url=last_url)
            det, ns = read_detail_and_state(bridge, nid)
            _st = bool(ns.get("ok"))
            rec["content"] = (ns.get("desc") if _st else "") or det.get("desc") or ""
            rec["images"] = (ns.get("images") if _st else []) or det.get("images") or []
            rec["status"] = "ok" if det.get("hasDesc") else ("no_desc" if rec["images"] else "empty")
            merge_state_into_rec(rec, ns)
            if with_comments:
                _cmts, _more = collect_comments(bridge, nid, max_comments=max_comments)
                rec["comments"] = _cmts
                rec["comments_collected"] = len(_cmts)
                rec["comments_more"] = bool(_more)

            if download_dir and rec["images"]:
                r = download_images_dc(bridge, cap, nid, rec["images"], download_dir)
                rec["images_downloaded"] = r["saved"]
                rec["img_result"] = r
                stats["img_total"] += r["total"]
                stats["img_saved"] += r["saved"]
            last_url = url
            fatal_run = 0
        except BridgeError as e:
            rec["status"] = "error:%s:%s" % (e.kind, str(e.message)[:80])
            stats["error"] += 1
            if e.kind in ("no_tab", "transient"):
                fatal_run += 1
            else:
                fatal_run += 1
            log("  [%d/%d] 异常 %s/%s: %s" % (i + 1, len(cards), e.code, e.kind, e.message[:70]))
        except Exception as e:
            rec["status"] = "error:%s" % str(e)[:80]
            stats["error"] += 1
            fatal_run += 1

        stats[rec["status"] if rec["status"] in stats else "error"] = \
            stats.get(rec["status"] if rec["status"] in stats else "error", 0) + 1
        if rec["status"] == "ok":
            stats["new_ok"] += 1
        stats["notes"].append({"id": nid[:12], "status": rec["status"],
                               "imgs": len(rec["images"]), "dl": rec["images_downloaded"]})
        writer.upsert(rec)
        log("  [%d/%d] %s | %s | %d字 %d图 下%d"
            % (i + 1, len(cards), nid[:10], rec["status"],
               len(rec["content"]), len(rec["images"]), rec["images_downloaded"]))

        if fatal_run >= max_fatal_run:
            log("  连续 %d 帖失败，中止本关键词（已落盘成果保留）" % fatal_run)
            stats["aborted"] = "fatal_run"
            stats["untouched"] = len(cards) - i - 1
            for j in range(i + 1, len(cards)):
                writer.upsert(make_rec(cards[j], j, "untouched"), flush=False)
            writer.flush()
            break
        time.sleep(delay)

    writer.set_stats(stats)
    return stats


def repair_keyword(bridge, json_path, download_dir, delay=1.2, only_missing=True):
    """对已有 JSON 补图。历史 JSON 无 token -> 重新搜索取 token。"""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    query = data.get("query") or os.path.splitext(os.path.basename(json_path))[0]
    notes = data.get("notes") or []
    # 图片落在 imgs/<关键词>/<note_id>/ ，与既有数据结构一致
    download_dir = os.path.join(download_dir, safe_name(query))

    targets = []
    for n in notes:
        if n.get("status") != "ok":
            continue
        want = len(n.get("images") or [])
        have = n.get("images_downloaded") or 0
        if only_missing and have >= want and want > 0:
            continue
        if want == 0:
            continue
        targets.append(n)

    if not targets:
        return {"query": query, "targets": 0, "fixed": 0, "saved": 0}

    tmap, cards = fetch_token_map(bridge, query)
    log("  重新搜索取到 token: %d 个（卡片 %d）" % (len(tmap), len(cards)))

    fixed = saved_total = 0
    last_url = search_url(query)
    for i, n in enumerate(targets):
        nid = n.get("id")
        token = n.get("token") or tmap.get(nid)
        try:
            if token:
                n["token"] = token
                url = note_url(nid, token)
                if not bridge.tab_alive():
                    bridge.ensure_tab(last_url)
                cap = Capture(bridge, url, bind_url=last_url)
                last_url = url
            else:
                # 无 token：笔记图是公开 CDN，在登录态搜索页直接强制请求即可触发抓包
                # （并入旧 xhs_batch_notoken_download.py 的能力，免导航详情页）
                cap = Capture(bridge, search_url(query), bind_url=last_url,
                              wait_selector=".note-item")
                last_url = search_url(query)
            r = download_images(bridge, cap, nid, n.get("images") or [], download_dir)
            n["images_downloaded"] = r["saved"]
            n["img_result"] = r
            saved_total += r["saved"]
            if r["saved"] >= r["total"] and r["total"] > 0:
                fixed += 1
            log("  [%d/%d] %s | %d/%d 张 | %s%s"
                % (i + 1, len(targets), nid[:10], r["saved"], r["total"],
                   r.get("note", ""), "" if token else " [no-token模式]"))
        except BridgeError as e:
            log("  [%d/%d] 异常 %s/%s: %s" % (i + 1, len(targets), e.code, e.kind, e.message[:70]))
        time.sleep(delay)
        # 增量写回
        tmp = json_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, json_path)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return {"query": query, "targets": len(targets), "fixed": fixed, "saved": saved_total}


def report(json_dir):
    """逐词统计 ok / no_token / empty / error / 图数。"""
    rows = []
    for fn in sorted(os.listdir(json_dir)):
        # 跳过内部文件（_batch_result.json 等）与非对象载荷
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        p = os.path.join(json_dir, fn)
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        notes = d.get("notes") or []
        st = {"ok": 0, "no_token": 0, "empty": 0, "error": 0, "other": 0}
        want = have = 0
        for n in notes:
            s = str(n.get("status", ""))
            key = s if s in st else ("error" if s.startswith("error") else "other")
            st[key] += 1
            want += len(n.get("images") or [])
            have += n.get("images_downloaded") or 0
        rows.append({"file": fn[:-5], "notes": len(notes), **st,
                     "img_want": want, "img_have": have,
                     "gap": want - have})
    return rows


def sha256_of(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
