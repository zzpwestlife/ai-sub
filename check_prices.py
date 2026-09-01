#!/usr/bin/env python3
"""
AI 订阅价格监控脚本
==================
每天定时从各提供商拉取价格，与 data.json 中的记录对比，发现变动时：
  1. 写入报告文件（price_reports/）
  2. 弹出 macOS 系统通知
  3. 不会自动修改 data.json —— 由人工确认后手动更新

监控范围：
  - OpenRouter：公开 API，拿到精确美元价格
  - apifun：    公开 API 拿分组倍率，推算实际价格（价格 = 官方美元价 × 倍率）
  - V3 API：    公开 API 拿基础倍率（分组倍率不公开，用快照对比检测变动）
  - 非线智能：  公开 /models 接口，直接返回人民币价格（无需登录）
  - Cubence：   API 需认证，/v1/models 不含定价（暂无法自动监控）

用法：
  python3 check_prices.py            # 正常检查
  python3 check_prices.py --verbose  # 打印详细过程
  python3 check_prices.py --note "说明"  # 本次检测到的历史条目附加备注

详细说明见同目录《价格监控指南.md》
"""

import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"
CONFIG_FILE = BASE_DIR / "monitor_config.local.json"
SNAPSHOT_FILE = BASE_DIR / "price_state" / "v3_snapshot.json"
DATA_SNAPSHOT_FILE = BASE_DIR / "price_state" / "data_snapshot.json"
HISTORY_FILE = BASE_DIR / "price_history.json"
REPORT_DIR = BASE_DIR / "price_reports"

VERBOSE = "--verbose" in sys.argv
NOTE_ARG = sys.argv[sys.argv.index("--note") + 1] if "--note" in sys.argv else ""

# ---------------------------------------------------------------------------
# 配置与常量
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "proxy": "",            # 如 "http://127.0.0.1:7897"，留空则不使用
    "fx_rate": 7.0,         # 美元兑人民币汇率
    "change_threshold_pct": 0.5,  # 价格变动超过该百分比才算变动（防舍入噪音）
}

# data.json 模型 → OpenRouter 模型 ID 映射
OPENROUTER_MODEL_MAP = {
    "claude-sonnet-5": "anthropic/claude-sonnet-5",
    "claude-opus-5": "anthropic/claude-opus-5",
    "claude-fable-5": "anthropic/claude-fable-5",
    "gpt-5.6-luna": "openai/gpt-5.6-luna",
    "gpt-5.6-terra": "openai/gpt-5.6-terra",
    "gpt-5.6-sol": "openai/gpt-5.6-sol",
    "grok-4.6": "x-ai/grok-4.6",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash-0731",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro-0813",
    "gemini-3.7-flash": "google/gemini-3.7-flash",
    "glm-5.3-flash": "z-ai/glm-5.3-flash",
    "glm-5.3": "z-ai/glm-5.3",
}

# apifun 分组名 → 负责的模型（与用户当前使用的分组一致）
APIFUN_GROUP_MODELS = {
    "Claude Plus（精品）": ["claude-sonnet-5", "claude-opus-5", "claude-fable-5"],
    "Codex Pro（外接版）": ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"],
    "Grok 企业版": ["grok-4.6"],
    "DeepSeek（自部署精选）": ["deepseek-v4-flash", "deepseek-v4-pro"],
    "智谱 Zhipu（满血模型）": ["glm-5.3-flash", "glm-5.3"],
    "Gemini （特价测试）": ["gemini-3.7-flash"],
}

# 官方价本身就是人民币的模型（DeepSeek/GLM），apifun 倍率直接乘
# data.json officialPrices 中的人民币价（单一数据源，改价只需改 data.json）；
# 其余平台（Anthropic/OpenAI/xAI/Google）官方价是美元，需先除以汇率再乘倍率。
CNY_OFFICIAL_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro", "glm-5.3-flash", "glm-5.3"}

# 非线智能上追踪的模型名（模型 ID 与 data.json 一致，无需单独映射）
NONELINEAR_MODELS = [
    "claude-sonnet-5", "claude-opus-5", "claude-fable-5",
    "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
    "grok-4.6", "deepseek-v4-flash", "deepseek-v4-pro",
    "gemini-3.7-flash", "glm-5.3-flash", "glm-5.3",
]

# V3 上追踪的模型名（V3 用带日期后缀的版本号）
V3_MODELS = [
    "claude-sonnet-5", "claude-opus-5", "claude-fable-5",
    "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
    "grok-4.6", "deepseek-v4-flash-0731", "deepseek-v4-pro-0813",
    "gemini-3.7-flash",
]

# Cubence 上追踪的模型（页面 $ 即人民币；仅 6 个模型可用）
CUBENCE_MODELS = [
    "claude-sonnet-5", "claude-opus-5", "claude-fable-5",
    "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
    "grok-4.6",
    "deepseek-v4-flash", "deepseek-v4-pro",
    "glm-5.3",
]


def log(msg):
    if VERBOSE:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg.update(json.load(f))
        except Exception as e:
            log(f"配置文件读取失败，使用默认配置: {e}")
    return cfg


def http_get_json(url, proxy=None, timeout=30, headers=None):
    """请求 JSON；先直连，失败且配置了代理则走代理重试。"""
    hdrs = {"User-Agent": "Mozilla/5.0 (ai-sub price monitor)"}
    if headers:
        hdrs.update(headers)

    def do_fetch(use_proxy):
        handler = None
        if use_proxy and proxy:
            handler = urllib.request.ProxyHandler({"https": proxy, "http": proxy})
            opener = urllib.request.build_opener(handler)
        else:
            opener = urllib.request.build_opener()
        req = urllib.request.Request(url, headers=hdrs)
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        return do_fetch(False)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        # 仅网络层异常才走代理重试；404/解析错误等重试也必然失败，直接上抛
        if proxy:
            log(f"直连失败({e})，尝试代理 {proxy} ...")
            return do_fetch(True)
        raise


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------

# 核验链接：发现变动后方便人工到源头确认
def verify_url(provider, model_id=None, or_id=None):
    if provider == "OpenRouter" and or_id:
        return f"https://openrouter.ai/{or_id}"
    if provider == "apifun":
        return "https://apikey.fun/pricing"
    if provider == "V3 API":
        return "https://api.v3.cm/panel"
    if provider == "非线智能":
        return "https://nonelinear.com/static/models.html"
    if provider == "Cubence":
        return "https://cubence.com/dashboard/model-plaza"
    return ""


def check_openrouter(data, cfg):
    """对比 OpenRouter 实际价格与 data.json 记录。
    策略：拉取每个模型的全部 provider 价格，取出现次数最多的价格（众数）
    作为数据库基准（代表最可能被路由到的价格档位）。
    """
    diffs = []
    log("拉取 OpenRouter 价格（含全部 provider）...")
    raw = http_get_json("https://openrouter.ai/api/v1/models", proxy=cfg.get("proxy"))
    existing = {m["id"] for m in raw.get("data", [])}
    fx = cfg["fx_rate"]

    or_entries = {p["model"]: p for p in data["prices"] if p["provider"] == "openrouter"}

    for model_id, or_id in OPENROUTER_MODEL_MAP.items():
        url = verify_url("OpenRouter", model_id, or_id)
        if or_id not in existing:
            diffs.append({"provider": "OpenRouter", "model": model_id, "field": "-",
                          "local": "-", "remote": "模型在 OpenRouter 上不存在", "url": url})
            continue
        try:
            ep_raw = http_get_json(f"https://openrouter.ai/api/v1/models/{or_id}/endpoints",
                                   proxy=cfg.get("proxy"))
        except Exception as e:
            diffs.append({"provider": "OpenRouter", "model": model_id, "field": "-",
                          "local": "-", "remote": f"endpoints 接口失败: {e}", "url": url})
            continue

        endpoints = ep_raw.get("data", {}).get("endpoints", [])
        if not endpoints:
            continue

        # 收集各 provider 价格（CNY），缓存读只统计非零值；
        # 同时记录每家（input, output, cacheRead）用于算最低档参考
        samples = {"input": [], "output": [], "cacheRead": []}
        ep_prices = []
        for ep in endpoints:
            p = ep.get("pricing", {})
            pi = round(float(p.get("prompt", 0)) * 1e6 * fx, 3)
            po = round(float(p.get("completion", 0)) * 1e6 * fx, 3)
            cr = float(p.get("input_cache_read") or 0) * 1e6 * fx
            samples["input"].append(pi)
            samples["output"].append(po)
            if cr > 0:
                samples["cacheRead"].append(round(cr, 4))
            ep_prices.append((pi, po, round(cr, 4)))

        # 最低档参考：按单家 provider 用看板同公式算综合单价（命中率按 90%），
        # 取最便宜一家的原始价格——是真实可达档位，不是跨家拼的虚拟价，
        # 也不参与对比报警，仅供看板展示参考。
        best_ref = None
        for pi, po, pcr in ep_prices:
            eff = pcr * 0.9 + pi * 0.1 if pcr > 0 else pi
            comp = eff * 0.7 + po * 0.3
            if best_ref is None or comp < best_ref[0]:
                ref_entry = {"input": pi, "output": po}
                if pcr > 0:
                    ref_entry["cacheRead"] = pcr
                best_ref = (comp, ref_entry)
        if best_ref:
            data.setdefault("openrouterRefMin", {})[model_id] = best_ref[1]

        local = or_entries.get(model_id, {})
        for field, values in samples.items():
            if not values:
                continue  # 无有效样本（如全部不支持缓存）
            mv, count = mode_price(values)
            lv = local.get(field)
            if lv is None:
                if mv > 0:
                    diffs.append({"provider": "OpenRouter", "model": model_id, "field": field,
                                  "local": "(空)", "remote": f"¥{mv}（众数，{count}/{len(values)} 家）",
                                  "url": url})
                continue
            if is_changed(lv, mv, cfg):
                diffs.append({"provider": "OpenRouter", "model": model_id, "field": field,
                              "local": f"¥{lv}", "remote": f"¥{mv}（众数，{count}/{len(values)} 家）",
                              "url": url})
    return diffs


def mode_price(values):
    """取众数（出现次数最多的价格）。平票时取最低价，避免产生虚拟价格。"""
    counter = Counter(values)
    max_count = counter.most_common(1)[0][1]
    if max_count == 1 and len(values) <= 2:
        # 仅 1~2 个样本，直接取最小值（最便宜档）
        return min(values), max_count
    top = sorted(v for v, c in counter.items() if c == max_count)
    if len(top) > 1:
        # 平票：取最低价（最便宜档），避免产生实际不存在的虚拟价格；
        # 也与 OpenRouter 默认反平方加权路由一致（大部分流量走最便宜的 provider）
        best_val = top[0]
    else:
        best_val = top[0]
    return best_val, max_count


# ---------------------------------------------------------------------------
# apifun
# ---------------------------------------------------------------------------

def check_apifun(data, cfg):
    """拉取 apifun 分组倍率，推算价格并与 data.json 对比。
    定价规则：CNY 价格 = 官方美元价 × 分组倍率（倍率已隐含汇率折算）
    DeepSeek 例外：官方价本身是人民币，直接 × 倍率。
    """
    diffs = []
    log("拉取 apifun 分组倍率 ...")
    raw = http_get_json(
        "https://apikey.fun/api/v1/pricing/groups?timezone=Asia%2FShanghai",
        proxy=cfg.get("proxy"))
    groups = {g["name"]: g for g in raw.get("data", [])}
    fx = cfg["fx_rate"]

    apifun_entries = {p["model"]: p for p in data["prices"] if p["provider"] == "apifun"}

    for group_name, model_ids in APIFUN_GROUP_MODELS.items():
        g = groups.get(group_name)
        if not g:
            diffs.append({"provider": "apifun", "model": "-", "field": "-",
                          "local": "-", "remote": f"分组「{group_name}」不存在（可能改名）",
                          "url": verify_url("apifun")})
            continue
        rate = g.get("rate_multiplier")
        if rate is None:
            diffs.append({"provider": "apifun", "model": "-", "field": "-",
                          "local": "-", "remote": f"分组「{group_name}」缺少 rate_multiplier 字段",
                          "url": verify_url("apifun")})
            continue
        for model_id in model_ids:
            local = apifun_entries.get(model_id)
            if not local:
                continue
            off = data["officialPrices"].get(model_id, {})
            if model_id in CNY_OFFICIAL_MODELS:
                expected = {
                    "input": off.get("input", 0) * rate,
                    "output": off.get("output", 0) * rate,
                    "cacheRead": (off.get("cacheRead") or 0) * rate,
                }
            else:
                expected = {
                    "input": off.get("input", 0) / fx * rate,
                    "output": off.get("output", 0) / fx * rate,
                    "cacheRead": (off.get("cacheRead") or 0) / fx * rate,
                }
                if off.get("cacheWrite"):
                    expected["cacheWrite"] = off["cacheWrite"] / fx * rate
            for field, ev in expected.items():
                lv = local.get(field)
                if ev == 0 or ev < 0.001:
                    continue
                if lv is None:
                    continue  # data.json 没记录该字段（如缓存写），不算变动
                if is_changed(lv, ev, cfg):
                    diffs.append({"provider": "apifun", "model": model_id, "field": field,
                                  "local": f"¥{lv}",
                                  "remote": f"¥{ev:.4f}（倍率 {rate}）",
                                  "url": verify_url("apifun")})
    return diffs


# ---------------------------------------------------------------------------
# 非线智能（公开 /models 接口，直接返回人民币价格）
# ---------------------------------------------------------------------------

def check_nonelinear(data, cfg):
    """拉取非线智能模型广场数据，直接与 data.json 中的人民币价格对比。
    行结构：[模型, 机构, 开源, 缓存命中价, 输入价, 输出价, ...]；'/' 表示无该价格。
    峰谷口径：部分模型（DeepSeek 等）线上挂峰值价 = 官方空闲价 × 2，
    data.json 记的是空闲价，峰谷切换会导致恰好 2 倍/0.5 倍的差异，识别后跳过。"""
    diffs = []
    log("拉取非线智能模型价格 ...")
    raw = http_get_json("https://nonelinear.com/models?offset=0&limit=1000",
                        proxy=cfg.get("proxy"))
    rows = {r[0]: r for r in raw.get("data", [])}
    url = verify_url("非线智能")

    nl_entries = {p["model"]: p for p in data["prices"] if p["provider"] == "nonelinear"}

    for model_id in NONELINEAR_MODELS:
        local = nl_entries.get(model_id)
        if not local:
            continue
        row = rows.get(model_id)
        if row is None:
            diffs.append({"provider": "非线智能", "model": model_id, "field": "-",
                          "local": "-", "remote": "模型在非线智能上不存在", "url": url})
            continue
        # 缓存命中价/输入价/输出价；'/' 表示不支持或未公布，跳过对应字段
        remote = {"cacheRead": row[3], "input": row[4], "output": row[5]}
        for field, rv in remote.items():
            lv = local.get(field)
            if lv is None:
                continue
            if rv == "/":
                continue
            try:
                rv = float(rv)
            except (TypeError, ValueError):
                continue
            if is_changed(lv, rv, cfg):
                # 峰谷口径差：线上挂牌价恰为本地记录的 2 倍（挂峰值价）或 0.5 倍（挂空闲价），
                # 属于峰谷切换而非调价，跳过以免反复误报；真实调价不会恰好整数倍于峰谷比。
                if lv:
                    ratio = rv / lv
                    if abs(ratio - 2) < 0.02 or abs(ratio - 0.5) < 0.02:
                        log(f"非线智能 {model_id} {field}: 线上为{'峰值' if ratio > 1 else '空闲'}档"
                            f"（×{ratio:g}），与本地口径不同，跳过")
                        continue
                diffs.append({"provider": "非线智能", "model": model_id, "field": field,
                              "local": f"¥{lv}", "remote": f"¥{rv}", "url": url})
    return diffs


# ---------------------------------------------------------------------------
# Cubence（API 需认证，页面 $ 即人民币）
# ---------------------------------------------------------------------------

def check_cubence(data, cfg):
    """Cubence API 需认证，定价不在 API 中返回。
    页面 $ 即人民币（充值 30 RMB 显示 $30）。
    价格数据来自 Model Plaza 页面截图（人工录入 data.json）。
    当前 /v1/models 仅返回模型列表，不含定价，无法自动监控。
    保留此函数作为占位，若 Cubence 未来开放定价 API 可在此补充。"""
    diffs = []

    # 读取 API Key（保留，以备未来 API 扩展）
    secrets_file = BASE_DIR / "secrets.local.json"
    api_key = None
    if secrets_file.exists():
        try:
            with open(secrets_file) as f:
                secrets = json.load(f)
                api_key = secrets.get("cubence_api_key")
        except Exception:
            pass
    if not api_key:
        log("Cubence: 未配置 API Key，跳过")
        return diffs

    log("Cubence: /v1/models 不含定价数据，暂无法自动监控（需手动关注 Model Plaza）")
    return diffs


# ---------------------------------------------------------------------------
# V3 API（基础倍率，快照对比）
# ---------------------------------------------------------------------------

def check_v3(cfg):
    """拉取 V3 基础倍率，与上次快照对比。分组倍率不公开，无法算出最终价格，
    因此只检测倍率本身是否变动。"""
    diffs = []
    log("拉取 V3 API 基础倍率 ...")
    raw = http_get_json("https://api.v3.cm/api/pricing", proxy=cfg.get("proxy"))
    items = raw if isinstance(raw, list) else raw.get("data", [])
    remote = {}
    for it in items:
        name = it.get("model_name", "")
        if name in V3_MODELS:
            remote[name] = {
                "model_ratio": it.get("model_ratio"),
                "completion_ratio": it.get("completion_ratio"),
                "enable_groups": it.get("enable_groups", []),
            }

    snapshot = {}
    if SNAPSHOT_FILE.exists():
        with open(SNAPSHOT_FILE) as f:
            snapshot = json.load(f)

    if not snapshot:
        log("V3 无历史快照，本次作为基线保存。")
    else:
        for name, r in remote.items():
            s = snapshot.get(name)
            if s is None:
                diffs.append({"provider": "V3 API", "model": name, "field": "-",
                              "local": "(快照中无)", "remote": f"新增模型 倍率 {r['model_ratio']}/{r['completion_ratio']}",
                              "url": verify_url("V3 API")})
                continue
            for k in ("model_ratio", "completion_ratio"):
                if s.get(k) != r.get(k):
                    diffs.append({"provider": "V3 API", "model": name, "field": k,
                                  "local": f"{s.get(k)}", "remote": f"{r.get(k)}",
                                  "url": verify_url("V3 API")})
            if sorted(s.get("enable_groups", [])) != sorted(r.get("enable_groups", [])):
                diffs.append({"provider": "V3 API", "model": name, "field": "enable_groups",
                              "local": str(s.get("enable_groups")), "remote": str(r.get("enable_groups")),
                              "url": verify_url("V3 API")})

    # 空/异常响应不覆写快照，否则下次恢复时会全量误报"新增模型"
    if not remote:
        log("V3 返回为空，保留旧快照，不更新基线。")
        return diffs

    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SNAPSHOT_FILE, remote)
    return diffs


# ---------------------------------------------------------------------------
# 价格变更历史（自动检测 data.json 的改动）
# ---------------------------------------------------------------------------

PRICE_FIELDS = ("input", "output", "cacheWrite", "cacheRead")


def atomic_write_json(path, obj):
    """写临时文件 + os.replace 原子替换，避免写一半被杀导致文件损坏。"""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def price_entry_key(p):
    """快照键：provider|model|peak/off。DeepSeek 官方有高峰/空闲两条，需区分。"""
    tag = "peak" if p.get("isPeak") else "off"
    return f"{p['provider']}|{p['model']}|{tag}"


def record_local_changes(data):
    """对比 data.json 当前价格与上次快照，将人工改动记入 price_history.json。
    每次脚本运行时自动执行，无需人工操作。"""
    current = {}
    for p in data.get("prices", []):
        current[price_entry_key(p)] = {f: p.get(f) for f in PRICE_FIELDS}

    snapshot = {}
    if DATA_SNAPSHOT_FILE.exists():
        try:
            with open(DATA_SNAPSHOT_FILE) as f:
                snapshot = json.load(f)
        except Exception as e:
            print(f"⚠️ data 快照损坏（{e}），重建基线，本次不记录历史。")
            snapshot = {}

    # 旧格式快照（两段式键）升级：静默重建基线，避免全量误报新增/删除
    if snapshot and "|" in next(iter(snapshot)) and next(iter(snapshot)).count("|") == 1:
        log("检测到旧格式快照，静默升级基线。")
        snapshot = {}

    entries = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if snapshot:
        for key, cur in current.items():
            prov, model, _tag = key.split("|")
            old = snapshot.get(key)
            if old is None:
                entries.append({"time": now, "provider": prov, "model": model,
                                "field": "(新增条目)", "from": "-", "to": "-",
                                "note": NOTE_ARG or "新录入的价格条目"})
                continue
            for f in PRICE_FIELDS:
                ov, cv = old.get(f), cur.get(f)
                if ov == cv or (ov is None and cv is None):
                    continue
                entries.append({"time": now, "provider": prov, "model": model,
                                "field": f, "from": ov if ov is not None else "-",
                                "to": cv if cv is not None else "-", "note": NOTE_ARG})
        for key in snapshot:
            if key not in current:
                prov, model, _tag = key.split("|")
                entries.append({"time": now, "provider": prov, "model": model,
                                "field": "(删除条目)", "from": "-", "to": "-",
                                "note": NOTE_ARG or "价格条目被移除"})
    else:
        log("无 data.json 历史快照，本次建立基线（不记录历史）。")

    if entries:
        history = []
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE) as f:
                    history = json.load(f)
            except Exception as e:
                # 历史是审计资产：损坏时中止写入并报错，绝不静默清空
                print(f"❌ price_history.json 读取失败（{e}），中止历史写入，请人工检查！")
                return entries
        history.extend(entries)
        atomic_write_json(HISTORY_FILE, history)
        print(f"📝 已记录 {len(entries)} 条价格变更 → {HISTORY_FILE.name}")

    DATA_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(DATA_SNAPSHOT_FILE, current)

    # 检测到人工改价时自动刷新 data.json 的 updateDate（只改日期，不碰价格）
    if entries:
        today = datetime.now().strftime("%Y-%m-%d")
        if data.get("updateDate") != today:
            data["updateDate"] = today
            atomic_write_json(DATA_FILE, data)
            log(f"updateDate 已自动刷新为 {today}")
    return entries


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def is_changed(local, remote, cfg):
    if local is None or remote is None:
        return local != remote
    if local == 0 and remote == 0:
        return False
    # 绝对差 ≤ ¥0.005 视为舍入误差（提供商页面通常只显示两位小数）
    if abs(local - remote) <= 0.005:
        return False
    base = max(abs(local), abs(remote), 1e-9)
    return abs(local - remote) / base * 100 > cfg["change_threshold_pct"]


def write_report(all_diffs, errors):
    REPORT_DIR.mkdir(exist_ok=True)
    ts = datetime.now()
    path = REPORT_DIR / f"report-{ts.strftime('%Y-%m-%d_%H%M')}.md"
    lines = [f"# 价格变动检查报告 {ts.strftime('%Y-%m-%d %H:%M')}", ""]
    if not all_diffs and not errors:
        lines.append("✅ 所有监控的提供商价格均无变动。")
    if all_diffs:
        lines.append(f"⚠️ 发现 {len(all_diffs)} 处变动（请确认后手动更新 data.json）：")
        lines.append("")
        lines.append("| 提供商 | 模型 | 字段 | 当前记录 | 线上最新 | 核验链接 |")
        lines.append("|--------|------|------|----------|----------|----------|")
        for d in all_diffs:
            link = f"[去核验]({d['url']})" if d.get("url") else "—"
            lines.append(f"| {d['provider']} | {d['model']} | {d['field']} | {d['local']} | {d['remote']} | {link} |")
    if errors:
        lines.append("")
        lines.append("## 检查失败的部分")
        for e in errors:
            lines.append(f"- {e}")
    lines.append("")
    lines.append("> 更新方法：编辑 `data.json` 中对应条目的价格（updateDate 会在下次运行脚本时自动刷新），跑一次 `python3 check_prices.py` 记录历史，然后 `git add data.json price_history.json && git commit && git push`")
    with open(path, "w") as f:
        f.write("\n".join(lines))

    # 清理 90 天前的旧报告，避免无界堆积
    cutoff = datetime.now().timestamp() - 90 * 86400
    for old in REPORT_DIR.glob("report-*.md"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    return path


def notify(n_diffs, n_errors, report_path):
    if n_diffs == 0 and n_errors == 0:
        log("无变动，不弹通知。")
        return
    if n_diffs > 0:
        title = f"AI 价格变动：发现 {n_diffs} 处差异"
        msg = f"报告：{report_path.name}（price_reports/ 目录）"
    else:
        title = "AI 价格检查：部分提供商查询失败"
        msg = f"{n_errors} 个检查项出错，详见报告"
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
    old_ref_min = data.get("openrouterRefMin")

    # 先检测 data.json 本身的人工改动，记入历史（在任何在线检查之前）
    try:
        record_local_changes(data)
    except Exception as e:
        log(f"历史检测失败（不影响主流程）: {e}")

    all_diffs, errors = [], []

    for name, fn in [
        ("OpenRouter", lambda: check_openrouter(data, cfg)),
        ("apifun", lambda: check_apifun(data, cfg)),
        ("非线智能", lambda: check_nonelinear(data, cfg)),
        ("V3 API", lambda: check_v3(cfg)),
        ("Cubence", lambda: check_cubence(data, cfg)),
    ]:
        try:
            diffs = fn()
            all_diffs.extend(diffs)
            log(f"{name}: {len(diffs)} 处变动")
        except Exception as e:
            errors.append(f"{name} 检查失败: {e}")
            log(f"{name} 检查失败: {e}")

    # OpenRouter 最低档参考价有变化时回写 data.json（仅供看板参考展示）
    if data.get("openrouterRefMin") != old_ref_min:
        atomic_write_json(DATA_FILE, data)
        log("openrouterRefMin 已更新 → data.json")

    report_path = write_report(all_diffs, errors)
    print(f"检查完成：{len(all_diffs)} 处变动，{len(errors)} 个错误")
    print(f"报告：{report_path}")
    if all_diffs:
        for d in all_diffs:
            print(f"  [{d['provider']}] {d['model']} {d['field']}: {d['local']} → {d['remote']}")
    notify(len(all_diffs), len(errors), report_path)


if __name__ == "__main__":
    main()
