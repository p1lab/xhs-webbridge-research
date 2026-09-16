# -*- coding: utf-8 -*-
"""
xhs_cli.py -- xhs_bridge 的命令行薄壳（两处字节级一致）

本文件不含任何 urllib / post / base64 逻辑，全部委托 xhs_bridge.py。
默认数据目录 = 本脚本所在目录下的 json/ 与 imgs/。

用法：
  python xhs_cli.py probe                      探活 + 登录态
  python xhs_cli.py smoke  --query "关键词"     单帖端到端探针
  python xhs_cli.py collect --query "关键词" [--limit 8] [--resume] [--known-ids 清单.tsv] [--filter '{"sort":"最新","note_type":"图文"}'] [--search-type 54]
  python xhs_cli.py search --query "关键词" --mode notes|topic|users [--fetch-notes] [--limit N] [--out out.json]
  python xhs_cli.py repair --json json/X.json  补图
  python xhs_cli.py batch  --keywords-file keywords.txt [--limit 8]
  python xhs_cli.py verify-sync --other <另一处 xhs_bridge.py 绝对路径>
  python xhs_cli.py report
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xhs_bridge as X  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JSON = os.path.join(HERE, "json")
DEFAULT_IMGS = os.path.join(HERE, "imgs")

EXIT_OK, EXIT_FAIL, EXIT_LOGIN, EXIT_DRIFT = 0, 1, 2, 3


def _bridge(a):
    return X.Bridge(base=a.base, session=a.session)


def cmd_probe(a):
    b = _bridge(a)
    url = X.search_url(a.query or "穿搭")
    b.ensure_tab(url)
    b.navigate(url, wait_selector=".note-item", timeout=15)
    ok, info = b.check_login()
    print(json.dumps({"session": b.session, "base": b.base,
                      "tab_alive": b.tab_alive(), "login": ok,
                      "info": info}, ensure_ascii=False, indent=1))
    return EXIT_OK if (ok and b.tab_alive()) else EXIT_FAIL


def cmd_smoke(a):
    """单帖端到端：content 非空 且 图全部下载 且 >=1 张。"""
    b = _bridge(a)
    out = a.out or os.path.join(DEFAULT_JSON, "_smoke.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        os.remove(out)
    st = X.collect_keyword(b, a.query, out, limit=1,
                           download_dir=a.download or DEFAULT_IMGS, resume=False)
    notes = []
    try:
        with open(out, "r", encoding="utf-8") as f:
            notes = json.load(f).get("notes") or []
    except Exception:
        pass
    if not notes:
        print(json.dumps({"pass": False, "reason": "no_note", "stats": st},
                         ensure_ascii=False, indent=1))
        return EXIT_FAIL
    n = notes[0]
    ok = (n.get("status") == "ok" and len(n.get("content") or "")) > 0 \
        and n.get("images_downloaded", 0) == len(n.get("images") or []) \
        and len(n.get("images") or []) >= 1
    print(json.dumps({"pass": ok, "status": n.get("status"),
                      "content_len": len(n.get("content") or ""),
                      "images": len(n.get("images") or []),
                      "downloaded": n.get("images_downloaded", 0),
                      "stats": st}, ensure_ascii=False, indent=1))
    return EXIT_OK if ok else EXIT_FAIL


def cmd_collect(a):
    b = _bridge(a)
    out = a.out or os.path.join(DEFAULT_JSON, X.safe_name(a.query) + ".json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # 用户通道：给了 --user-id 或 --author-label 即走 collect_user_notes
    # （v1.5.2 起支持仅昵称：user_id/token 双缺时一级按昵称解析 uid+token）
    if getattr(a, "user_id", "") or getattr(a, "author_label", ""):
        if a.filter or getattr(a, "search_type", 51) != 51:
            print(json.dumps({"error": "--user-id 与 --filter/--search-type 互斥"},
                             ensure_ascii=False))
            return EXIT_FAIL
        st = X.collect_user_notes(b, a.user_id, a.user_token or "", out, limit=a.limit,
                                  download_dir=a.download or DEFAULT_IMGS, resume=a.resume,
                                  known_ids_file=a.known_ids,
                                  author_label=getattr(a, "author_label", "") or "",
                                  user_target=getattr(a, "user_target", 0),
                                  with_comments=getattr(a, "with_comments", False),
                                  max_comments=getattr(a, "max_comments", 200))
    else:
        st = X.collect_keyword(b, a.query, out, limit=a.limit,
                               download_dir=a.download or DEFAULT_IMGS, resume=a.resume,
                               known_ids_file=a.known_ids, filters=X.parse_filters(a.filter),
                               search_type=getattr(a, "search_type", 51),
                               with_comments=getattr(a, "with_comments", False),
                               max_comments=getattr(a, "max_comments", 200))
    print(json.dumps(st, ensure_ascii=False, indent=1))
    # 被中止（扩展未连接 / 无卡片 / 登录异常 / 连续失败）也算失败：
    # 否则二级会把"0 成果"的任务标成完成，误导判断
    if isinstance(st, dict) and st.get("aborted"):
        return EXIT_FAIL
    return EXIT_OK


def cmd_search(a):
    """多检索语义统一入口：notes(51) / topic(54) / users(用户实体)。"""
    b = _bridge(a)
    out = a.out or os.path.join(DEFAULT_JSON, X.safe_name(a.query) + "_%s.json" % a.mode)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if a.mode == "users":
        st = X.collect_user(b, a.query, out, limit_users=a.limit_users,
                            fetch_notes=a.fetch_notes, notes_limit=a.limit,
                            with_profile=a.with_profile)
    else:
        st = X.collect_keyword(b, a.query, out, limit=a.limit,
                               download_dir=a.download or DEFAULT_IMGS, resume=a.resume,
                               known_ids_file=a.known_ids, filters=X.parse_filters(a.filter),
                               search_type=54 if a.mode == "topic" else 51,
                               with_comments=getattr(a, "with_comments", False),
                               max_comments=getattr(a, "max_comments", 200))
    print(json.dumps(st, ensure_ascii=False, indent=1))
    if isinstance(st, dict) and st.get("aborted"):
        return EXIT_FAIL
    return EXIT_OK


def cmd_suggest(a):
    b = _bridge(a)
    out = a.out or os.path.join(DEFAULT_JSON, X.safe_name(a.query) + "_suggest.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    payload = X.suggest(b, a.query, out_path=out)
    print(json.dumps(payload.get("stats", {}), ensure_ascii=False, indent=1))
    print("OUT:", out)
    st = payload.get("stats", {})
    ok = st.get("suggestions", 0) > 0 or st.get("trending_queries", 0) > 0 or st.get("ai_words", 0) > 0
    return EXIT_OK if ok else EXIT_FAIL


def cmd_repair(a):
    b = _bridge(a)
    st = X.repair_keyword(b, a.json, a.download or DEFAULT_IMGS)
    print(json.dumps(st, ensure_ascii=False, indent=1))
    return EXIT_OK if st.get("saved", 0) > 0 or st.get("targets", 0) == 0 else EXIT_FAIL


def cmd_batch(a):
    with open(a.keywords_file, "r", encoding="utf-8") as f:
        kws = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    b = _bridge(a)
    json_dir = a.out_dir or DEFAULT_JSON
    os.makedirs(json_dir, exist_ok=True)
    results, aborted = [], None
    for i, kw in enumerate(kws, 1):
        X.log("===== [%d/%d] %s =====" % (i, len(kws), kw))
        out = os.path.join(json_dir, X.safe_name(kw) + ".json")
        try:
            st = X.collect_keyword(b, kw, out, limit=a.limit,
                                   download_dir=a.download or DEFAULT_IMGS, resume=True,
                                   with_comments=getattr(a, "with_comments", False),
                                   max_comments=getattr(a, "max_comments", 200))
        except Exception as e:
            st = {"query": kw, "aborted": "exception:%s" % str(e)[:120]}
        results.append(st)
        with open(os.path.join(json_dir, "_batch_result.json"), "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
        if st.get("aborted") in ("login",):
            aborted = "login"
            break
    print(json.dumps({"total": len(kws), "done": len(results),
                      "aborted": aborted, "results": results},
                     ensure_ascii=False, indent=1))
    return EXIT_OK if not aborted else EXIT_LOGIN


def cmd_verify_sync(a):
    me = os.path.join(HERE, "xhs_bridge.py")
    if not os.path.exists(a.other):
        print("找不到对照文件: %s" % a.other)
        return EXIT_DRIFT
    h1, h2 = X.sha256_of(me), X.sha256_of(a.other)
    same = h1 == h2
    print(json.dumps({"same": same, "local": me, "local_sha": h1[:16],
                      "other": a.other, "other_sha": h2[:16]},
                     ensure_ascii=False, indent=1))
    if not same:
        print("两处 xhs_bridge.py 已漂移，请先同步再运行。", file=sys.stderr)
    return EXIT_OK if same else EXIT_DRIFT


def cmd_report(a):
    rows = X.report(a.out_dir or DEFAULT_JSON)
    rows = [r for r in rows if not r["file"].startswith("_")]
    tot = {"notes": 0, "img_want": 0, "img_have": 0, "gap": 0}
    for r in rows:
        for k in ("notes", "img_want", "img_have", "gap"):
            tot[k] += r[k]
    print("| %-24s %5s %4s %6s %5s %6s %6s %6s |"
          % ("关键词", "笔记", "ok", "noTok", "empty", "需图", "已有", "缺口"))
    print("|" + "-" * 78 + "|")
    for r in rows:
        print("| %-24s %5d %4d %6d %5d %6d %6d %6d |"
              % (r["file"][:24], r["notes"], r["ok"], r["no_token"],
                 r["empty"], r["img_want"], r["img_have"], r["gap"]))
    print("|" + "-" * 78 + "|")
    print("| %-24s %5d %4s %6s %5s %6d %6d %6d |"
          % ("合计", tot["notes"], "", "", "", tot["img_want"], tot["img_have"], tot["gap"]))
    print(json.dumps(tot, ensure_ascii=False))
    return EXIT_OK


def main():
    ap = argparse.ArgumentParser(description="小红书采集工具")
    ap.add_argument("--base", default=X.DEFAULT_BASE)
    ap.add_argument("--session", default=X.DEFAULT_SESSION)
    ap.add_argument("--quiet", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe");           p.add_argument("--query", default="穿搭"); p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("smoke");           p.add_argument("--query", required=True)
    p.add_argument("--out", default=""); p.add_argument("--download", default=""); p.set_defaults(fn=cmd_smoke)
    p = sub.add_parser("collect");         p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--out", default=""); p.add_argument("--download", default="")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--known-ids", default="",
                   help="去重清单：每行 note_id<TAB>kw1,kw2；已知笔记不重采（探索台注入）")
    p.add_argument("--filter", default="",
                   help='筛选：JSON {"sort":"最新","note_type":"图文","time":"一周内"} 或 k=v,k=v；'
                        '值可用英文别名(latest/image/1w/city...)或UI文本')
    p.add_argument("--search-type", type=int, default=51,
                   help="检索语义：51=笔记全文检索(默认)，54=话题模式（点 #话题 的落点，返回话题聚焦笔记）")
    p.add_argument("--user-id", default="",
                   help="按用户采集：用户 uid（给了即走用户主页通道，query 仅作标签/类别名「用户:xx」）")
    p.add_argument("--user-token", default="",
                   help="按用户采集：用户级 xsec_token（过期会 404/300031，任务失败自带原因）")
    p.add_argument("--author-label", default="",
                   help="按用户采集：作者昵称回填（产物 author 字段与「用户:」类别名）")
    p.add_argument("--user-target", type=int, default=0,
                   help="按用户采集：滚动目标条数（>0 滚动主页无限流至前 N 篇；0=仅首屏，缺省兼容 v1.5.3）")
    p.add_argument("--with-comments", action="store_true", help="逐条采集评论(state滚动翻页,opt-in)")
    p.add_argument("--max-comments", type=int, default=200, help="每帖评论上限")
    p.set_defaults(fn=cmd_collect)
    p = sub.add_parser("search")
    p.add_argument("--query", required=True)
    p.add_argument("--mode", choices=["notes", "topic", "users"], default="notes",
                   help="检索语义：notes=笔记全文，topic=话题聚合，users=用户实体（含 --fetch-notes 展开）")
    p.add_argument("--limit", type=int, default=8, help="notes/topic：采集配额；users：每用户笔记数上限")
    p.add_argument("--limit-users", type=int, default=0, help="users：最多取前 N 个用户（0=全部）")
    p.add_argument("--fetch-notes", action="store_true", help="users：逐用户进主页取笔记卡片清单")
    p.add_argument("--with-profile", action="store_true", help="users：逐用户进主页读资料卡(昵称/号/IP/简介/性别/地区/关注/粉丝/获赞收藏)")
    p.add_argument("--out", default=""); p.add_argument("--download", default="")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--known-ids", default="", help="去重清单（仅 notes/topic）")
    p.add_argument("--filter", default="", help="筛选（仅 notes）")
    p.add_argument("--with-comments", action="store_true", help="notes/topic：逐条采集评论(opt-in)")
    p.add_argument("--max-comments", type=int, default=200, help="每帖评论上限")
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser("suggest");         p.add_argument("--query", required=True)
    p.add_argument("--out", default=""); p.set_defaults(fn=cmd_suggest)
    p = sub.add_parser("repair");          p.add_argument("--json", required=True)
    p.add_argument("--download", default=""); p.set_defaults(fn=cmd_repair)
    p = sub.add_parser("batch");           p.add_argument("--keywords-file", required=True)
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--out-dir", default=""); p.add_argument("--download", default="")
    p.add_argument("--with-comments", action="store_true", help="逐条采集评论(opt-in)")
    p.add_argument("--max-comments", type=int, default=200, help="每帖评论上限")
    p.set_defaults(fn=cmd_batch)
    p = sub.add_parser("verify-sync");     p.add_argument("--other", required=True); p.set_defaults(fn=cmd_verify_sync)
    p = sub.add_parser("report");          p.add_argument("--out-dir", default=""); p.set_defaults(fn=cmd_report)

    a = ap.parse_args()
    if a.quiet:
        X.VERBOSE = False
    sys.exit(a.fn(a) or EXIT_OK)


if __name__ == "__main__":
    main()
