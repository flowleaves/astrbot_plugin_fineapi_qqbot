"""近 N 分钟模型状态分析。

思路参考 new-api 自带模型性能面板的“窗口聚合”，但那类面板通常直接看成功率，
会把客户端 4xx 和单用户重试算进去。这里改为只看错误日志，并做两项修正：
排除 4xx、按用户去重。

数据源：
- /api/log/?type=5：错误日志（含 other.status_code / error_code）
- /api/perf-metrics/summary：近 1 小时成功率（仅作展示，不参与判定）

规则：
- 只统计时间窗口内的错误；
- 默认排除 400-499 客户端错误（"400 类错误"）；
- 同一用户在同一模型上的重复错误只算 1 个用户，避免单用户死循环把模型打成异常；
- 不同用户数 >= abnormal_users 才判定为异常，1 个用户为波动。
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

STATUS_LABELS = {
    "down": "异常",
    "warn": "波动",
    "ok": "正常",
    "nodata": "无数据",
}

_STATUS_RE = re.compile(r"(?:status code|status_code|HTTP)\D{0,8}(\d{3})", re.IGNORECASE)


@dataclass
class ModelAgg:
    model: str
    error_users: set[str] = field(default_factory=set)
    error_count: int = 0
    client_error_count: int = 0
    last_error_at: int = 0
    status_codes: Counter = field(default_factory=Counter)
    error_codes: Counter = field(default_factory=Counter)


@dataclass
class Analysis:
    window_minutes: int
    models: list[dict[str, Any]]
    ok_count: int
    nodata_count: int
    ignored_client_errors: int
    total_error_logs: int
    scanned_logs: int
    sampled: bool
    perf_global: float | None
    perf_available: bool

    def has_issues(self) -> bool:
        return any(item["level"] in {"down", "warn"} for item in self.models)


def parse_other(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("other")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except ValueError:
            return {}
    return {}


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def extract_status_code(item: dict[str, Any], other: dict[str, Any]) -> int:
    for key in ("status_code", "statusCode", "status"):
        if key in other:
            code = _to_int(other.get(key))
            if code:
                return code
    for key in ("status_code", "statusCode"):
        if key in item:
            code = _to_int(item.get(key))
            if code:
                return code
    content = str(item.get("content") or "")
    match = _STATUS_RE.search(content)
    if match:
        return int(match.group(1))
    return 0


def extract_user_key(item: dict[str, Any]) -> str:
    user_id = item.get("user_id")
    if user_id not in (None, "", 0, "0"):
        return f"u:{user_id}"
    username = str(item.get("username") or "").strip()
    if username:
        return f"n:{username}"
    ip = str(item.get("ip") or "").strip()
    if ip:
        return f"ip:{ip}"
    return "unknown"


def extract_error_code(item: dict[str, Any], other: dict[str, Any]) -> str:
    for key in ("error_code", "error_type"):
        value = other.get(key) or item.get(key)
        if value:
            return str(value)[:48]
    return ""


def _success_rate(perf_models: dict[str, dict[str, Any]], model: str) -> float | None:
    data = perf_models.get(model)
    if not data:
        return None
    raw = data.get("success_rate")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # 兼容 0-1 与 0-100 两种返回
    if value > 1:
        value /= 100.0
    return max(0.0, min(1.0, value))


def analyze(
    items: list[dict[str, Any]],
    *,
    perf_summary: dict[str, Any] | None = None,
    window_minutes: int = 30,
    exclude_client_errors: bool = True,
    abnormal_users: int = 2,
    top_n: int = 8,
    now: int | None = None,
    sampled: bool = False,
    total_error_logs: int | None = None,
) -> Analysis:
    now = int(now or time.time())
    start = now - max(1, int(window_minutes)) * 60
    aggregates: dict[str, ModelAgg] = {}
    ignored = 0
    scanned = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        created_at = _to_int(item.get("created_at"))
        if created_at and created_at < start:
            continue
        model = str(item.get("model_name") or "").strip()
        if not model or model == "-":
            continue
        scanned += 1
        other = parse_other(item)
        status = extract_status_code(item, other)
        is_client_error = 400 <= status < 500

        agg = aggregates.get(model)
        if agg is None:
            agg = ModelAgg(model=model)
            aggregates[model] = agg

        if is_client_error and exclude_client_errors:
            agg.client_error_count += 1
            ignored += 1
            continue

        agg.error_count += 1
        agg.error_users.add(extract_user_key(item))
        agg.last_error_at = max(agg.last_error_at, created_at)
        if status:
            agg.status_codes[status] += 1
        error_code = extract_error_code(item, other)
        if error_code:
            agg.error_codes[error_code] += 1

    perf_models: dict[str, dict[str, Any]] = {}
    perf_global: float | None = None
    perf_available = False
    if isinstance(perf_summary, dict):
        for model in perf_summary.get("models") or []:
            if isinstance(model, dict):
                name = str(model.get("model_name") or "").strip()
                if name:
                    perf_models[name] = model
        summary = perf_summary.get("summary")
        if isinstance(summary, dict):
            raw = summary.get("success_rate")
            if raw is not None:
                try:
                    perf_global = float(raw)
                    if perf_global > 1:
                        perf_global /= 100.0
                    perf_global = max(0.0, min(1.0, perf_global))
                    perf_available = True
                except (TypeError, ValueError):
                    perf_global = None

    results: list[dict[str, Any]] = []
    ok_count = 0
    nodata_count = 0

    for name, agg in aggregates.items():
        users = len(agg.error_users)
        if users >= max(1, int(abnormal_users)):
            level = "down"
        elif users >= 1:
            level = "warn"
        else:
            level = "ok"
        if level == "ok":
            ok_count += 1
        results.append(
            {
                "model": name,
                "level": level,
                "error_users": users,
                "error_count": agg.error_count,
                "client_error_count": agg.client_error_count,
                "last_error_at": agg.last_error_at,
                "status_codes": dict(agg.status_codes),
                "error_codes": dict(agg.error_codes),
                "success_rate": _success_rate(perf_models, name),
            }
        )
        perf_models.pop(name, None)

    # 没有错误日志、但近 1 小时有性能数据的模型，也显示为正常。
    for name, perf in perf_models.items():
        rate = _success_rate(perf_models, name)
        if rate is None:
            nodata_count += 1
        else:
            ok_count += 1
        results.append(
            {
                "model": name,
                "level": "ok",
                "error_users": 0,
                "error_count": 0,
                "client_error_count": 0,
                "last_error_at": 0,
                "status_codes": {},
                "error_codes": {},
                "success_rate": rate,
            }
        )

    if not perf_available and not perf_summary:
        # 没有性能数据时，不把“未出现错误”等同于有流量。
        pass

    rank = {"down": 0, "warn": 1, "ok": 2}
    results.sort(
        key=lambda row: (
            rank.get(row["level"], 3),
            -row["error_users"],
            -row["last_error_at"],
            row["model"],
        )
    )

    return Analysis(
        window_minutes=max(1, int(window_minutes)),
        models=results[: max(1, int(top_n))],
        ok_count=ok_count,
        nodata_count=nodata_count,
        ignored_client_errors=ignored,
        total_error_logs=int(
            total_error_logs if total_error_logs is not None else len(items)
        ),
        scanned_logs=scanned,
        sampled=bool(sampled),
        perf_global=perf_global,
        perf_available=perf_available,
    )


def _format_time(timestamp: int) -> str:
    if not timestamp:
        return "-"
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(timestamp))
    except (ValueError, OSError):
        return "-"


def _dominant_reason(row: dict[str, Any]) -> str:
    status_codes = row.get("status_codes") or {}
    if status_codes:
        code = max(status_codes.items(), key=lambda item: item[1])[0]
        return f"主要 {code}"
    error_codes = row.get("error_codes") or {}
    if error_codes:
        code = max(error_codes.items(), key=lambda item: item[1])[0]
        return str(code)[:32]
    return "未知错误"


def _rate_suffix(row: dict[str, Any]) -> str:
    rate = row.get("success_rate")
    if rate is None:
        return ""
    return f"，近 1 小时成功率 {rate * 100:.1f}%"


def render(analysis: Analysis, *, now: int | None = None, show_ok: bool = False) -> str:
    now = int(now or time.time())
    lines = [f"📡 星芽模型状态（近 {analysis.window_minutes} 分钟）"]
    lines.append(f"🕒 统计时间：{_format_time(now)}")

    down = [row for row in analysis.models if row["level"] == "down"]
    warn = [row for row in analysis.models if row["level"] == "warn"]
    ok_models = [row for row in analysis.models if row["level"] == "ok"]

    if down:
        lines.append("")
        lines.append("❌ 异常（多个用户报错）")
        for row in down:
            lines.append(
                f"• {row['model']} — {row['error_users']} 个用户 / "
                f"{row['error_count']} 次，最后 {_format_time(row['last_error_at'])}，"
                f"{_dominant_reason(row)}{_rate_suffix(row)}"
            )
    if warn:
        lines.append("")
        lines.append("⚠️ 波动（仅 1 个用户，可能是单用户重试）")
        for row in warn:
            lines.append(
                f"• {row['model']} — {row['error_users']} 个用户 / "
                f"{row['error_count']} 次，最后 {_format_time(row['last_error_at'])}，"
                f"{_dominant_reason(row)}{_rate_suffix(row)}"
            )

    if not down and not warn:
        lines.append("")
        lines.append("✅ 近窗口内未发现多用户异常。")

    if show_ok and ok_models:
        lines.append("")
        lines.append("✅ 正常模型")
        for row in ok_models:
            rate = row.get("success_rate")
            suffix = f"，成功率 {rate * 100:.1f}%" if rate is not None else ""
            lines.append(f"• {row['model']}{suffix}")

    lines.append("")
    if analysis.ok_count:
        lines.append(f"✅ 正常：{analysis.ok_count} 个模型")
    if analysis.nodata_count:
        lines.append(f"⏸ 无数据：{analysis.nodata_count} 个模型")
    if analysis.ignored_client_errors:
        lines.append(f"ℹ️ 已忽略 4xx 客户端错误：{analysis.ignored_client_errors} 条")
    if analysis.perf_available and analysis.perf_global is not None:
        lines.append(
            f"📈 近 1 小时整体成功率：{analysis.perf_global * 100:.1f}%（仅供参考）"
        )
    if analysis.total_error_logs == 0:
        lines.append("")
        lines.append("⚠️ 未读取到错误日志：请确认站点 ERROR_LOG_ENABLED=true。")
        lines.append("   否则“未发现异常”不代表模型真实健康。")
        low_rate = [
            row
            for row in analysis.models
            if row.get("success_rate") is not None
            and float(row["success_rate"]) < 0.9
        ]
        if low_rate:
            lines.append("近 1 小时低成功率（含 4xx，仅供参考）：")
            for row in low_rate[:5]:
                lines.append(f"• {row['model']} — {row['success_rate'] * 100:.1f}%")

    if analysis.sampled:
        lines.append(
            f"🧾 样本：已扫描 {analysis.scanned_logs} / "
            f"{analysis.total_error_logs} 条错误日志"
        )
    if not analysis.models:
        lines.append("ℹ️ 统计窗口内没有错误日志，也没有性能数据。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1 小时总体正确率（/<模型>状态）
# ---------------------------------------------------------------------------

_STATUS_QUERY_RE = re.compile(r"/?\s*([^\s/]+?)\s*状态")


def extract_status_query(message: str) -> str | None:
    """从消息里提取「<模型>状态」的模型查询词。

    AstrBot 会在唤醒检查阶段去掉 wake_prefix（默认 `/`），所以真实事件里的
    ``message_str`` 通常是 ``claude状态``；这里同时兼容仍带 `/` 的情况。

    Args:
        message: 事件的消息文本。

    Returns:
        查询词；不符合 ``<模型>状态`` 格式时返回 None。
    """
    match = _STATUS_QUERY_RE.fullmatch(str(message or "").strip())
    if not match:
        return None
    return str(match.group(1) or "").strip() or None


HOUR_LABELS = {
    "ok": "稳定",
    "warn": "波动",
    "bad": "异常",
    "na": "数据不足",
}


def _hour_status(rate: float | None, calls: int, min_calls: int) -> str:
    if rate is None or calls < max(1, int(min_calls)):
        return "na"
    if rate >= 0.95:
        return "ok"
    if rate >= 0.80:
        return "warn"
    return "bad"


def _bucket_index(timestamp: int, start: int, window_minutes: int, buckets: int = 30) -> int:
    span = max(1, int(window_minutes) * 60)
    bucket_seconds = max(1, span // buckets)
    index = int((int(timestamp) - int(start)) // bucket_seconds)
    return max(0, min(buckets - 1, index))


def analyze_hour(
    success_logs: list[dict[str, Any]],
    error_logs: list[dict[str, Any]],
    model_names: list[str],
    query: str,
    *,
    now: int | None = None,
    window_minutes: int = 60,
    min_calls: int = 5,
    top_n: int = 10,
    exclude_client_errors: bool = True,
) -> dict[str, Any]:
    """统计近 1 小时每个模型的调用数、正确率与 30 段状态。

    - 成功调用来自 type=2 消费日志；
    - 错误来自 type=5 错误日志；
    - 4xx 默认不计入调用数/错误数；
    - 同一用户在同一模型上的重复错误只算 1 次；
    - 调用数 = 成功 + 去重后的错误数。
    """
    now = int(now or time.time())
    window_minutes = max(1, int(window_minutes))
    start = now - window_minutes * 60
    buckets_count = 30
    records: dict[str, dict[str, Any]] = {}
    available = {str(name).strip() for name in model_names if str(name).strip()}

    def record(name: str) -> dict[str, Any]:
        if name not in records:
            records[name] = {
                "success": 0,
                "errors": {},
                "success_buckets": [0] * buckets_count,
                "error_buckets": [0] * buckets_count,
            }
        return records[name]

    for item in success_logs:
        if not isinstance(item, dict):
            continue
        timestamp = _to_int(item.get("created_at"))
        if timestamp and (timestamp < start or timestamp > now):
            continue
        name = str(item.get("model_name") or "").strip()
        if not name or name == "-":
            continue
        agg = record(name)
        agg["success"] += 1
        if timestamp:
            agg["success_buckets"][_bucket_index(timestamp, start, window_minutes)] += 1

    for item in error_logs:
        if not isinstance(item, dict):
            continue
        timestamp = _to_int(item.get("created_at"))
        if timestamp and (timestamp < start or timestamp > now):
            continue
        name = str(item.get("model_name") or "").strip()
        if not name or name == "-":
            continue
        other = parse_other(item)
        status = extract_status_code(item, other)
        if exclude_client_errors and 400 <= status < 500:
            continue
        user_key = extract_user_key(item)
        agg = record(name)
        if user_key in agg["errors"]:
            continue
        agg["errors"][user_key] = timestamp or now
        if timestamp:
            agg["error_buckets"][_bucket_index(timestamp, start, window_minutes)] += 1

    available.update(records.keys())
    query = str(query or "").strip()
    is_overview = query in {"", "模型", "全部", "所有"}

    if is_overview:
        matched = [name for name in records if (records[name]["success"] + len(records[name]["errors"])) > 0]
    else:
        lowered = query.lower()
        matched = [name for name in available if lowered in name.lower()]
        exact = [name for name in matched if name.lower() == lowered]
        if exact:
            matched = exact

    models: list[dict[str, Any]] = []
    for name in matched:
        agg = records.get(name)
        if agg is None:
            success = 0
            error_count = 0
            success_buckets = [0] * buckets_count
            error_buckets = [0] * buckets_count
        else:
            success = int(agg["success"])
            error_count = len(agg["errors"])
            success_buckets = agg["success_buckets"]
            error_buckets = agg["error_buckets"]
        calls = success + error_count
        rate = (success / calls) if calls > 0 else None
        status = _hour_status(rate, calls, min_calls)
        if status == "na":
            rate_display = None
            segments = ["na"] * buckets_count
        else:
            rate_display = rate
            segments = []
            for index in range(buckets_count):
                ok = success_buckets[index]
                bad = error_buckets[index]
                total = ok + bad
                if total <= 0:
                    segments.append("na")
                else:
                    segments.append(
                        _hour_status(ok / total, total, min_calls=1)
                    )
        models.append(
            {
                "name": name,
                "calls": calls,
                "success": success,
                "rate": rate_display,
                "status": status,
                "status_text": HOUR_LABELS[status],
                "segments": segments,
            }
        )

    models.sort(
        key=lambda row: (
            -int(row["calls"]),
            -(float(row["rate"]) if row["rate"] is not None else -1),
            str(row["name"]),
        )
    )
    shown = models[: max(1, int(top_n))]
    total_calls = sum(int(row["calls"]) for row in shown)
    weighted_success = sum(int(row["success"]) for row in shown)
    overall_rate = (weighted_success / total_calls) if total_calls > 0 else None

    return {
        "query": query or "模型",
        "window_minutes": window_minutes,
        "window_start": start,
        "window_end": now,
        "matched": len(models),
        "shown": len(shown),
        "total_calls": total_calls,
        "overall_rate": overall_rate,
        "models": shown,
    }


def _format_clock(timestamp: int) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(int(timestamp)))
    except (ValueError, OSError):
        return "-"


def build_status_text(analysis: dict[str, Any]) -> tuple[str, str]:
    """生成 (带 JSON 注释的 T2I 文本, 纯 Markdown 文本)。"""
    models = analysis.get("models") or []
    window_text = (
        f"{_format_clock(analysis.get('window_start', 0))} → "
        f"{_format_clock(analysis.get('window_end', 0))}"
    )
    window_minutes = int(analysis.get("window_minutes", 60))
    window_label = "近 1 小时" if window_minutes == 60 else f"近 {window_minutes} 分钟"
    subtitle = f"{window_label} · 调用正确率"
    if analysis.get("error_log_total") == 0 and analysis.get("total_calls", 0) > 0:
        subtitle += " · 错误日志为空，请确认 ERROR_LOG_ENABLED"
    meta = {
        "title": "模型状态",
        "subtitle": subtitle,
        "window_text": window_text,
        "query": analysis.get("query") or "模型",
        "matched": analysis.get("matched", len(models)),
        "shown": analysis.get("shown", len(models)),
        "total_calls": analysis.get("total_calls", 0),
        "overall_rate": analysis.get("overall_rate"),
        "generated_at": _format_clock(analysis.get("window_end", 0)),
        "models": models,
    }

    lines = [
        f"**{meta['query']} · {window_label}**",
        "",
        "| 模型 | 调用 | 正确率 | 状态 |",
        "|---|---:|---:|---|",
    ]
    for row in models:
        rate = row.get("rate")
        rate_text = f"{float(rate) * 100:.2f}%" if rate is not None else "—"
        lines.append(
            f"| {row['name']} | {row['calls']} | {rate_text} | {row['status_text']} |"
        )
    if not models:
        lines.append("| 未找到匹配模型 | 0 | — | 数据不足 |")

    markdown = "\n".join(lines)
    comment = "<!--XINGYA_MODEL_STATUS:" + json.dumps(
        meta, ensure_ascii=False, separators=(",", ":")
    ) + "-->"
    return comment + "\n" + markdown, markdown
