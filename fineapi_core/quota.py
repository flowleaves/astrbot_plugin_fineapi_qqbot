"""额度换算与展示。

星芽的换算口径（与站点前端一致）：
- 1 USD = QuotaPerUnit（默认 500000）内部额度
- 1 USD = 100 芽点，即 1 芽点 = 5000 内部额度
"""

from __future__ import annotations

YADIAN_PER_USD = 100.0
DEFAULT_QUOTA_PER_UNIT = 500000


def _unit(quota_per_unit: int | float | None) -> float:
    value = float(quota_per_unit or 0)
    return value if value > 0 else float(DEFAULT_QUOTA_PER_UNIT)


def quota_to_usd(quota: int | float | None, quota_per_unit: int = DEFAULT_QUOTA_PER_UNIT) -> float:
    return float(quota or 0) / _unit(quota_per_unit)


def usd_to_quota(usd: float, quota_per_unit: int = DEFAULT_QUOTA_PER_UNIT) -> int:
    return int(round(float(usd) * _unit(quota_per_unit)))


def quota_to_yadian(quota: int | float | None, quota_per_unit: int = DEFAULT_QUOTA_PER_UNIT) -> float:
    return quota_to_usd(quota, quota_per_unit) * YADIAN_PER_USD


def yadian_to_quota(yadian: float, quota_per_unit: int = DEFAULT_QUOTA_PER_UNIT) -> int:
    """把芽点换算成内部 quota（1 芽点 = 5000 quota，默认口径）。"""
    return int(round(float(yadian) * _unit(quota_per_unit) / YADIAN_PER_USD))


def format_usd(value: float) -> str:
    return f"${value:,.4f}"


def format_yadian(value: float) -> str:
    return f"{value:,.2f} 芽点"


def format_quota(
    quota: int | float | None,
    quota_per_unit: int = DEFAULT_QUOTA_PER_UNIT,
    *,
    show_yadian: bool = True,
    show_cny: bool = False,
    exchange_rate: float = 7.2,
) -> str:
    """把内部 quota 格式化为 `$x.xxxx（xx.xx 芽点，≈¥xx.xx）`。"""
    usd = quota_to_usd(quota, quota_per_unit)
    parts = [format_usd(usd)]
    if show_yadian:
        parts.append(format_yadian(quota_to_yadian(quota, quota_per_unit)))
    if show_cny:
        parts.append(f"≈¥{usd * float(exchange_rate or 0):,.2f}")
    return "（" + "，".join(parts) + "）" if len(parts) > 1 else parts[0]
