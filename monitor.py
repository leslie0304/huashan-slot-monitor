#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
华山医院(浦东院区)可约号源监测器 — GitHub Actions 版(循环模式)
数据源 : 114名医导航移动端免登录接口 (GET, 无需登录)
信号   : 医生 remainnum > 0 (放号新释放 / 他人退号弃号)
推送   : Server酱(方糖) sctapi.ftqq.com -> 微信
状态   : state.json (由 workflow 的 actions/cache 跨 run 传递)
运行方式: 默认单次查询; 放号窗口内自动进入内循环高频监测(由 workflow 的
         bash 循环每 5 分钟调用一次, 窗口内 monitor.py 自行以 60s/轮盯到窗口结束)
功能   : 上升沿提醒 / 冷却 / 每日5条硬顶 / 12:00&18:00平安简报 /
         放号窗口高频+到点预警 / 门诊前夜20:00退号候补提醒 /
         前夜退号高频窗口(门诊前一晚20:00-23:30按排班自动推导)
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
# weekdays: Python weekday(), 周一=0 ... 周日=6（用于推导门诊前夜提醒日）
DOCTORS = [
    {"id": 56654, "keyword": "齐思思", "name": "齐思思", "section": "皮肤科",
     "level": "副主任医师", "sessions": "周二上午 / 周四下午（40元专家门诊）",
     "weekdays": [1, 3]},
    {"id": 45011, "keyword": "周丽诺", "name": "周丽诺", "section": "内分泌科",
     "level": "主任医师", "sessions": "周四上午 / 周四下午（50元专家门诊）",
     "weekdays": [3]},
]

# ---------- 可调参数（环境变量覆盖） ----------
def _env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip())
    except Exception:
        return default

COOLDOWN_MIN = _env_int("ALERT_COOLDOWN_MIN", 15)  # 常规上升沿连报最小间隔(防抖动,分钟)
DAILY_CAP = _env_int("DAILY_PUSH_CAP", 5)          # 每日推送总上限(Server酱免费版=5条/天)

# 放号窗口: 落入这些时段时, monitor.py 自动以 window["interval"] 秒/轮内循环盯到窗口结束,
# 并在 window["headsup"] 时刻发一条到点预警; window["cooldown"] 可缩短冷却(放号时号源抖动快)。
# date 缺省=每日生效; 填 "YYYY-MM-DD" 则仅当日生效(收到预约页面的放号公告后按需添加)。
DEFAULT_RELEASE_WINDOWS = [
    # end=18:10: 实测放号后约5分钟内号源即被抢光(2026-10-19 陛下口径),
    # 18:10 后的残余时段交给 5 分钟网格兜底(15分钟冷却), 不再占60秒高频
    {"start": "17:35", "end": "18:10", "interval": 60,
     "headsup": "17:55", "cooldown": 3, "label": "晚间放号窗口"},
]

def _load_release_windows():
    raw = os.environ.get("RELEASE_WINDOWS", "").strip()
    if raw:
        try:
            ws = json.loads(raw)
            if isinstance(ws, list) and ws:
                return ws
        except Exception as e:
            log("RELEASE_WINDOWS 解析失败, 使用默认: %s" % e, "WARN")
    return DEFAULT_RELEASE_WINDOWS

RELEASE_WINDOWS = _load_release_windows()

# 门诊前夜(退号)窗口: 由 DOCTORS[*].weekdays 推导"门诊日的前一天"(周一/周三),
# 20:00 起他人取消的次日门诊号多在此区间放出, 以内循环高频盯到 23:30。
# weekdays: Python weekday(), 周一=0 ... 周日=6(与 DOCTORS 一致)。
EVE_WEEKDAYS = sorted({(w - 1) % 7 for doc in DOCTORS for w in doc["weekdays"]})

EVE_WINDOWS = [
    {"start": "20:00", "end": "23:30", "interval": 180, "cooldown": 5,
     "label": "前夜退号窗口", "weekdays": EVE_WEEKDAYS, "max_min": 220},
]

EVE_REMIND_HHMM = "20:00"          # 门诊前夜提醒时刻(北京时间)
EVE_REMIND_SPAN_MIN = 15           # 触发窗口(分钟)
HEADS_UP_SPAN_MIN = 10             # 到点预警触发窗口(分钟)
MAX_WINDOW_RUN_MIN = 120           # 放号窗口内循环保护上限(分钟)

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

def _hhmm_to_min(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)

def in_span(dt, start, end):
    """dt 的时分是否落在 [start, end) 内; start/end 为 'HH:MM' 或分钟数"""
    s = start if isinstance(start, int) else _hhmm_to_min(start)
    e = end if isinstance(end, int) else _hhmm_to_min(end)
    return s <= dt.hour * 60 + dt.minute < e

def window_active(dt, w):
    if w.get("date") and w["date"] != dt.strftime("%Y-%m-%d"):
        return False
    wds = w.get("weekdays")
    if wds and dt.weekday() not in wds:
        return False
    return in_span(dt, w["start"], w["end"])

def current_interval(dt):
    """返回 (间隔秒, 命中的窗口或 None); 前夜退号窗口与放号窗口时段不重叠, 先后无所谓"""
    for w in EVE_WINDOWS + RELEASE_WINDOWS:
        if window_active(dt, w):
            return int(w.get("interval", 60)), w
    return None, None

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
def default_daily():
    return {"date": today_str(), "sent": 0, "w_morning": 0, "w_afternoon": 0,
            "hb_noon": False, "hb_evening": False,
            "service_sent": 0, "heads_done": [], "remind_eve": False}

def default_state():
    return {"version": 1, "doctors": {}, "daily": default_daily(),
            "struct_streak": 0, "last_check_ts": None}

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s, dict):
            raise ValueError("state 不是 object")
        s.setdefault("doctors", {})
        s.setdefault("struct_streak", 0)
        # 北京时间每日 0 点整体重置(集中在此处; 旧版仅在有号时才重置, 会导致无号日
        # 心跳标志永不刷新、次日不再发平安简报的 bug)
        if s.get("daily", {}).get("date") != today_str():
            s["daily"] = default_daily()
        else:
            for k, v in default_daily().items():
                s["daily"].setdefault(k, v)
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

def quota_left(state):
    """Server酱 免费版 5 条/天为总上限; 放号提醒与服务消息共享"""
    d = state["daily"]
    return DAILY_CAP - d.get("sent", 0) - d.get("service_sent", 0)

def send_service(state, ctx, title, desp):
    """服务类消息(平安简报/放号预警/门诊前夜提醒): 不占 sent 放号额度,
    但共享 5 条/天总上限; 额度满则跳过(放号提醒优先)"""
    d = state["daily"]
    if quota_left(state) <= 0:
        log("服务消息跳过(今日额度已满): %s" % title, "WARN")
        return False
    if ctx["skip_push"]:
        log("[SKIP_PUSH] 服务消息: %s" % title)
        d["service_sent"] = d.get("service_sent", 0) + 1
        return True
    ok, pdet = push_with_retry(title, desp, ctx["sendkey"])
    if ok:
        d["service_sent"] = d.get("service_sent", 0) + 1
    log("服务消息%s (%s)" % ("成功" if ok else "失败", pdet))
    return ok

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
def should_alert(dstate, state, remain, prev, cooldown_min):
    if state["daily"].get("date") != today_str():          # 北京时间每日 0 点重置
        state["daily"] = default_daily()
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
        if gap is not None and gap < cooldown_min:         # 防接口抖动导致的 0→1→0→1 连报
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
    ctx["good"] = ctx.get("good", 0) + 1     # 本轮成功取到真实号数(用于重置结构异常连击)
    log("%s(%s): remainnum=%s (上轮=%s)" % (doc["name"], doc["section"], remain, prev))

    if remain > 0:
        detail_block = None
        if not ctx["forced"]:
            detail_block = fetch_clinic_detail(doc["id"])   # 尽力而为, 失败不影响主流程
        ok_flag, reason = should_alert(dstate, state, remain, prev, ctx["cooldown_min"])
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
                _h = now_bjt().hour
                if _h < 12:
                    state["daily"]["w_morning"] = state["daily"].get("w_morning", 0) + 1
                elif _h < 18:
                    state["daily"]["w_afternoon"] = state["daily"].get("w_afternoon", 0) + 1
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

# ---------- 保活通知（心跳）----------
def heartbeat_msgs(state, which):
    if which == "noon":
        label, cnt = "上午 (00:00–11:59)", state["daily"].get("w_morning", 0)
        title = "【平安简报】中午监测正常"
    else:
        label, cnt = "下午 (12:00–17:59)", state["daily"].get("w_afternoon", 0)
        title = "【平安简报】傍晚监测正常"
    lines = [
        "**华山抢号监测器 · 存活确认 ✅**",
        "",
        "监测时段：%s" % label,
        "本时段放号提醒：**%d 次**" % cnt,
    ]
    if cnt == 0:
        lines += ["→ 暂未监测到可约号源（齐思思/周丽诺 浦东院区）。",
                  "→ 收到此条即说明系统正常在跑；号源出现会另发推送。"]
    else:
        lines += ["→ 已有 %d 次放号提醒，请查收微信历史并尽快抢号。" % cnt]
    lines += ["", "系统持续监测（放号窗口内 1 分钟一轮），无需手动。",
              "⚠️ 若您**未收到此条**，说明监测器可能断联，请检查 GitHub Actions 运行状态。"]
    return title, "\n".join(lines)

# ---------- 放号窗口到点预警 ----------
def maybe_headsup(state, ctx, now):
    d = state["daily"]
    for w in RELEASE_WINDOWS:
        hs = w.get("headsup")
        if not hs:
            continue
        if w.get("date") and w["date"] != d.get("date"):
            continue
        if not in_span(now, hs, _hhmm_to_min(hs) + HEADS_UP_SPAN_MIN):
            continue
        key = "%s|%s" % (d.get("date"), w.get("label", hs))
        if key in d.get("heads_done", []):
            continue
        label = w.get("label", "放号")
        title = ("【放号预警】%s即将开始" % label).replace("\n", " ")[:32]
        desp = "\n".join([
            "**放号窗口预警**",
            "",
            "预约页面提示的「%s」即将开始，请立即就位：" % label,
            "微信 → 华山医院公众号 → 就医助手 → 门诊预约 → 选「浦东院区」→ 搜目标医生（齐思思/周丽诺）。",
            "",
            "监测器已进入 **1 分钟高频模式**，一出现可约号会立刻推送。",
            "若页面并无放号公告，请忽略本条。",
        ])
        log("放号预警触发(%s)" % label)
        if send_service(state, ctx, title, desp):
            d.setdefault("heads_done", [])
            if key not in d["heads_done"]:
                d["heads_done"].append(key)

# ---------- 门诊前夜提醒（查退号/挂候补） ----------
# EVE_WEEKDAYS / EVE_WINDOWS 见上方配置区(由 DOCTORS[*].weekdays 推导)

def tomorrow_clinic_doctors(now):
    tw = (now.weekday() + 1) % 7
    return [d for d in DOCTORS if tw in d["weekdays"]]

def maybe_eve_reminder(state, ctx, now):
    d = state["daily"]
    if d.get("remind_eve"):
        return
    if now.weekday() not in EVE_WEEKDAYS:
        return
    if not in_span(now, EVE_REMIND_HHMM, _hhmm_to_min(EVE_REMIND_HHMM) + EVE_REMIND_SPAN_MIN):
        return
    docs = tomorrow_clinic_doctors(now)
    if not docs:
        return
    weekday_cn = "一二三四五六日"
    tmr_cn = "周" + weekday_cn[(now.weekday() + 1) % 7]
    title = ("【门诊前夜】明天(%s)有门诊,查退号" % tmr_cn).replace("\n", " ")[:32]
    lines = [
        "**门诊前夜提醒 · 退号高峰**",
        "",
        "明天（%s）门诊：" % tmr_cn,
    ]
    for doc in docs:
        lines.append("- **%s**（%s·%s）%s" % (doc["name"], doc["section"], doc["level"], doc["sessions"]))
    lines += [
        "",
        "今晚是退号/弃号高峰，请现在就做两件事：",
        "1. 打开 华山医院公众号 → 就医助手 → 门诊预约 → 「浦东院区」，刷目标医生页面：**有可约号直接约**；",
        "2. 若显示约满，立刻点「**候补**」排队——有人退号系统自动补位，无需守着刷新。",
        "",
        "监测器已进入 **高频盯号模式**（今晚 20:00–23:30 约 3 分钟一轮），真出现可约号会立刻推送。",
    ]
    log("门诊前夜提醒触发(%s, %d位医生)" % (tmr_cn, len(docs)))
    if send_service(state, ctx, title, "\n".join(lines)):
        d["remind_eve"] = True

# ---------- 主流程 ----------
def run_once(cooldown_min=None):
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    skip_push = os.environ.get("SKIP_PUSH", "false").lower() in ("1", "true", "yes")
    force = os.environ.get("FORCE_REMAIN", "").strip()
    if not sendkey and not skip_push:
        log("缺少 SERVERCHAN_SENDKEY (或设 SKIP_PUSH=true 空跑)", "ERROR")
        return 1

    ctx = {"sendkey": sendkey, "skip_push": skip_push, "forced": False,
           "transient": False, "structural": False, "alerts": [], "good": 0,
           "cooldown_min": cooldown_min or COOLDOWN_MIN}
    state = load_state()
    save_state(state)                    # 确保 state.json 存在, 便于 actions/cache 保存
    log("=== 监测开始 doctors=%d daily_sent=%s/%s service=%s cooldown=%s force=%r skip_push=%s ==="
        % (len(DOCTORS), state["daily"].get("sent", 0), DAILY_CAP,
           state["daily"].get("service_sent", 0), ctx["cooldown_min"], force, skip_push))

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

    # 服务类消息: 放号预警 / 平安简报(12:00,18:00) / 门诊前夜提醒(20:00)
    now = now_bjt()
    d = state["daily"]
    maybe_headsup(state, ctx, now)
    if now.hour == 12 and now.minute < 15 and not d.get("hb_noon"):
        t, m = heartbeat_msgs(state, "noon")
        if send_service(state, ctx, t, m):
            d["hb_noon"] = True
    if now.hour == 18 and now.minute < 15 and not d.get("hb_evening"):
        t, m = heartbeat_msgs(state, "evening")
        if send_service(state, ctx, t, m):
            d["hb_evening"] = True
    maybe_eve_reminder(state, ctx, now)

    # 结构性异常连击计数: 放号峰值期的瞬时超时/空响应(10-16 18:00 实测)不再一击致命,
    # 连续 3 轮结构异常才判死(rc=3)拉响 GitHub 失败邮件哨兵; 任一轮成功取到号数即清零
    if ctx["structural"]:
        state["struct_streak"] = state.get("struct_streak", 0) + 1
    elif ctx.get("good"):
        state["struct_streak"] = 0
    streak = state.get("struct_streak", 0)
    state["last_check_ts"] = now.isoformat()
    save_state(state)
    log("=== 监测结束 alerts=%d daily_sent=%d/%d service=%d transient=%s structural=%s streak=%d/3 ==="
        % (len(ctx["alerts"]), state["daily"].get("sent", 0), DAILY_CAP,
           state["daily"].get("service_sent", 0), ctx["transient"], ctx["structural"], streak))
    return 3 if streak >= 3 else 0

def window_follow_up():
    """放号/前夜退号窗口内自动进入内循环高频监测, 直到窗口结束(或保护上限)"""
    now = now_bjt()
    interval, win = current_interval(now)
    if not win:
        return 0
    end_min = _hhmm_to_min(win["end"])
    label = win.get("label", "放号窗口")
    cooldown = int(win.get("cooldown", 3))
    max_min = int(win.get("max_min", MAX_WINDOW_RUN_MIN))
    log("进入窗口内循环(%s): %ds/轮, 冷却%dm, 至 %s" % (label, interval, cooldown, win["end"]))
    t0 = time.monotonic()
    while True:
        time.sleep(interval)
        now = now_bjt()
        if now.hour * 60 + now.minute >= end_min:
            log("窗口(%s)结束, 退出内循环" % label)
            return 0
        if (time.monotonic() - t0) / 60 >= max_min:
            log("内循环超 %d 分钟保护退出" % max_min, "WARN")
            return 0
        rc = run_once(cooldown_min=cooldown)
        if rc == 3:
            log("结构性错误(rc=3), 终止内循环以便 GitHub 发失败邮件", "ERROR")
            return 3

def main():
    rc = run_once()
    if rc != 0:
        return rc
    return window_follow_up()

if __name__ == "__main__":
    sys.exit(main())
