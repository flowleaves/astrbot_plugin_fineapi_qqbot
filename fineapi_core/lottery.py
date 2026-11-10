"""抽奖时间解析、奖品拆分与开奖选择。"""

from __future__ import annotations

import re
import secrets
import time
from typing import Any, Iterable

_DURATION_RE = re.compile(
    r"^\+?(\d+(?:\.\d+)?)\s*(s|sec|秒|m|min|分钟|分|h|hour|小时|d|day|天)$",
    re.IGNORECASE,
)
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_MD_RE = re.compile(r"^(\d{1,2})-(\d{1,2})[T\s](\d{1,2}):(\d{2})$")
_YMD_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[T\s](\d{1,2}):(\d{2})$")
_CN_DAY_RE = re.compile(r"^(今天|明天|后天)\s*(\d{1,2}):(\d{2})$")


class LotteryTimeError(ValueError):
    pass


def _at_local(year: int, month: int, day: int, hour: int, minute: int) -> int:
    try:
        return int(
            time.mktime(
                time.struct_time((year, month, day, hour, minute, 0, 0, 0, -1))
            )
        )
    except (OverflowError, ValueError) as exc:
        raise LotteryTimeError("开奖时间格式不正确") from exc


def parse_draw_time(
    text: str,
    *,
    now: int | None = None,
    default_minutes: int = 30,
    min_seconds: int = 60,
    max_days: int = 30,
) -> int:
    """解析开奖时间。

    支持：
    - 空字符串：now + default_minutes
    - 30m / 2h / 90s / 1d
    - 21:30（今天或明天）
    - 今天 21:30 / 明天 21:30
    - 11-10T21:30 / 11-10 21:30（今年或明年）
    - 2026-11-10T21:30 / 2026-11-10 21:30
    """
    now = int(now or time.time())
    raw = str(text or "").strip().replace("：", ":").replace("＋", "+")
    if not raw:
        draw_at = now + max(1, int(default_minutes)) * 60
    else:
        match = _DURATION_RE.match(raw)
        if match:
            amount = float(match.group(1))
            unit = match.group(2).lower()
            if unit in {"s", "sec", "秒"}:
                seconds = amount
            elif unit in {"m", "min", "分钟", "分"}:
                seconds = amount * 60
            elif unit in {"h", "hour", "小时"}:
                seconds = amount * 3600
            else:
                seconds = amount * 86400
            draw_at = now + int(seconds)
        else:
            match = _CN_DAY_RE.match(raw)
            if match:
                offset = {"今天": 0, "明天": 1, "后天": 2}[match.group(1)]
                base = time.localtime(now + offset * 86400)
                draw_at = _at_local(
                    base.tm_year, base.tm_mon, base.tm_mday,
                    int(match.group(2)), int(match.group(3)),
                )
            else:
                match = _HHMM_RE.match(raw)
                if match:
                    today = time.localtime(now)
                    draw_at = _at_local(
                        today.tm_year, today.tm_mon, today.tm_mday,
                        int(match.group(1)), int(match.group(2)),
                    )
                    if draw_at <= now + 30:
                        draw_at += 86400
                else:
                    match = _MD_RE.match(raw)
                    if match:
                        today = time.localtime(now)
                        draw_at = _at_local(
                            today.tm_year,
                            int(match.group(1)),
                            int(match.group(2)),
                            int(match.group(3)),
                            int(match.group(4)),
                        )
                        if draw_at <= now:
                            draw_at = _at_local(
                                today.tm_year + 1,
                                int(match.group(1)),
                                int(match.group(2)),
                                int(match.group(3)),
                                int(match.group(4)),
                            )
                    else:
                        match = _YMD_RE.match(raw)
                        if not match:
                            raise LotteryTimeError(
                                "无法识别开奖时间。示例：30m、2h、21:30、"
                                "11-10T21:30、2026-11-10T21:30"
                            )
                        draw_at = _at_local(
                            int(match.group(1)),
                            int(match.group(2)),
                            int(match.group(3)),
                            int(match.group(4)),
                            int(match.group(5)),
                        )

    if draw_at < now + max(1, int(min_seconds)):
        raise LotteryTimeError("开奖时间必须至少在 1 分钟之后")
    if draw_at > now + max(1, int(max_days)) * 86400:
        raise LotteryTimeError(f"开奖时间不能超过 {max_days} 天")
    return int(draw_at)


def split_prize(total_quota: int, count: int) -> list[int]:
    """把总 quota 尽量平均地拆成 count 份，余数给前面的 winner。"""
    total_quota = int(total_quota)
    count = int(count)
    if count <= 0:
        raise ValueError("中奖人数必须大于 0")
    if total_quota < count:
        raise ValueError("奖品总额太小，无法分给所有中奖者")
    base, remainder = divmod(total_quota, count)
    return [base + (1 if index < remainder else 0) for index in range(count)]


def pick_winners(entries: Iterable[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """安全随机抽取 count 个参与者；不足时全部中奖。"""
    pool = [entry for entry in entries if isinstance(entry, dict)]
    count = max(1, int(count))
    if len(pool) <= count:
        return pool
    return secrets.SystemRandom().sample(pool, count)


def format_draw_time(timestamp: int) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(timestamp)))
    except (ValueError, OSError):
        return "-"
