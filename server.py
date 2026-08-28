#!/usr/bin/env python3
"""Local AI HOT signal review app.

The server intentionally uses only the Python standard library so it can run in
this repository without adding a package manager or a frontend build step. It
can optionally read a Miner ``state/tree.json`` file for node suggestions, but
otherwise runs as a standalone service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


APP_DIR = Path(__file__).resolve().parent
VERSION = "0.2.1"


def discover_miner_root() -> Path | None:
    """Return the surrounding Miner checkout when running in-tree."""
    candidate = APP_DIR.parents[1]
    if (candidate / "state" / "tree.json").exists():
        return candidate
    return None


MINER_ROOT = discover_miner_root()
DEFAULT_STATE_DIR = MINER_ROOT / "state" / "signal_inbox" if MINER_ROOT else APP_DIR / "data"
STATE_DIR = Path(os.environ.get("AIHOT_DATA_DIR", DEFAULT_STATE_DIR)).expanduser().resolve()
RUNS_DIR = STATE_DIR / "aihot-runs"
DECISIONS_PATH = STATE_DIR / "aihot-decisions.jsonl"
VIEWS_PATH = STATE_DIR / "aihot-views.jsonl"
_default_tree_path = MINER_ROOT / "state" / "tree.json" if MINER_ROOT else None
_tree_path_env = os.environ.get("AIHOT_TREE_PATH")
TREE_PATH = Path(_tree_path_env).expanduser().resolve() if _tree_path_env else _default_tree_path
AIHOT_ITEMS_URL = os.environ.get("AIHOT_ITEMS_URL", "https://aihot.virxact.com/api/public/items")
DEFAULT_AI_HOT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 "
    "Safari/537.36 aihot-skill/0.2.0"
)
AIHOT_UA = os.environ.get("AIHOT_USER_AGENT", DEFAULT_AI_HOT_UA)
BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class Rule:
    weight: int
    tag: str
    label: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class InboxSnapshot:
    signature: tuple[tuple[str, int, int], ...]
    runs: list[dict[str, Any]]
    signals: list[dict[str, Any]]
    stats: dict[str, int]
    tag_counts: dict[str, int]
    total_pulled: int


INBOX_CACHE_LOCK = threading.RLock()
STATE_WRITE_LOCK = threading.RLock()
INBOX_CACHE: InboxSnapshot | None = None


POSITIVE_RULES = [
    Rule(
        5,
        "chips_compute",
        "芯片/算力/半导体",
        re.compile(
            r"韬定律|τ|麒麟|昇腾|鲲鹏|NVIDIA|英伟达|TSMC|台积电|三星|SK海力士|美光|"
            r"DRAM|HBM|ASIC|GPU|芯片|半导体|封装|Chiplet|2\.5D|3D|晶圆|测试工厂|"
            r"存储|光通信|CPO|硅光|互联|算力|服务器|集群|Vera|Grace|Blackwell|RTX|马赫",
            re.I,
        ),
    ),
    Rule(4, "robotics", "机器人/具身", re.compile(r"机器人|具身|humanoid|人形|宇树|Figure|Optimus|小鹏机器人|导购", re.I)),
    Rule(
        4,
        "business",
        "商业/融资/量产",
        re.compile(r"融资|领投|估值|收入|营收|利润|财报|量产|采购|合作|订单|客户|部署|上线|市场雷达|白金会员|基金会|会员", re.I),
    ),
    Rule(
        3,
        "agent_infra",
        "Agent/AI基础设施",
        re.compile(
            r"\bAgent\b|智能体|MCP|Claude Code|Codex|SWE-bench|编程模型|推理优化|上下文|"
            r"token|OpenRouter|PyTorch|云原生|智能体原生|沙箱|隔离|插件|训练框架",
            re.I,
        ),
    ),
    Rule(3, "policy", "政策/监管/认证", re.compile(r"立法|监管|政策|认证|安全可靠|出口管制|制裁|政府|央行|工信部|证监会", re.I)),
    Rule(2, "research", "论文/benchmark/开源", re.compile(r"论文|arxiv|benchmark|SWE-bench|评测|开源|训练框架|代码开源|研究", re.I)),
]

NEGATIVE_RULES = [
    (3, re.compile(r"提示词|Vibe Coding|Chrome插件|新标签页|观点|访谈|如何|教程|指南|技巧|播客|快捷键", re.I)),
    (2, re.compile(r"耳机|路由|护眼屏|手机|汽车OTA|登录|便利店|店员|音乐|Todo|天气|小艺|语音指令", re.I)),
]

NODE_HINTS = [
    (re.compile(r"韬定律|ASIC|芯片|XPU|昇腾|麒麟|GPU|Vera|Grace|Blackwell", re.I), "custom-ai-asic-and-xpu-platforms"),
    (re.compile(r"HBM|DRAM|美光|SK海力士|三星|存储", re.I), "hbm4-capacity-bandwidth-and-ai-memory-supply"),
    (re.compile(r"封装|Chiplet|2\.5D|3D|台积电|TSMC|CoWoS|SoIC", re.I), "foundry-packaging-and-substrates"),
    (re.compile(r"机器人|具身|humanoid|人形|Figure|Optimus|小鹏机器人|比亚迪", re.I), "humanoid-robot-platforms-and-production-ramp"),
    (re.compile(r"MCP|Claude Code|Codex|Agent|智能体|沙箱|隔离", re.I), "agent-governance-observability-and-evaluation"),
    (re.compile(r"OpenRouter|模型分发|inference provider|token", re.I), "model-hubs-inference-providers-and-agent-ecosystems"),
]

def ensure_dirs() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def configure_runtime(
    data_dir: str | Path,
    tree_path: str | Path | None = None,
    items_url: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Apply CLI/runtime paths while keeping module-level functions testable."""
    global STATE_DIR, RUNS_DIR, DECISIONS_PATH, VIEWS_PATH, TREE_PATH
    global AIHOT_ITEMS_URL, AIHOT_UA

    STATE_DIR = Path(data_dir).expanduser().resolve()
    RUNS_DIR = STATE_DIR / "aihot-runs"
    DECISIONS_PATH = STATE_DIR / "aihot-decisions.jsonl"
    VIEWS_PATH = STATE_DIR / "aihot-views.jsonl"
    TREE_PATH = Path(tree_path).expanduser().resolve() if tree_path else None
    if items_url:
        AIHOT_ITEMS_URL = items_url
    if user_agent:
        AIHOT_UA = user_agent
    invalidate_inbox_cache()
    ensure_dirs()


def invalidate_inbox_cache() -> None:
    global INBOX_CACHE
    with INBOX_CACHE_LOCK:
        INBOX_CACHE = None


def write_json_atomic(path: Path, payload: Any) -> None:
    """Replace JSON state atomically so readers never see a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with STATE_WRITE_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            fh.flush()


def now_bj() -> datetime:
    return datetime.now(timezone.utc).astimezone(BEIJING)


def parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def signal_id(item: dict[str, Any]) -> str:
    key = item.get("url") or item.get("id") or json.dumps(item, sort_keys=True, ensure_ascii=False)
    return "sig-aihot-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def item_text(item: dict[str, Any]) -> str:
    parts = [item.get("title"), item.get("summary"), item.get("title_en"), item.get("source"), item.get("category")]
    return " ".join(str(part) for part in parts if part)


def infer_node(text: str) -> str:
    # Node suggestions are a Miner integration, not a standalone requirement.
    # A fresh clone deliberately leaves this field empty.
    if TREE_PATH is None or not TREE_PATH.exists():
        return ""
    for pattern, node_id in NODE_HINTS:
        if pattern.search(text):
            return node_id
    return ""


def score_item(item: dict[str, Any]) -> dict[str, Any]:
    text = item_text(item)
    score = 0
    tags: list[str] = []
    reasons: list[str] = []
    for rule in POSITIVE_RULES:
        if rule.pattern.search(text):
            score += rule.weight
            tags.append(rule.tag)
            reasons.append(rule.label)
    for weight, pattern in NEGATIVE_RULES:
        if pattern.search(text):
            score -= weight
    category = item.get("category")
    if category in {"industry", "paper", "ai-models"}:
        score += 1
    if category == "tip":
        score -= 1
    if score >= 7:
        action = "keep"
    else:
        action = "review"
    published = parse_iso_utc(item.get("publishedAt"))
    time_bj = published.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M") if published else ""
    return {
        "id": signal_id(item),
        "score": score,
        "aihot_id": item.get("id"),
        "time_bj": time_bj,
        "published_at": item.get("publishedAt"),
        "category": category,
        "title": item.get("title") or "",
        "source": item.get("source") or "",
        "url": item.get("url") or "",
        "summary": item.get("summary") or "",
        "title_en": item.get("title_en") or "",
        "tags": sorted(set(tags)),
        "reasons": sorted(set(reasons)),
        "suggested_node": infer_node(text),
        "decision": action,
        "strength": "high" if score >= 10 else "medium",
        "user_note": "",
    }


def read_decisions() -> dict[str, dict[str, Any]]:
    decisions: dict[str, dict[str, Any]] = {}
    if not DECISIONS_PATH.exists():
        return decisions
    with DECISIONS_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            sig_id = record.get("id")
            if sig_id:
                decisions[sig_id] = record
    return decisions


def read_views() -> dict[str, dict[str, Any]]:
    views: dict[str, dict[str, Any]] = {}
    if not VIEWS_PATH.exists():
        return views
    with VIEWS_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            sig_id = record.get("id")
            if sig_id:
                views[sig_id] = record
    return views


def apply_decisions(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decisions = read_decisions()
    for signal in signals:
        decision = decisions.get(signal["id"])
        if not decision:
            signal["reviewed"] = False
            continue
        for key in ["decision", "strength", "suggested_node", "user_note"]:
            if key in decision:
                signal[key] = decision[key]
        signal["reviewed"] = True
        signal["reviewed_at"] = decision.get("reviewed_at")
    return signals


def apply_views(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    views = read_views()
    for signal in signals:
        record = views.get(signal["id"])
        if not record:
            signal["viewed"] = False
            continue
        signal["viewed"] = True
        signal["viewed_at"] = record.get("viewed_at")
    return signals


def apply_signal_state(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signals = apply_views(apply_decisions(signals))
    for signal in signals:
        if signal.get("reviewed") and not signal.get("viewed"):
            signal["viewed"] = True
            signal["viewed_at"] = signal.get("reviewed_at")
            signal["viewed_inferred"] = True
    return signals


def fetch_aihot(hours: int = 24, since_dt: datetime | None = None, mode: str = "hours") -> dict[str, Any]:
    ensure_dirs()
    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
        mode = "hours"
    else:
        since_dt = since_dt.astimezone(timezone.utc)
    since = since_dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    pages: list[dict[str, Any]] = []
    while True:
        params = {"mode": "all", "since": since, "take": "100"}
        if cursor:
            params["cursor"] = cursor
        url = AIHOT_ITEMS_URL + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": AIHOT_UA})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        batch = payload.get("items", [])
        items.extend(batch)
        pages.append({"count": len(batch), "has_next": bool(payload.get("hasNext"))})
        if not payload.get("hasNext") or not payload.get("nextCursor"):
            break
        cursor = payload["nextCursor"]
        time.sleep(0.2)

    signals = [score_item(item) for item in items]
    signals.sort(key=lambda sig: (sig["decision"] != "keep", -sig["score"], sig.get("published_at") or ""))
    signals = apply_signal_state(signals)
    run = {
        "run_id": now_bj().strftime("aihot-%Y%m%d-%H%M%S"),
        "generated_at_bj": now_bj().isoformat(timespec="seconds"),
        "since_bj": since_dt.astimezone(BEIJING).isoformat(timespec="seconds"),
        "hours": hours,
        "pull_mode": mode,
        "pages": pages,
        "total_pulled": len(items),
        "filtered_count": len(signals),
        "signal_count": len(signals),
        "category_counts": dict(Counter(str(item.get("category") or "null") for item in items)),
        "source_counts": dict(Counter(str(item.get("source") or "unknown") for item in items).most_common(20)),
        "signals": signals,
    }
    run_path = RUNS_DIR / f"{run['run_id']}.json"
    with STATE_WRITE_LOCK:
        write_json_atomic(run_path, run)
        write_json_atomic(STATE_DIR / "aihot-latest.json", run)
    invalidate_inbox_cache()
    return run


def latest_run() -> dict[str, Any] | None:
    latest = STATE_DIR / "aihot-latest.json"
    if latest.exists():
        run = load_run(latest)
        if run:
            return run
    runs = sorted(RUNS_DIR.glob("aihot-*.json"))
    if runs:
        return json.loads(runs[-1].read_text(encoding="utf-8"))
    return None


def load_run(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def file_signature(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (str(path), -1, -1)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def inbox_signature() -> tuple[tuple[str, int, int], ...]:
    ensure_dirs()
    signatures = [file_signature(path) for path in sorted(RUNS_DIR.glob("aihot-*.json"))]
    signatures.append(file_signature(DECISIONS_PATH))
    signatures.append(file_signature(VIEWS_PATH))
    return tuple(signatures)


def load_inbox_runs() -> list[dict[str, Any]]:
    ensure_dirs()
    runs: list[dict[str, Any]] = []
    for path in sorted(RUNS_DIR.glob("aihot-*.json")):
        run = load_run(path)
        if run:
            run["_path"] = str(path)
            runs.append(run)
    return runs


def merge_inbox_signals(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    first_seen: dict[str, str] = {}
    last_seen: dict[str, str] = {}
    for run in runs:
        run_id = str(run.get("run_id") or "")
        for signal in run.get("signals", []):
            sig_id = signal.get("id")
            if not sig_id:
                continue
            existing = by_id.get(sig_id, {})
            merged = {**existing, **dict(signal)}
            if sig_id not in first_seen:
                first_seen[sig_id] = run_id
            last_seen[sig_id] = run_id
            merged["first_seen_run_id"] = first_seen[sig_id]
            merged["last_seen_run_id"] = last_seen[sig_id]
            by_id[sig_id] = merged
    return apply_signal_state(list(by_id.values()))


def build_inbox_snapshot(signature: tuple[tuple[str, int, int], ...]) -> InboxSnapshot:
    runs = load_inbox_runs()
    signals = merge_inbox_signals(runs)
    return InboxSnapshot(
        signature=signature,
        runs=runs,
        signals=signals,
        stats=inbox_stats(signals),
        tag_counts=dict(Counter(tag for signal in signals for tag in signal.get("tags", []))),
        total_pulled=sum(int(run.get("total_pulled") or 0) for run in runs),
    )


def get_inbox_snapshot() -> InboxSnapshot:
    global INBOX_CACHE
    signature = inbox_signature()
    with INBOX_CACHE_LOCK:
        if INBOX_CACHE is None or INBOX_CACHE.signature != signature:
            INBOX_CACHE = build_inbox_snapshot(signature)
        return INBOX_CACHE


def replace_cached_signals(signals: list[dict[str, Any]], signature: tuple[tuple[str, int, int], ...]) -> None:
    global INBOX_CACHE
    if INBOX_CACHE is None:
        return
    INBOX_CACHE = InboxSnapshot(
        signature=signature,
        runs=INBOX_CACHE.runs,
        signals=signals,
        stats=inbox_stats(signals),
        tag_counts=dict(Counter(tag for signal in signals for tag in signal.get("tags", []))),
        total_pulled=INBOX_CACHE.total_pulled,
    )


def update_cached_decision(record: dict[str, Any]) -> None:
    global INBOX_CACHE
    with INBOX_CACHE_LOCK:
        if INBOX_CACHE is None:
            return
        signature = inbox_signature()
        if signature[:-2] != INBOX_CACHE.signature[:-2]:
            INBOX_CACHE = None
            return
        signals: list[dict[str, Any]] = []
        for signal in INBOX_CACHE.signals:
            if signal.get("id") != record.get("id"):
                signals.append(signal)
                continue
            updated = dict(signal)
            for key in ["decision", "strength", "suggested_node", "user_note"]:
                if key in record:
                    updated[key] = record[key]
            updated["reviewed"] = True
            updated["reviewed_at"] = record.get("reviewed_at")
            if not updated.get("viewed"):
                updated["viewed"] = True
                updated["viewed_at"] = record.get("reviewed_at")
                updated["viewed_inferred"] = True
            signals.append(updated)
        replace_cached_signals(signals, signature)


def update_cached_view(record: dict[str, Any]) -> None:
    global INBOX_CACHE
    with INBOX_CACHE_LOCK:
        if INBOX_CACHE is None:
            return
        signature = inbox_signature()
        if signature[:-2] != INBOX_CACHE.signature[:-2]:
            INBOX_CACHE = None
            return
        signals: list[dict[str, Any]] = []
        for signal in INBOX_CACHE.signals:
            if signal.get("id") != record.get("id"):
                signals.append(signal)
                continue
            updated = dict(signal)
            updated["viewed"] = True
            updated["viewed_at"] = record.get("viewed_at")
            signals.append(updated)
        replace_cached_signals(signals, signature)


def load_inbox_signals() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    snapshot = get_inbox_snapshot()
    return snapshot.signals, snapshot.runs


def effectively_viewed(signal: dict[str, Any]) -> bool:
    return bool(signal.get("viewed") or signal.get("reviewed"))


def signal_matches_decision(signal: dict[str, Any], decision: str) -> bool:
    if decision == "all":
        return True
    if decision == "viewed_keep":
        return effectively_viewed(signal) and bool(signal.get("reviewed")) and signal.get("decision") == "keep"
    if decision == "unviewed":
        return not effectively_viewed(signal)
    if decision == "unlabeled":
        return not bool(signal.get("reviewed"))
    return signal.get("decision") == decision


def signal_matches_query(signal: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(
        str(part)
        for part in [
            signal.get("title"),
            signal.get("summary"),
            signal.get("source"),
            signal.get("suggested_node"),
            *(signal.get("reasons") or []),
        ]
        if part
    ).lower()
    return query.lower() in haystack


def sort_signals(signals: list[dict[str, Any]], sort_mode: str) -> list[dict[str, Any]]:
    if sort_mode == "time":
        return sorted(signals, key=lambda sig: sig.get("published_at") or "", reverse=True)
    if sort_mode == "decision":
        return sorted(signals, key=lambda sig: (str(sig.get("decision") or ""), -int(sig.get("score") or 0)))
    return sorted(signals, key=lambda sig: (int(sig.get("score") or 0), sig.get("published_at") or ""), reverse=True)


def inbox_stats(signals: list[dict[str, Any]]) -> dict[str, int]:
    stats = {
        "all": len(signals),
        "viewed_keep": 0,
        "unviewed": 0,
        "unlabeled": 0,
        "keep": 0,
        "review": 0,
        "drop": 0,
    }
    for signal in signals:
        decision = signal.get("decision")
        if decision in {"keep", "review", "drop"}:
            stats[decision] += 1
        if not effectively_viewed(signal):
            stats["unviewed"] += 1
        if not signal.get("reviewed"):
            stats["unlabeled"] += 1
        if effectively_viewed(signal) and signal.get("reviewed") and decision == "keep":
            stats["viewed_keep"] += 1
    return stats


def parse_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def query_inbox(params: dict[str, list[str]] | None = None) -> dict[str, Any]:
    params = params or {}
    snapshot = get_inbox_snapshot()
    signals = snapshot.signals
    runs = snapshot.runs

    value = lambda key, default="": (params.get(key) or [default])[0]
    offset = parse_int(value("offset", "0"), 0, 0, 1_000_000)
    limit = parse_int(value("limit", "80"), 80, 1, 250)
    decision = value("decision", "all")
    tag = value("tag", "all")
    sort_mode = value("sort", "score")
    query = value("q", "").strip()

    filtered = [
        signal
        for signal in signals
        if signal_matches_decision(signal, decision)
        and (tag == "all" or tag in (signal.get("tags") or []))
        and signal_matches_query(signal, query)
    ]
    filtered = sort_signals(filtered, sort_mode)
    total_count = len(filtered)
    page = filtered[offset : offset + limit]
    next_offset = offset + len(page)
    latest = runs[-1] if runs else {}

    return {
        "scope": "inbox",
        "run_id": latest.get("run_id"),
        "generated_at_bj": latest.get("generated_at_bj"),
        "since_bj": latest.get("since_bj"),
        "pull_mode": latest.get("pull_mode"),
        "runs_count": len(runs),
        "total_pulled": snapshot.total_pulled,
        "signal_count": total_count,
        "total_count": total_count,
        "unique_count": len(signals),
        "loaded_count": len(page),
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset if next_offset < total_count else None,
        "has_more": next_offset < total_count,
        "stats": snapshot.stats,
        "tag_counts": snapshot.tag_counts,
        "signals": page,
    }


def latest_generated_at() -> datetime | None:
    run = latest_run()
    if not run:
        return None
    value = run.get("generated_at_bj")
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def load_tree_nodes() -> list[dict[str, Any]]:
    if TREE_PATH is None or not TREE_PATH.exists():
        return []
    data = json.loads(TREE_PATH.read_text(encoding="utf-8"))
    nodes = data.get("nodes", {})
    results = []
    for node_id, node in nodes.items():
        results.append(
            {
                "id": node_id,
                "title": node.get("title") or node_id,
                "status": node.get("status"),
                "depth": node.get("depth"),
            }
        )
    results.sort(key=lambda node: (node.get("depth") or 0, node["id"]))
    return results


def save_decision(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"id", "decision", "strength", "suggested_node", "user_note"}
    missing = sorted(required - payload.keys())
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    if payload["decision"] not in {"keep", "review", "drop"}:
        raise ValueError("decision must be keep, review, or drop")
    if payload["strength"] not in {"high", "medium", "low"}:
        raise ValueError("strength must be high, medium, or low")
    record = {
        "id": payload["id"],
        "decision": payload["decision"],
        "strength": payload["strength"],
        "suggested_node": payload["suggested_node"],
        "user_note": payload.get("user_note", ""),
        "reviewed_at": now_bj().isoformat(timespec="seconds"),
    }
    append_jsonl(DECISIONS_PATH, record)
    update_cached_decision(record)
    return record


def save_view(payload: dict[str, Any]) -> dict[str, Any]:
    sig_id = payload.get("id")
    if not sig_id:
        raise ValueError("missing id")
    record = {
        "id": sig_id,
        "viewed_at": now_bj().isoformat(timespec="seconds"),
    }
    append_jsonl(VIEWS_PATH, record)
    update_cached_view(record)
    return record


def json_response(handler: BaseHTTPRequestHandler, data: Any, status: int = 200) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def error_response(handler: BaseHTTPRequestHandler, message: str, status: int = 400) -> None:
    json_response(handler, {"error": message}, status)


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = f"AIHotReview/{VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/":
                return self.serve_static("index.html", "text/html; charset=utf-8")
            if parsed.path in {"/app.js", "/styles.css"}:
                content_type = "application/javascript; charset=utf-8" if parsed.path.endswith(".js") else "text/css; charset=utf-8"
                return self.serve_static(parsed.path.lstrip("/"), content_type)
            if parsed.path == "/api/health":
                snapshot = get_inbox_snapshot()
                return json_response(
                    self,
                    {
                        "status": "ok",
                        "version": VERSION,
                        "runs_count": len(snapshot.runs),
                        "signal_count": len(snapshot.signals),
                    },
                )
            if parsed.path == "/api/signals":
                return json_response(self, query_inbox(urllib.parse.parse_qs(parsed.query)))
            if parsed.path == "/api/nodes":
                return json_response(self, {"nodes": load_tree_nodes()})
        except Exception as exc:  # pragma: no cover - keeps UI errors readable
            return error_response(self, str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
        return error_response(self, "not found", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/pull":
                payload = self.read_json_body(default={})
                mode = payload.get("mode") or "hours"
                hours = int(payload.get("hours") or 24)
                if hours < 1 or hours > 168:
                    raise ValueError("hours must be between 1 and 168")
                if mode == "since_last":
                    since_dt = latest_generated_at()
                    if since_dt is None:
                        return json_response(self, fetch_aihot(hours=hours, mode="hours_fallback"))
                    return json_response(self, fetch_aihot(hours=hours, since_dt=since_dt, mode="since_last"))
                if mode != "hours":
                    raise ValueError("mode must be hours or since_last")
                return json_response(self, fetch_aihot(hours=hours, mode="hours"))
            if parsed.path == "/api/decision":
                return json_response(self, save_decision(self.read_json_body()))
            if parsed.path == "/api/view":
                return json_response(self, save_view(self.read_json_body()))
        except Exception as exc:  # pragma: no cover - keeps UI errors readable
            return error_response(self, str(exc))
        return error_response(self, "not found", HTTPStatus.NOT_FOUND)

    def read_json_body(self, default: Any | None = None) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            if default is not None:
                return default
            raise ValueError("empty request body")
        data = self.rfile.read(length).decode("utf-8")
        return json.loads(data)

    def serve_static(self, filename: str, content_type: str) -> None:
        path = APP_DIR / filename
        if not path.exists():
            return error_response(self, "not found", HTTPStatus.NOT_FOUND)
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local AI HOT review app")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--pull", action="store_true", help="pull the latest AI HOT data before serving")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument(
        "--data-dir",
        default=str(STATE_DIR),
        help="persistent state directory (env: AIHOT_DATA_DIR)",
    )
    parser.add_argument(
        "--tree-path",
        default=str(TREE_PATH) if TREE_PATH else "",
        help="optional Miner state/tree.json path (env: AIHOT_TREE_PATH)",
    )
    parser.add_argument(
        "--items-url",
        default=AIHOT_ITEMS_URL,
        help="AI HOT items endpoint (env: AIHOT_ITEMS_URL)",
    )
    args = parser.parse_args()
    if args.hours < 1 or args.hours > 168:
        parser.error("--hours must be between 1 and 168")
    configure_runtime(args.data_dir, args.tree_path, args.items_url, AIHOT_UA)
    if args.pull:
        run = fetch_aihot(args.hours)
        print(f"Pulled {run['total_pulled']} items, loaded {run['signal_count']} signals")
    snapshot = get_inbox_snapshot()
    print(f"Warmed inbox cache with {len(snapshot.signals)} signals from {len(snapshot.runs)} runs")
    address = (args.host, args.port)
    httpd = ThreadingHTTPServer(address, ReviewHandler)
    print(f"AI HOT review app: http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
