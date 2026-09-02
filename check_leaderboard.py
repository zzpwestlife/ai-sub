#!/usr/bin/env python3
"""AI 模型榜单追踪脚本

追踪多个榜单来源的模型排名/得分变动，每日抓取：

【aihot.virxact.com 综合榜】
  - 数据源：https://aihot.virxact.com/leaderboard（Next.js SSR payload，纯 HTTP 可抓）
  - 追踪 data.json 中已有模型的排名/分数变化

【aihubmix.com 排行榜】
  - 数据源：https://aihubmix.com/api/router/leaderboard（公开 JSON API）
  - 追踪 Overall（text.overall）和 Coding（text.coding）两个榜单
  - 检测新模型上架、排名变动、分数变动

存储：leaderboard_data.json 按日期存全量快照（全保留，供趋势/日志派生）
同日重复运行幂等覆盖当天快照；抓取/解析失败绝不覆写旧数据
有变动（排名/分数>±0.5/上下榜/新模型）弹 macOS 通知，无变动静默

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

# 与价格监控共用工具函数（日志/配置/原子写入/HTTP JSON 请求）
from check_prices import VERBOSE, log, load_config, atomic_write_json, http_get_json

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"
LEADERBOARD_FILE = BASE_DIR / "leaderboard_data.json"

# ---------------------------------------------------------------------------
# aihot 相关配置
# ---------------------------------------------------------------------------

AIHOT_URL = "https://aihot.virxact.com/leaderboard"

# data.json 模型 ID → aihot 榜单 slug
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

# ---------------------------------------------------------------------------
# aihubmix 相关配置
# ---------------------------------------------------------------------------

AIHUBMIX_LEADERBOARD_URL = "https://aihubmix.com/api/router/leaderboard"

# 关注的榜单维度（dim key → 展示名称）
AIHUBMIX_DIMS = {
    "text.overall": "Overall",
    "text.coding": "Coding",
}

# ---------------------------------------------------------------------------
# 共用常量
# ---------------------------------------------------------------------------

# 分数变动超过该绝对值才算真实变动（榜单分数带小数舍入噪音）
SCORE_NOISE = 0.5


# ===========================================================================
# aihot 抓取与解析
# ===========================================================================

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


def build_aihot_snapshot(entries, model_names):
    """按追踪清单生成当日 aihot 快照；榜上无此模型记为 null（未收录）。"""
    models = {}
    for model_id, slug in MODEL_SLUG_MAP.items():
        raw = entries.get(slug)
        if raw is None:
            log(f"{model_names.get(model_id, model_id)}: 未在 aihot 榜单中找到（slug={slug}）")
            models[model_id] = None
            continue
        models[model_id] = {k: raw.get(k) for k in TRACK_FIELDS}
    return {
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": AIHOT_URL,
        "models": models,
    }


def diff_aihot_snapshots(prev, cur):
    """对比两份 aihot 快照（各含 models 字典），返回人类可读的变动列表。"""
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


# ===========================================================================
# aihubmix 抓取与解析
# ===========================================================================

def fetch_aihubmix_leaderboard(proxy=None):
    """从 aihubmix 公开 API 拉取排行榜数据，返回 dims 字典。"""
    data = http_get_json(AIHUBMIX_LEADERBOARD_URL, proxy=proxy)
    dims = data.get("dims", {})
    if not dims:
        raise ValueError("aihubmix API 返回空 dims（接口可能变动）")
    return dims


def build_aihubmix_snapshot(dims):
    """构建 aihubmix 当日快照，包含各维度的完整模型列表（带排名、分数、价格、延迟）。"""
    snapshot = {
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": AIHUBMIX_LEADERBOARD_URL,
    }
    for dim_key, dim_name in AIHUBMIX_DIMS.items():
        raw_models = dims.get(dim_key, [])
        models = []
        for i, m in enumerate(raw_models, 1):
            models.append({
                "rank": i,
                "model": m["m"],
                "vendor": m["v"],
                "score": m["s"],
                "priceIn": m["pin"],
                "priceOut": m["pout"],
                "ttftMs": m["lat"],
            })
        snapshot[dim_key] = models
        log(f"  aihubmix {dim_name}榜：{len(models)} 个模型")
    return snapshot


def diff_aihubmix_dimension(prev_models, cur_models, dim_name):
    """对比单个维度的两份榜单，返回变动列表。"""
    changes = []
    prev_map = {m["model"]: m for m in prev_models}
    cur_map = {m["model"]: m for m in cur_models}

    # 新上架模型
    new_models = [m for m in cur_models if m["model"] not in prev_map]
    for m in new_models:
        changes.append(f"[{dim_name}] 新模型上架：{m['model']}（#{m['rank']}，分数 {m['score']}）")

    # 下架模型
    removed_models = [m for m in prev_models if m["model"] not in cur_map]
    for m in removed_models:
        changes.append(f"[{dim_name}] 模型下架：{m['model']}（原 #{m['rank']}）")

    # 排名/分数变动
    for model_id, cur_m in cur_map.items():
        prev_m = prev_map.get(model_id)
        if not prev_m:
            continue
        # 排名变动
        if cur_m["rank"] != prev_m["rank"]:
            delta = prev_m["rank"] - cur_m["rank"]  # 正数 = 上升
            arrow = "↑" if delta > 0 else "↓"
            changes.append(
                f"[{dim_name}] {model_id}: 排名 #{prev_m['rank']} → #{cur_m['rank']} "
                f"（{arrow}{abs(delta)}）"
            )
        # 分数变动
        ps, cs = prev_m["score"], cur_m["score"]
        if abs(cs - ps) > SCORE_NOISE:
            changes.append(
                f"[{dim_name}] {model_id}: 分数 {ps} → {cs}（{cs - ps:+.1f}）"
            )

    return changes


def diff_aihubmix_snapshots(prev, cur):
    """对比两份 aihubmix 快照，返回所有维度的变动列表。"""
    changes = []
    for dim_key, dim_name in AIHUBMIX_DIMS.items():
        prev_models = prev.get(dim_key, [])
        cur_models = cur.get(dim_key, [])
        changes.extend(diff_aihubmix_dimension(prev_models, cur_models, dim_name))
    return changes


# ===========================================================================
# 通知 & 工具
# ===========================================================================

def notify(title, msg):
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{msg}" with title "{title}" sound name "Glass"'
        ], check=False, timeout=10)
    except Exception as e:
        log(f"通知发送失败: {e}")


def load_store():
    """读取 leaderboard_data.json，自动迁移旧格式（aihot 日期直挂顶层）。"""
    if not LEADERBOARD_FILE.exists():
        return {"aihot": {}, "aihubmix": {}}

    try:
        with open(LEADERBOARD_FILE) as f:
            store = json.load(f)
    except Exception as e:
        print(f"❌ leaderboard_data.json 读取失败（{e}），中止写入，请人工检查！")
        sys.exit(1)

    # 旧格式：顶层全是日期键 → 迁移到 aihot 命名空间
    date_keys = [k for k in store if re.match(r"^\d{4}-\d{2}-\d{2}$", k)]
    if date_keys and "aihot" not in store:
        log(f"检测到旧格式数据（{len(date_keys)} 天 aihot 快照），正在迁移...")
        aihot_data = {k: store.pop(k) for k in date_keys}
        store["aihot"] = aihot_data
        store["aihubmix"] = {}

    # 确保结构完整
    store.setdefault("aihot", {})
    store.setdefault("aihubmix", {})
    return store


def get_prev_snapshot(store_section, today):
    """从某个 section（aihot 或 aihubmix）中获取最近一份非今天的快照。"""
    prev_dates = sorted(d for d in store_section if d != today)
    return store_section[prev_dates[-1]] if prev_dates else None


# ===========================================================================
# 主流程
# ===========================================================================

def check_aihot(cfg, model_names, store, today):
    """抓取并对比 aihot 榜单，返回 (changes_list, had_error)。"""
    log("【aihot】抓取榜单页 ...")
    try:
        html = http_get_text(AIHOT_URL, proxy=cfg.get("proxy"))
        blob = extract_flight_payload(html)
        entries = extract_entries(blob)
    except Exception as e:
        print(f"❌ aihot 抓取失败：{e}")
        return [], True

    log(f"【aihot】榜单共解析出 {len(entries)} 个模型条目")
    if not entries:
        print("❌ aihot 未解析到任何榜单条目（疑似改版），不写入快照！")
        return [], True

    snapshot = build_aihot_snapshot(entries, model_names)
    prev = get_prev_snapshot(store["aihot"], today)
    changes = diff_aihot_snapshots(prev, snapshot) if prev else []

    store["aihot"][today] = snapshot  # 同日重跑幂等覆盖
    return changes, False


def check_aihubmix(cfg, store, today):
    """抓取并对比 aihubmix 榜单，返回 (changes_list, had_error)。"""
    log("【aihubmix】抓取排行榜 API ...")
    try:
        dims = fetch_aihubmix_leaderboard(proxy=cfg.get("proxy"))
    except Exception as e:
        print(f"❌ aihubmix 抓取失败：{e}")
        return [], True

    snapshot = build_aihubmix_snapshot(dims)

    # 空响应保护：维度数据为空不覆盖
    for dim_key in AIHUBMIX_DIMS:
        if not snapshot.get(dim_key):
            print(f"❌ aihubmix {dim_key} 维度返回空列表，不写入快照！")
            return [], True

    prev = get_prev_snapshot(store["aihubmix"], today)
    changes = diff_aihubmix_snapshots(prev, snapshot) if prev else []

    store["aihubmix"][today] = snapshot  # 同日重跑幂等覆盖
    return changes, False


def main():
    cfg = load_config()
    with open(DATA_FILE) as f:
        data = json.load(f)
    model_names = {m["id"]: m["name"] for m in data.get("models", [])}

    today = datetime.now().strftime("%Y-%m-%d")
    store = load_store()

    all_changes = []
    had_any_error = False

    # ---- aihot ----
    aihot_changes, aihot_err = check_aihot(cfg, model_names, store, today)
    all_changes.extend(aihot_changes)
    had_any_error = had_any_error or aihot_err
    if aihot_changes:
        log(f"【aihot】发现 {len(aihot_changes)} 处变动")

    # ---- aihubmix ----
    aihubmix_changes, aihubmix_err = check_aihubmix(cfg, store, today)
    all_changes.extend(aihubmix_changes)
    had_any_error = had_any_error or aihubmix_err
    if aihubmix_changes:
        log(f"【aihubmix】发现 {len(aihubmix_changes)} 处变动")

    # 写入快照（只有无错误时才写；有错误的那个 source 已经没被塞进去）
    atomic_write_json(LEADERBOARD_FILE, store)

    # ---- 输出结果 ----
    total_changes = len(all_changes)

    # 判断是否首次运行（各维度基线检查）
    aihot_first = len(store["aihot"]) == 1 and today in store["aihot"]
    aihubmix_first = len(store["aihubmix"]) == 1 and today in store["aihubmix"]

    if aihot_first:
        n_models = len(MODEL_SLUG_MAP)
        print(f"📊 aihot 基线已建立（{n_models} 个追踪模型）→ {LEADERBOARD_FILE.name}")

    if aihubmix_first:
        n_dims = len(AIHUBMIX_DIMS)
        total_models = sum(len(store["aihubmix"][today].get(d, [])) for d in AIHUBMIX_DIMS)
        print(f"📊 aihubmix 基线已建立（{n_dims} 个维度，共 {total_models} 条模型记录）→ {LEADERBOARD_FILE.name}")

    if total_changes > 0:
        print(f"📊 榜单变动共 {total_changes} 处：")
        for c in all_changes:
            print(f"  - {c}")
        top = "；".join(all_changes[:3]) + ("…" if len(all_changes) > 3 else "")
        notify(f"AI 榜单变动：{total_changes} 处", top)
    elif not (aihot_first or aihubmix_first):
        log("各榜单均无变动，静默。")
        print("📊 榜单无变动。")

    if had_any_error:
        print("⚠️ 部分数据源抓取失败，详见上方错误信息。")
        sys.exit(1)


if __name__ == "__main__":
    main()
