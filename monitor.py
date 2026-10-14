#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
华山医院(浦东院区)可约号源监测器 — GitHub Actions 版
数据源 : 114名医导航移动端免登录接口 (GET, 无需登录)
信号   : 医生 remainnum > 0 (放号新释放 / 他人退号弃号)
推送   : Server酱(方糖) sctapi.ftqq.com -> 微信
状态   : state.json (由 workflow 的 actions/cache 跨 run 传递)
退出码 : 0=正常(含瞬时网络错误)  1=配置错误  3=接口结构异常(触发 GitHub 失败邮件哨兵)
"""
import json, os, random, sys, time
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta

BJT = timezone(timedelta(hours=8))                 # 北京时间 UTC+8
STATE_FILE = "state.json"
HTTP_TIMEOUT, HTTP_RETRIES, PUSH_RETRIES = 15, 3, 3

HEADERS_LIST = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Referer": "https://m.114-91.com/doctor/list",
    "Accept": "application/json, text/plain, */*",
}
HEADERS_DETAIL = dict(HEADERS_LIST, Referer="https://m.114-91.com/doctor/detail")

LIST_API = DETAIL_API = "https://m.114-91.com/common/api"
HOSPITAL_ID = 209

# ---------- 医生配置（id 均以 114 接口 2026-10-13 实测为准） ----------
DOCTORS = [
    {"id": 56654, "keyword": "齐思思", "name": "齐思思", "section": "皮肤科",
     "level": "副主任医师", "sessions": "周二上午 / 周四下午（40元专家门诊）"},
    {"id": 45011, "keyword": "周丽诺", "name": "周丽诺", "section": "内分泌科",
     "level": "主任医师", "sessions": "周四上午 / 周四下午（50元专家门诊）"},
]

# ---------- 可调参数（环境变量覆盖） ----------
def _env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip())
    except Exception:
        return default

COOLDOWN_MIN = _env_int("ALERT_COOLDOWN_MIN", 15)  # 上升沿连报最小间隔(防接口抖动,分钟)
DAILY_CAP = _env_int("DAILY_PUSH_CAP", 5)          # 每日推送总上限(Server酱免费版=5条/天)

# ---------- 基础工具 ----------
def now_bjt():
    return datetime.now(BJT)

def today_str():
    return now_bjt().strftime("%Y-%m-%d")

def log(msg, level="INFO"):
    print("[%s] %-5s %s" % (now_bjt().strftime("%Y-%m-%d %H:%M:%S"), level, msg), flush=True)

def gh_error(msg):   # GitHub Actions 注解, 失败步骤红色高亮
    print("::error::%s" % msg, flush=True)

def backoff_sleep(i):
    time.sleep([1, 2, 4][min(i, 2)] + random.uniform(0, 1))

def fetch_raw(url, headers, retries=HTTP_RETRIES):
    """带重试的 GET; 返回 (text, None) 或 (None, 网络/HTTP错误)"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return r.read().decode("utf-8", "ignore"), None
        except Exception as e:
            last = e
            log("fetch failed (%d/%d): %s" % (i + 1, retries, e), "WARN")
            if i < retries - 1:
                backoff_sleep(i)
    return None, last

def parse_json(text):
    try:
        return json.loads(text), None
    except Exception as e:
        return None, e

def find_doctor(payload, doc_id):
    if not isinstance(payload, dict):
        return None, "payload 不是 object"
    lst = payload.get("list")
    if not isinstance(lst, list):
        return None, "响应缺少 list[]"
    for it in lst:
        if isinstance(it, dict) and str(it.get("id")) == str(doc_id):
            return it, None
    return None, "医生 %s 不在 list[] 中(n=%d)" % (doc_id, len(lst))

def to_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None

# ---------- state ----------
def default_state():
    return {"version": 1, "doctors": {}, "daily": {"date": today_str(), "sent": 0},
            "last_check_ts": None}

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s, dict):
            raise ValueError("state 不是 object")
        s.setdefault("doctors", {})
        s.setdefault("daily", {"date": today_str(), "sent": 0})
        return s
    except Exception as e:
        log("state 加载失败, 使用默认值: %s" % e, "WARN")
        return default_state()

def save_state(s):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log("state 保存失败: %s" % e, "WARN")

# ---------- Server酱推送 ----------
def push_serverchan(title, desp, sendkey):
    url = "https://sctapi.ftqq.com/%s.send" % sendkey
    body = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": HEADERS_LIST["User-Agent"]})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            text = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:      # 业务错误(如额度不足)也可能是 4xx+JSON
        text = e.read().decode("utf-8", "ignore")
    try:
        j = json.loads(text)
    except Exception:
        return False, "non-JSON: %s" % text[:200]
    if j.get("code") == 0:
        return True, text[:200]
    return False, "code=%s message=%s" % (j.get("code"), j.get("message"))

def push_with_retry(title, desp, sendkey):
    detail = ""
    for i in range(PUSH_RETRIES):
        try:
            ok, detail = push_serverchan(title, desp, sendkey)
        except Exception as e:
            ok, detail = False, "exc: %s" % e
        if ok:
            return True, detail
        log("push failed (%d/%d): %s" % (i + 1, PUSH_RETRIES, detail), "WARN")
        if i < PUSH_RETRIES - 1:
            time.sleep([2, 5, 10][min(i, 2)] + random.uniform(0, 1))
    return False, detail

# ---------- 文案 ----------
def build_message(doc, remain, detail_block=None):
    title = ("【抢号】%s(%s)有号 剩%d个" % (doc["name"], doc["section"], remain))
    title = title.replace("\n", " ")[:32]          # Server酱: title<=32字符且禁换行
    lines = [
        "**%s**（%s·%s）出现可约号源！" % (doc["name"], doc["section"], doc["level"]),
        "",
        "- 剩余号数：**%d**" % remain,
        "- 医院：复旦大学附属华山医院（浦东院区）",
        "- 目标班次：%s" % doc["sessions"],
        "- 检测时间：%s（北京时间）" % now_bjt().strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "**立即抢号：** 微信 → 华山医院公众号 → 就医助手 → 门诊预约 → 选「浦东院区」→ 搜「%s」" % doc["name"],
        "",
        "**注意：**",
        "1. 号数为 114 聚合值，可能含特需/涉外档或其它班次，具体班次与价格请以 App 内显示为准；",
        "2. 若打开时已约满，说明刚被抢完，可等下一轮放号或使用候补；",
        "3. 已开候补的用户，有人退号系统会自动补位，无需手动刷新。",
    ]
    if detail_block:
        lines += ["", detail_block]
    return title, "\n".join(lines)

def fetch_clinic_detail(doc_id):
    """尽力而为: DoctorDetailApp 提取 cliniclist 摘要; 任何失败返回 None, 不影响主流程"""
    url = DETAIL_API + "?" + urllib.parse.urlencode({"cmd": "DoctorDetailApp", "doctorid": doc_id})
    raw, err = fetch_raw(url, HEADERS_DETAIL, retries=2)
    if err:
        return None
    data, jerr = parse_json(raw)
    if jerr or not isinstance(data, dict):
        return None
    cl = data.get("cliniclist")
    if isinstance(cl, list) and cl:
        parts = []
        for e in cl[:8]:
            if isinstance(e, dict):
                kv = ["%s=%s" % (k, v) for k, v in e.items() if v not in ("", None, [], {})]
                parts.append("; ".join(kv)[:160])
            else:
                parts.append(str(e)[:160])
        return "114排班明细（请以App为准）：\n" + "\n".join("- " + p for p in parts)
    info = data.get("clinicinfo")
    if isinstance(info, str) and info.strip():
        return "114提示：" + info.strip()[:120]
    return None

# ---------- 报警判定 ----------
# 优化策略：仅在 remainnum 从 0 跳到有号（上升沿 = 新放号 / 新退号）时提醒；
# 号源持续挂着（remain>0 且 prev>0）不再续报，避免浪费方糖每日 5 条免费额度。
def should_alert(dstate, state, remain, prev):
    if state["daily"].get("date") != today_str():          # 北京时间每日 0 点重置
        state["daily"] = {"date": today_str(), "sent": 0}
    if state["daily"].get("sent", 0) >= DAILY_CAP:
        return False, "达到每日推送上限 %d 条(Server酱免费额度)" % DAILY_CAP
    if prev == 0 and remain > 0:                          # 上升沿：新放号 / 新退号
        last_ts = dstate.get("last_alert_ts")
        gap = None
        if last_ts:
            try:
                gap = (now_bjt() - datetime.fromisoformat(last_ts)).total_seconds() / 60
            except Exception:
                gap = None
        if gap is not None and gap < COOLDOWN_MIN:         # 防接口抖动导致的 0→1→0→1 连报
            return False, "上升沿冷却中(距上次 %.1f 分钟)" % gap
        return True, "新放号/退号(0→%d)" % remain
    return False, "非上升沿(持续有号不重复提醒)"

# ---------- 单个医生的判定与动作 ----------
def evaluate(doc, item, state, ctx):
    dstate = state["doctors"].get(str(doc["id"]),
                                  {"last_remainnum": 0, "alerted": False, "last_alert_ts": None})
    remain = to_int(item.get("remainnum"))
    if remain is None:
        log("%s: remainnum 无法解析: %r" % (doc["name"], item.get("remainnum")), "ERROR")
        gh_error("%s remainnum 异常: %r" % (doc["name"], item.get("remainnum")))
        ctx["structural"] = True
        return
    prev = dstate.get("last_remainnum", 0)
    log("%s(%s): remainnum=%s (上轮=%s)" % (doc["name"], doc["section"], remain, prev))

    if remain > 0:
        detail_block = None
        if not ctx["forced"]:
            detail_block = fetch_clinic_detail(doc["id"])   # 尽力而为, 失败不影响主流程
        ok_flag, reason = should_alert(dstate, state, remain, prev)
        if ok_flag:
            title, desp = build_message(doc, remain, detail_block)
            if ctx["forced"]:
                title = "【测试勿抢】" + title   # 伪造数据, 标题必须明示, 防止误导
                desp = "> ⚠️ 本条为**链路测试**（强制伪造号源），非真实号源。\n\n" + desp
            if ctx["skip_push"]:
                log("[SKIP_PUSH] 模拟推送: %s" % title)
                ok = True
            else:
                ok, pdet = push_with_retry(title, desp, ctx["sendkey"])
                log("推送%s (%s)" % ("成功" if ok else "失败", pdet))
            if ok:
                dstate["alerted"] = True
                dstate["last_alert_ts"] = now_bjt().isoformat()
                state["daily"]["sent"] = state["daily"].get("sent", 0) + 1
                ctx["alerts"].append(doc["name"])
                log("RESULT ALERT_SENT doctor=%s remainnum=%s" % (doc["name"], remain))
            else:
                log("推送失败, 不标记 alerted, 下轮重试", "WARN")
                log("RESULT ALERT_PUSH_FAILED doctor=%s remainnum=%s" % (doc["name"], remain))
        else:
            log("有号但跳过推送: %s" % reason)
            log("RESULT STILL_AVAILABLE_SKIP doctor=%s remainnum=%s (%s)" % (doc["name"], remain, reason))
    else:
        if dstate.get("alerted"):
            log("%s 号源已消失, 重新进入监测" % doc["name"])
        dstate["alerted"] = False
        dstate["last_alert_ts"] = None
        log("RESULT NO_SLOT doctor=%s remainnum=0" % doc["name"])

    dstate["last_remainnum"] = remain
    state["doctors"][str(doc["id"])] = dstate

def check_doctor(doc, state, ctx):
    url = LIST_API + "?" + urllib.parse.urlencode({
        "cmd": "DoctorList", "sr": 1, "keyword": doc["keyword"],
        "hospitalid": HOSPITAL_ID, "ipage": 1, "pagesize": 20, "sectiontype": 0})
    raw, err = fetch_raw(url, HEADERS_LIST)
    if err:
        log("%s: 网络错误, 本轮跳过, 下轮重试" % doc["name"], "WARN")
        ctx["transient"] = True
        return
    data, jerr = parse_json(raw)
    if jerr:
        log("%s: JSON解析失败: %s | 响应片段: %s" % (doc["name"], jerr, raw[:300]), "ERROR")
        gh_error("%s JSON解析失败(接口可能变更): %s" % (doc["name"], jerr))
        ctx["structural"] = True
        return
    item, ferr = find_doctor(data, doc["id"])
    if ferr:
        log("%s: %s | 响应片段: %s" % (doc["name"], ferr, raw[:300]), "ERROR")
        gh_error("%s: %s(接口可能变更或医生下架)" % (doc["name"], ferr))
        ctx["structural"] = True
        return
    evaluate(doc, item, state, ctx)

# ---------- 主流程 ----------
def main():
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    skip_push = os.environ.get("SKIP_PUSH", "false").lower() in ("1", "true", "yes")
    force = os.environ.get("FORCE_REMAIN", "").strip()
    if not sendkey and not skip_push:
        log("缺少 SERVERCHAN_SENDKEY (或设 SKIP_PUSH=true 空跑)", "ERROR")
        return 1

    ctx = {"sendkey": sendkey, "skip_push": skip_push, "forced": False,
           "transient": False, "structural": False, "alerts": []}
    state = load_state()
    save_state(state)                    # 确保 state.json 存在, 便于 actions/cache 保存
    log("=== 监测开始 doctors=%d daily_sent=%s/%s force_remain=%r skip_push=%s ==="
        % (len(DOCTORS), state["daily"].get("sent", 0), DAILY_CAP, force, skip_push))

    for idx, doc in enumerate(DOCTORS):
        forced = None
        if force:
            fid, sep, fnum = force.partition(":")
            if sep and fnum.strip().lstrip("-").isdigit() and fid.strip() == str(doc["id"]):
                forced = int(fnum)                       # 形如 "45011:1"
            elif not sep and fid.strip().lstrip("-").isdigit() and idx == 0:
                forced = int(fid)                        # 形如 "1" → 第一位医生
        if forced is not None:
            log("[TEST] 强制 %s remainnum=%d (跳过真实拉取)" % (doc["name"], forced))
            ctx["forced"] = True
            evaluate(doc, {"id": doc["id"], "remainnum": forced}, state, ctx)
            ctx["forced"] = False
        else:
            check_doctor(doc, state, ctx)

    state["last_check_ts"] = now_bjt().isoformat()
    save_state(state)
    log("=== 监测结束 alerts=%d daily_sent=%d/%d transient=%s structural=%s ==="
        % (len(ctx["alerts"]), state["daily"].get("sent", 0), DAILY_CAP,
           ctx["transient"], ctx["structural"]))
    return 3 if ctx["structural"] else 0

if __name__ == "__main__":
    sys.exit(main())
