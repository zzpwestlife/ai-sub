#!/usr/bin/env python3
"""AI 模型榜单追踪脚本（aihot.virxact.com）

追踪 data.json 中现有模型在 aihot 综合榜上的排名/得分，每日抓取：
  - 数据源：https://aihot.virxact.com/leaderboard（Next.js SSR payload，纯 HTTP 可抓）
  - 存储：  leaderboard_data.json 按本地日期存全量快照（全保留，供趋势/日志派生）
  - 同日重复运行幂等覆盖当天快照；抓取/解析失败绝不覆写旧数据
  - 有变动（排名/分数>±0.5/上下榜）弹 macOS 通知，无变动静默

用法：
  python3 check_leaderboard.py            # 正常检查
  python3 check_leaderboard.py --verbose  # 打印详细过程

arena.ai 的 agent 榜因 Cloudflare 封锁暂未接入，详见《价格监控指南.md》。
"""

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# 与价格监控共用工具函数（日志/配置/原子写入）
from check_prices import VERBOSE, log, load_config, atomic_write_json

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"
LEADERBOARD_FILE = BASE_DIR / "leaderboard_data.json"

AIHOT_URL = "https://aihot.virxact.com/leaderboard"

# data.json 模型 ID → aihot 榜单 slug（2026-08-31 核对）
MODEL_SLUG_MAP = {
    "claude-sonnet-5": "claude-sonnet-5",
    "claude-opus-5": "claude-opus-5",
    "claude-fable-5": "claude-fable-5",
    "gpt-5.6-luna": "gpt-5-6-luna",
    "gpt-5.6-terra": "gpt-5-6-terra",
    "gpt-5.6-sol": "gpt-5-6-sol",
    "grok-4.6": "grok-4-6",
    "deepseek-v4-flash": "deepseek-v-4-flash-20260731",
    "deepseek-v4-pro": "deepseek-v-4-pro-20260813",
    "gemini-3.7-flash": "gemini-3-7-flash",
    "glm-5.3-flash": "glm-5-3-flash",
    "glm-5.3": "glm-5-3",
}

# 每个模型存档的字段（components 子分存档不展示，留作将来分析）
TRACK_FIELDS = ("rank", "previousRank", "rankChange", "score", "uncertainty",
                "confidence", "releasedAt", "components")

# 分数变动超过该绝对值才算真实变动（榜单分数带小数舍入噪音）
SCORE_NOISE = 0.5


def http_get_text(url, proxy=None, timeout=30):
    """抓 HTML 文本；先直连，失败且配置了代理则走代理重试（同价格监控策略）。"""
    hdrs = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
    }

    def do_fetch(use_proxy):
        handler = None
        if use_proxy and proxy:
            handler = urllib.request.ProxyHandler({"https": proxy, "http": proxy})
            opener = urllib.request.build_opener(handler)
        else:
            opener = urllib.request.build_opener()
        req = urllib.request.Request(url, headers=hdrs)
        with opener.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")

    try:
        return do_fetch(False)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        if proxy:
            log(f"直连失败({e})，尝试代理 {proxy} ...")
            return do_fetch(True)
        raise


def extract_flight_payload(html):
    """拼接 Next.js RSC flight payload（self.__next_f.push 的字符串分片）。"""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', html, re.S)
    if not chunks:
        raise ValueError("页面中找不到 __next_f payload（aihot 可能改版）")
    return "".join(json.loads(c) for c in chunks)


def extract_entries(blob):
    """从 payload 中抠出榜单条目。条目形如 {"rank":N,"previousRank":...,"slug":...}，
    用花括号配平截取完整 JSON 对象再 json.loads，解析失败的候选直接丢弃。"""
    entries = {}
    for m in re.finditer(r'\{"rank":\d+,"previousRank":', blob):
        start = m.start()
        depth, i, in_str, esc = 0, start, False, False
        while i < len(blob):
            ch = blob[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
            i += 1
        try:
            obj = json.loads(blob[start:i + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("slug"):
            entries[obj["slug"]] = obj
    return entries


def build_snapshot(entries, model_names):
    """按追踪清单生成当日快照；榜上无此模型记为 null（未收录）。"""
    models = {}
    for model_id, slug in MODEL_SLUG_MAP.items():
        raw = entries.get(slug)
        if raw is None:
            log(f"{model_names.get(model_id, model_id)}: 未在榜单中找到（slug={slug}）")
            models[model_id] = None
            continue
        models[model_id] = {k: raw.get(k) for k in TRACK_FIELDS}
    return {
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": AIHOT_URL,
        "models": models,
    }


def diff_snapshots(prev, cur):
    """对比两份快照（各含 models 字典），返回人类可读的变动列表。"""
    changes = []
    prev_m, cur_m = prev.get("models", {}), cur.get("models", {})
    for model_id, cur_e in cur_m.items():
        prev_e = prev_m.get(model_id)
        if prev_e is None and cur_e is None:
            continue
        if prev_e is None and cur_e is not None:
            changes.append(f"{model_id}: 新上榜 #{cur_e['rank']}")
            continue
        if prev_e is not None and cur_e is None:
            changes.append(f"{model_id}: 掉榜（原 #{prev_e['rank']}）")
            continue
        if cur_e.get("rank") != prev_e.get("rank"):
            changes.append(f"{model_id}: 排名 #{prev_e.get('rank')} → #{cur_e.get('rank')}")
        ps, cs = prev_e.get("score"), cur_e.get("score")
        if ps is not None and cs is not None and abs(cs - ps) > SCORE_NOISE:
            changes.append(f"{model_id}: 分数 {ps} → {cs}（{cs - ps:+.1f}）")
    return changes


def notify(title, msg):
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{msg}" with title "{title}" sound name "Glass"'
        ], check=False, timeout=10)
    except Exception as e:
        log(f"通知发送失败: {e}")


def main():
    cfg = load_config()
    with open(DATA_FILE) as f:
        data = json.load(f)
    model_names = {m["id"]: m["name"] for m in data.get("models", [])}

    log("抓取 aihot 榜单页 ...")
    html = http_get_text(AIHOT_URL, proxy=cfg.get("proxy"))
    blob = extract_flight_payload(html)
    entries = extract_entries(blob)
    log(f"榜单共解析出 {len(entries)} 个模型条目")
    if not entries:
        print("❌ 未解析到任何榜单条目（疑似改版），不写入快照，请人工检查！")
        sys.exit(1)

    today = datetime.now().strftime("%Y-%m-%d")
    snapshot = build_snapshot(entries, model_names)

    # 读取历史快照文件
    store = {}
    if LEADERBOARD_FILE.exists():
        try:
            with open(LEADERBOARD_FILE) as f:
                store = json.load(f)
        except Exception as e:
            print(f"❌ leaderboard_data.json 读取失败（{e}），中止写入，请人工检查！")
            sys.exit(1)

    # 与最近一个"非今天"的快照对比，得出变动
    prev_dates = sorted(d for d in store if d != today)
    prev = store[prev_dates[-1]] if prev_dates else None
    changes = diff_snapshots(prev, snapshot) if prev else []

    store[today] = snapshot  # 同日重跑幂等覆盖
    atomic_write_json(LEADERBOARD_FILE, store)

    if prev is None:
        print(f"📊 榜单基线已建立（{len(MODEL_SLUG_MAP)} 个追踪模型）→ {LEADERBOARD_FILE.name}")
        return
    if changes:
        print(f"📊 榜单变动 {len(changes)} 处：")
        for c in changes:
            print(f"  - {c}")
        top = "；".join(changes[:3]) + ("…" if len(changes) > 3 else "")
        notify(f"AI 榜单变动：{len(changes)} 处", top)
    else:
        log("榜单无变动，静默。")
        print("📊 榜单无变动。")


if __name__ == "__main__":
    main()
