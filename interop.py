"""time_sense 时间感知桥接（**软依赖**）。

为什么要接它：备份包里最有价值的信息之一是「这份记忆是什么时候的」。
自己 ``strftime`` 只能写出 ``2026-10-20 04:30``，而时间感知能说
「2026-10-20 周二 04:30（深夜）」——跨自然日、时钟可信度、安静时段它都知道。

三条纪律：

1. **软依赖，绝不写进 manifest 的 dependencies.plugins**（缺依赖会被框架静默剔除）。
   这里全部 ``try/except``，没装 time_sense 也能跑，只是时间措辞朴素一点。
2. **延迟导入**：框架模块只在函数里 import，这样离线自检（不启动框架）也能用回落实现。
3. **不缓存失败**：插件加载顺序不定，第一次拿不到就下次再试，别把「暂时没有」记成「永远没有」。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

#: 时间感知服务的签名（与 time_sense 插件里的常量一致）。
TIME_SENSE_SIGNATURE = "time_sense:service:time_sense"

#: 成功取到的服务实例（失败不缓存，允许下次重试）。
_service_cache: Any = None

#: 上次取失败的时刻（失败后冷却一段时间，别每次调用都去 manager 里翻一遍）。
_service_failed_at: float = 0.0

#: 失败后的冷却秒数。
_FAIL_COOLDOWN = 60.0


def get_time_sense() -> Any | None:
    """取 time_sense 的 service 实例；没装/没启用时返回 ``None``。

    ``service_api.get_service()`` 找不到签名时会打日志。备份一次要问好几回时间，
    如果每回都去打一遍日志就太吵了 —— 所以失败后冷却 60 秒再试。
    """
    global _service_cache, _service_failed_at
    if _service_cache is not None:
        return _service_cache

    now = time.time()
    if _service_failed_at and (now - _service_failed_at) < _FAIL_COOLDOWN:
        return None

    try:
        from src.app.plugin_system.api.service_api import get_service

        _service_cache = get_service(TIME_SENSE_SIGNATURE)
    except Exception:  # noqa: BLE001 - 拿不到就是没有，不影响备份
        _service_cache = None
    if _service_cache is None:
        _service_failed_at = now
    return _service_cache


def reset_cache() -> None:
    """清掉缓存（插件重载后用）。"""
    global _service_cache, _service_failed_at
    _service_cache = None
    _service_failed_at = 0.0


def available() -> bool:
    """时间感知是否可用。"""
    return get_time_sense() is not None


def describe_moment(stamp: float | None = None) -> str:
    """把一个时刻说成可读文本。

    Args:
        stamp: epoch 秒；``None`` 表示现在。

    Returns:
        有 time_sense 时形如 ``2026-10-20 周二 04:30（深夜）``；
        否则退回 ``2026-10-20 04:30``。
    """
    moment = datetime.fromtimestamp(float(stamp)) if stamp else None
    service = get_time_sense()
    if service is not None:
        try:
            return str(service.render(moment, style="full", with_weekday=True))
        except Exception:  # noqa: BLE001 - 时间服务抖一下就回落
            pass
    target = moment or datetime.now()
    return target.strftime("%Y-%m-%d %H:%M")


def period_of(stamp: float | None = None) -> str:
    """取时段名（深夜/清晨/上午/中午/下午/傍晚/夜里）；拿不到返回空串。"""
    service = get_time_sense()
    if service is None:
        return ""
    try:
        moment = datetime.fromtimestamp(float(stamp)) if stamp else None
        return str(service.period_at(moment) or "")
    except Exception:  # noqa: BLE001
        return ""


def humanize_ago(stamp: float | None, *, fallback: str = "") -> str:
    """把「多久以前」说成人话。

    Args:
        stamp: 过去的时刻（epoch 秒）。
        fallback: 时间不可用时的兜底文本。

    Returns:
        有 time_sense 时用它算（跨天更准），否则自己算。
    """
    try:
        target = float(stamp or 0.0)
    except (TypeError, ValueError):
        return fallback
    if target <= 0:
        return fallback or "从没备份过"

    service = get_time_sense()
    if service is not None:
        try:
            span = service.between(target, time.time())
            text = str(span.get("text") or "").strip()
            days = int(span.get("days") or 0)
            if text:
                return f"{text}前" if days <= 0 else f"{text}前"
        except Exception:  # noqa: BLE001
            pass

    delta = max(0.0, time.time() - target)
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


def days_since(stamp: float | None) -> int:
    """从某个时刻到现在跨了几个自然日。

    Args:
        stamp: 参考时刻（epoch 秒）。

    Returns:
        跨过的自然日数；时间不可用时按本地日期相减估算。
    """
    try:
        target = float(stamp or 0.0)
    except (TypeError, ValueError):
        return 0
    if target <= 0:
        return 0

    service = get_time_sense()
    if service is not None:
        try:
            return int(service.day_changed_since(target))
        except Exception:  # noqa: BLE001
            pass

    try:
        then = datetime.fromtimestamp(target).date()
        today = datetime.now().date()
        return max(0, (today - then).days)
    except (OSError, ValueError):
        return 0


def is_same_day(first: float | None, second: float | None) -> bool:
    """两个时刻是否同一天。"""
    try:
        left = float(first or 0.0)
        right = float(second or 0.0)
    except (TypeError, ValueError):
        return False
    if left <= 0 or right <= 0:
        return False

    service = get_time_sense()
    if service is not None:
        try:
            return bool(service.is_same_day(left, right))
        except Exception:  # noqa: BLE001
            pass
    try:
        return datetime.fromtimestamp(left).date() == datetime.fromtimestamp(right).date()
    except (OSError, ValueError):
        return False


def is_quiet_now() -> bool:
    """现在是不是安静时段（没人说话）。拿不到就是 ``False``。"""
    service = get_time_sense()
    if service is None:
        return False
    try:
        return bool(service.quiet_hours_active())
    except Exception:  # noqa: BLE001
        return False


def clock_ok() -> bool:
    """系统时钟是否可信。

    时间感知拿不到可靠时间源时会把 ``clock_reading`` 标成不可用；
    那种情况下备份包里要如实标注「时间可能不准」，而不是假装知道。
    """
    service = get_time_sense()
    if service is None:
        return True
    try:
        reading = service.clock_reading()
        return bool(getattr(reading, "available", True))
    except Exception:  # noqa: BLE001
        return True


def snapshot_context(stamp: float | None = None) -> dict[str, Any]:
    """给 manifest 用的时间上下文。

    Args:
        stamp: 备份时刻；``None`` 表示现在。

    Returns:
        ``{"described", "period", "clock_ok", "source"}``；
        ``source`` 是 ``time_sense`` 或 ``local``。
    """
    moment = float(stamp) if stamp else time.time()
    service = get_time_sense()
    described = describe_moment(moment)
    return {
        "timestamp": moment,
        "described": described,
        "period": period_of(moment),
        "clock_ok": clock_ok(),
        "source": "time_sense" if service is not None else "local",
        "quiet": is_quiet_now(),
    }
