"""星芽（fineapi）QQ 助手 —— AstrBot 插件。

命令：
    /绑定 <用户名> <ID>
    /解绑
    /余额
    /领取首绑礼
    /抽奖
    /抽奖信息
    /模型状态
    /用户命令
    /管理命令（管理员）
    /芽点帮助
    /强制解绑 <平台用户ID>          （管理员）
    /绑定查询 <平台用户ID或星芽ID>  （管理员）
    /补发首绑礼 <平台用户ID> [force]（管理员）
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import sys
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    from astrbot.api.event import MessageChain
except ImportError:  # 兼容旧版本；主动消息会降级为仅日志
    MessageChain = None

# AstrBot 有的版本以包方式加载 main，有的版本把插件目录加入 sys.path 后加载 main。
# 这里同时兼容两种方式，并使用带前缀的包名避免与其他插件的 core 冲突。
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.append(_PLUGIN_DIR)

try:
    from .fineapi_core.client import NewAPIClient, NewAPIError
    from .fineapi_core.lottery import (
        LotteryTimeError,
        format_draw_time,
        parse_draw_time,
        pick_winners,
        split_prize,
    )
    from .fineapi_core.model_status import (
        analyze_hour,
        build_status_text,
        extract_status_query,
    )
    from .fineapi_core.quota import (
        format_quota,
        format_usd,
        format_yadian,
        quota_to_yadian,
        usd_to_quota,
        yadian_to_quota,
    )
    from .fineapi_core.store import Store
except ImportError:  # pragma: no cover
    from fineapi_core.client import NewAPIClient, NewAPIError
    from fineapi_core.lottery import (
        LotteryTimeError,
        format_draw_time,
        parse_draw_time,
        pick_winners,
        split_prize,
    )
    from fineapi_core.model_status import (
        analyze_hour,
        build_status_text,
        extract_status_query,
    )
    from fineapi_core.quota import (
        format_quota,
        format_usd,
        format_yadian,
        quota_to_yadian,
        usd_to_quota,
        yadian_to_quota,
    )
    from fineapi_core.store import Store

PLUGIN_NAME = "astrbot_plugin_fineapi_qqbot"

BIND_CMD_RE = re.compile(r"(?:绑定ID|绑定|bind)\s*(.*)$", re.IGNORECASE)

GIFT_STATE_LABELS = {
    "none": "未发放",
    "pending": "发放中",
    "granted": "已发放",
    "failed": "发放失败",
    "unknown": "状态待核对",
    "skipped": "不发放（老用户）",
}


class FineAPIQQBot(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = self._resolve_data_dir()
        self.store = Store(self.data_dir / "fineapi.db")
        self.client = NewAPIClient(
            str(self._cfg("site.base_url", "https://xingya.site")),
            str(self._cfg("site.access_token", "")),
            admin_user_id=int(self._cfg("site.admin_user_id", 0) or 0),
            timeout_seconds=int(self._cfg("site.timeout_seconds", 10) or 10),
        )
        self._ready = False
        self._ready_lock = asyncio.Lock()
        self._last_query: dict[str, float] = {}
        self._model_status_cache: dict[str, tuple[float, str, str, str | None]] = {}
        self._lottery_task: asyncio.Task | None = None
        self._lottery_lock = asyncio.Lock()
        self._stopping = False

    # ---------- 生命周期 ----------

    async def initialize(self):
        await self._ensure_ready()

    async def terminate(self):
        self._stopping = True
        task = self._lottery_task
        self._lottery_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.client.close()
        await self.store.close()

    async def _ensure_ready(self):
        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:
                return
            await self.store.connect()
            try:
                stale = await self.store.mark_stale_pending_unknown(600)
                if stale:
                    logger.warning(
                        "[fineapi] %s 条首绑礼记录停留在 pending，已标记为 unknown", stale
                    )
                stale_winners = await self.store.mark_stale_lottery_winners_unknown(600)
                if stale_winners:
                    logger.warning(
                        "[fineapi] %s 条中奖发放记录停留在 pending，已标记为 unknown",
                        stale_winners,
                    )
            except Exception as exc:  # 不阻塞插件启动
                logger.warning("[fineapi] 清理 pending 记录失败: %s", exc)
            self._ready = True
            if not self.client.configured():
                logger.warning(
                    "[fineapi] 尚未配置 site.base_url / site.access_token，命令会提示配置缺失"
                )
            else:
                logger.info("[fineapi] 星芽助手已就绪")
            if (
                not self._stopping
                and bool(self._cfg("lottery.enabled", True))
                and self._lottery_task is None
            ):
                self._lottery_task = asyncio.create_task(self._lottery_loop())

    # ---------- 配置与工具 ----------

    @staticmethod
    def _resolve_data_dir() -> Path:
        name = PLUGIN_NAME
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            base = Path(get_astrbot_data_path()) / "plugin_data" / name
        except Exception:
            try:
                from astrbot.api.star import StarTools

                base = Path(StarTools.get_data_dir(name))
            except Exception:
                base = Path("data") / "plugin_data" / name
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _cfg(self, path: str, default: Any = None) -> Any:
        node: Any = self.config
        for part in str(path).split("."):
            if node is None:
                return default
            try:
                node = node.get(part)
            except AttributeError:
                return default
        return default if node is None else node

    def _is_group(self, event: AstrMessageEvent) -> bool:
        return bool(event.get_group_id())

    def _allowed(self, event: AstrMessageEvent) -> bool:
        if self._is_group(event):
            if not bool(self._cfg("binding.allow_group", True)):
                return False
            whitelist = [
                str(item).strip()
                for item in (self._cfg("access.whitelist_groups", []) or [])
                if str(item).strip()
            ]
            return not whitelist or str(event.get_group_id()) in whitelist
        return bool(self._cfg("binding.allow_private", True))

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        qq = str(event.get_sender_id()).strip()
        admins = {
            str(item).strip()
            for item in (self._cfg("access.admin_qqs", []) or [])
            if str(item).strip()
        }
        return qq in admins

    def _cooldown_ok(self, qq: str) -> tuple[bool, int]:
        seconds = int(self._cfg("access.query_cooldown_seconds", 3) or 0)
        if seconds <= 0:
            return True, 0
        now = time.time()
        last = self._last_query.get(qq, 0.0)
        if now - last < seconds:
            return False, max(1, int(seconds - (now - last)) + 1)
        self._last_query[qq] = now
        if len(self._last_query) > 5000:
            cutoff = now - 3600
            self._last_query = {k: v for k, v in self._last_query.items() if v >= cutoff}
        return True, 0

    def _gift_new_user_id_after(self) -> int:
        try:
            value = int(self._cfg("gift.new_user_id_after", 8352) or 0)
        except (TypeError, ValueError):
            value = 8352
        return max(0, value)

    def _is_old_gift_user(self, user_id: int) -> bool:
        threshold = self._gift_new_user_id_after()
        return threshold > 0 and int(user_id) <= threshold

    def _quota_per_unit(self) -> int:
        try:
            value = int(self._cfg("site.quota_per_unit", 500000) or 500000)
        except (TypeError, ValueError):
            value = 500000
        return value if value > 0 else 500000

    def _fmt_quota(self, quota: int | float | None) -> str:
        return format_quota(
            quota,
            self._quota_per_unit(),
            show_yadian=bool(self._cfg("ui.show_yadian", True)),
            show_cny=bool(self._cfg("ui.show_cny", False)),
            exchange_rate=float(self._cfg("ui.exchange_rate", 7.2) or 7.2),
        )

    def _parse_bind_args(
        self, event: AstrMessageEvent, username_arg: str, user_id_arg: str
    ) -> tuple[str, str, bool]:
        """解析 /绑定 <用户名> <ID>。

        返回 (用户名, 用户ID, 是否使用了旧的“ID 在前”格式)。
        同时兼容用户名中间有空格，例如 /绑定 alice smith 123。
        """
        raw = str(getattr(event, "message_str", "") or "").strip()
        match = BIND_CMD_RE.search(raw)
        rest = str(match.group(1) if match else "").strip()
        tokens = rest.split()

        if len(tokens) >= 2:
            if tokens[-1].isdigit():
                return " ".join(tokens[:-1]).strip(), tokens[-1], False
            if tokens[0].isdigit():
                return " ".join(tokens[1:]).strip(), tokens[0], True

        name = str(username_arg or "").strip()
        uid = str(user_id_arg or "").strip()
        if uid.isdigit() and name:
            return name, uid, False
        if name.isdigit() and uid:
            return uid, name, True
        return name, uid, False

    def _user_help_text(self) -> str:
        return "\n".join(
            [
                "🌱 星芽助手 · 用户命令",
                "/绑定 <用户名> <ID>   绑定星芽账号（ID 必须是纯数字）",
                "  例：/绑定 alice 123",
                "/解绑                解除当前绑定",
                "/余额                查询余额 / 累计消耗 / 调用次数",
                "/领取首绑礼          领取首次绑定赠送",
                "/抽奖                参与当前会话的抽奖",
                "/抽奖信息            查看当前抽奖 / 最近开奖结果",
                "/<模型>状态           模糊查询模型，例：/claude状态",
                "/模型状态            全部模型近 1 小时 Top 10",
                "/我的ID              查看平台用户 ID / 群 ID",
                "/用户命令            只显示用户命令",
                "/芽点帮助            显示完整帮助",
            ]
        )

    def _admin_help_text(self) -> str:
        return "\n".join(
            [
                "🛠 星芽助手 · 管理命令",
                "/强制解绑 <平台用户ID>",
                "/绑定查询 <平台用户ID或星芽ID>",
                "/补发首绑礼 <平台用户ID> [force]",
                "/发抽奖 [时间] [芽点] [人数] [标题]",
                "  例：/发抽奖 30m 100 1 周末福利",
                "  例：/发抽奖 21:30 100 3 今晚福利",
                "/开奖 [编号]",
                "/取消抽奖 [编号]",
                "/抽奖管理",
                "/补发抽奖 [编号]",
                "/管理命令            只显示管理命令",
            ]
        )

    @staticmethod
    def _fmt_time(timestamp: Any) -> str:
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(timestamp)))
        except (TypeError, ValueError, OSError):
            return "-"

    # ---------- 用户命令 ----------

    @filter.command("绑定", alias={"绑定ID", "bind"})
    async def bind(self, event: AstrMessageEvent, username: str = "", user_id: str = ""):
        """绑定星芽账号：/绑定 <用户名> <ID>（ID 为纯数字）。"""
        if not self._allowed(event):
            return
        await self._ensure_ready()

        username, user_id, legacy_order = self._parse_bind_args(
            event, username, user_id
        )
        if not user_id.isdigit() or int(user_id) <= 0 or not username:
            yield event.plain_result(
                "用法：/绑定 <用户名> <ID>\n例如：/绑定 alice 123\n"
                "ID 必须是纯数字；用户名必须与站点账号完全一致（区分大小写）。"
            )
            return

        qq = str(event.get_sender_id()).strip()
        uid = int(user_id)

        try:
            existing = await self.store.get_binding(qq)
            if existing:
                yield event.plain_result(
                    f"你已经绑定了账号 {existing['username']}（ID {existing['newapi_user_id']}）。\n"
                    "如需更换，请先发送 /解绑。"
                )
                return

            other = await self.store.get_binding_by_user_id(uid)
            if other:
                yield event.plain_result("该星芽账号已被其他平台用户绑定，无法重复绑定。")
                return

            try:
                user = await self.client.get_user(uid)
            except NewAPIError as exc:
                yield event.plain_result(f"绑定失败：{exc.message}")
                return

            actual_username = str(user.get("username") or "")
            if actual_username != username:
                # 不区分“ID 不存在”和“用户名不匹配”，降低枚举价值
                yield event.plain_result("绑定失败：ID 与用户名不匹配，或账号不存在。")
                return

            try:
                role = int(user.get("role", 1) or 1)
            except (TypeError, ValueError):
                role = 1
            if role >= 10 and bool(self._cfg("binding.block_admin", True)):
                yield event.plain_result("该账号为管理员账号，已禁止绑定。")
                return

            try:
                status = int(user.get("status", 1))
            except (TypeError, ValueError):
                status = 1
            if status != 1:
                yield event.plain_result("该账号已被禁用或封禁，无法绑定。")
                return

            created, reason = await self.store.create_binding(qq, uid, actual_username)
            if not created:
                if reason == "user_bound":
                    yield event.plain_result("该星芽账号已被其他平台用户绑定，无法重复绑定。")
                elif reason == "qq_bound":
                    yield event.plain_result("你已经绑定过账号，请先发送 /解绑。")
                else:
                    yield event.plain_result("绑定失败：数据写入冲突，请稍后重试。")
                return

            gift_text = await self._try_gift(qq, uid)
            lines = [
                "✅ 绑定成功",
                f"👤 星芽账号：{actual_username}（ID {uid}）",
            ]
            if gift_text:
                lines.append(gift_text)
            if legacy_order:
                lines.append("💡 新格式：/绑定 <用户名> <ID>（用户名在前）")
            lines.append("发送 /余额 查看余额")
            yield event.plain_result("\n".join(lines))
        except Exception:
            logger.exception("[fineapi] 绑定异常 qq=%s uid=%s", qq, user_id)
            yield event.plain_result("绑定失败：插件内部错误，请稍后重试或联系管理员。")

    @filter.command("解绑", alias={"取消绑定", "unbind"})
    async def unbind(self, event: AstrMessageEvent):
        """解除当前平台用户与星芽账号的绑定（不影响站点账号）。"""
        if not self._allowed(event):
            return
        await self._ensure_ready()
        qq = str(event.get_sender_id()).strip()
        try:
            binding = await self.store.get_binding(qq)
            if not binding:
                yield event.plain_result("你还没有绑定星芽账号。")
                return
            await self.store.delete_binding(qq)
            yield event.plain_result(
                f"✅ 已解除绑定：{binding['username']}（ID {binding['newapi_user_id']}）\n"
                "站点账号和额度不受影响。"
            )
        except Exception:
            logger.exception("[fineapi] 解绑异常 qq=%s", qq)
            yield event.plain_result("解绑失败：插件内部错误，请稍后重试。")

    @filter.command("余额", alias={"查询余额", "我的额度", "balance"})
    async def balance(self, event: AstrMessageEvent):
        """查询已绑定星芽账号的余额。"""
        if not self._allowed(event):
            return
        await self._ensure_ready()
        qq = str(event.get_sender_id()).strip()

        if self._is_group(event) and bool(self._cfg("ui.private_balance_only", False)):
            yield event.plain_result("为了保护隐私，请私聊机器人发送 /余额。")
            return

        ok, wait = self._cooldown_ok(qq)
        if not ok:
            yield event.plain_result(f"查询太快了，请 {wait} 秒后再试。")
            return

        try:
            binding = await self.store.get_binding(qq)
            if not binding:
                yield event.plain_result(
                    "你还没有绑定星芽账号。\n发送 /绑定 <用户名> <ID> 完成绑定。"
                )
                return

            user = await self.client.get_user(int(binding["newapi_user_id"]))
            quota = int(user.get("quota") or 0)
            used = int(user.get("used_quota") or 0)
            count = int(user.get("request_count") or 0)
            username = str(user.get("username") or binding["username"])
            status = int(user.get("status", 1) or 1)

            lines = [
                f"👤 {username}（ID {binding['newapi_user_id']}）",
                f"💰 余额：{self._fmt_quota(quota)}",
                f"📊 累计消耗：{self._fmt_quota(used)}",
                f"🔢 调用次数：{count}",
            ]
            if status != 1:
                lines.append("⚠️ 该账号当前不是正常状态，请联系站点管理员。")
            yield event.plain_result("\n".join(lines))
        except NewAPIError as exc:
            yield event.plain_result(f"查询失败：{exc.message}")
        except Exception:
            logger.exception("[fineapi] 余额查询异常 qq=%s", qq)
            yield event.plain_result("查询失败：插件内部错误，请稍后重试。")

    @filter.command("领取首绑礼", alias={"首绑礼", "领取礼物"})
    async def claim_gift(self, event: AstrMessageEvent):
        """领取首次绑定赠送的额度。"""
        if not self._allowed(event):
            return
        await self._ensure_ready()
        qq = str(event.get_sender_id()).strip()
        try:
            binding = await self.store.get_binding(qq)
            if not binding:
                yield event.plain_result(
                    "你还没有绑定星芽账号。\n发送 /绑定 <用户名> <ID> 完成绑定。"
                )
                return
            text = await self._try_gift(qq, int(binding["newapi_user_id"]))
            yield event.plain_result(text or "当前没有可领取的首绑礼。")
        except Exception:
            logger.exception("[fineapi] 领取首绑礼异常 qq=%s", qq)
            yield event.plain_result("领取失败：插件内部错误，请稍后重试。")

    @filter.command("我的ID", alias={"我的id", "平台ID"})
    async def my_id(self, event: AstrMessageEvent):
        """查看当前平台用户 ID / 群 ID（官方机器人为 OpenID，用于配置管理员和群白名单）。"""
        sender = str(event.get_sender_id()).strip()
        group = str(event.get_group_id() or "").strip()
        lines = [f"平台用户 ID：{sender}"]
        lines.append(f"当前群 ID：{group}" if group else "当前会话：私聊")
        lines.append("以上 ID 仅用于本插件配置，不是 QQ 号。")
        yield event.plain_result("\n".join(lines))

    @filter.command("用户命令", alias={"用户帮助", "玩家命令"})
    async def user_help_cmd(self, event: AstrMessageEvent):
        """只显示普通用户可用的命令。"""
        if not self._allowed(event):
            return
        yield event.plain_result(self._user_help_text())

    @filter.command("管理命令", alias={"管理帮助", "管理员命令"})
    async def admin_help_cmd(self, event: AstrMessageEvent):
        """只显示管理员命令。"""
        if not self._allowed(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("无权限：该命令仅管理员可用。")
            return
        yield event.plain_result(self._admin_help_text())

    @filter.command("芽点帮助", alias={"星芽帮助", "芽点菜单", "帮助"})
    async def help_cmd(self, event: AstrMessageEvent):
        """显示完整命令介绍（用户 + 管理）。"""
        if not self._allowed(event):
            return
        if self._is_admin(event):
            yield event.plain_result(
                self._user_help_text() + "\n\n" + self._admin_help_text()
            )
        else:
            yield event.plain_result(
                self._user_help_text() + "\n\n管理命令：仅管理员可用"
            )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def model_status_listener(self, event: AstrMessageEvent):
        """处理 /<模型>状态 与 /模型状态。"""
        # AstrBot 在唤醒检查阶段会先去掉 wake_prefix（默认 /），到这里
        # message_str 通常是 "claude状态"；这里同时兼容仍带 / 的情况。
        raw = str(getattr(event, "message_str", "") or "").strip()
        if not getattr(event, "is_at_or_wake_command", True):
            return
        query = extract_status_query(raw)
        if not query or not self._allowed(event):
            return
        if not bool(self._cfg("model_status.enabled", True)):
            yield event.plain_result("模型状态监控未开启。")
            event.stop_event()
            return
        if bool(self._cfg("model_status.admin_only", False)) and not self._is_admin(event):
            yield event.plain_result("无权限：模型状态仅管理员可用。")
            event.stop_event()
            return
        await self._ensure_ready()

        cache_seconds = max(0, int(self._cfg("model_status.cache_seconds", 60) or 60))
        now = time.time()
        cache_key = query.lower()
        cached = self._model_status_cache.get(cache_key)
        if cached and cached[0] > now:
            if bool(self._cfg("model_status.image", True)) and cached[3]:
                yield event.image_result(cached[3])
            else:
                yield event.plain_result(cached[2])
            event.stop_event()
            return

        window_minutes = max(5, int(self._cfg("model_status.window_minutes", 60) or 60))
        max_pages = max(1, int(self._cfg("model_status.max_pages", 10) or 10))
        max_fetch_seconds = max(1, int(self._cfg("model_status.max_fetch_seconds", 12) or 12))
        min_calls = max(1, int(self._cfg("model_status.min_calls", 5) or 5))
        top_n = max(1, int(self._cfg("model_status.top_n", 10) or 10))
        end_ts = int(now)
        start_ts = end_ts - window_minutes * 60
        deadline = time.monotonic() + max_fetch_seconds

        # 成功日志、错误日志、模型价格表并发拉取；总耗时受 max_fetch_seconds 限制。
        success_result, error_result, pricing_result = await asyncio.gather(
            self._fetch_log_pages(2, start_ts, end_ts, max_pages, deadline),
            self._fetch_log_pages(5, start_ts, end_ts, max_pages, deadline),
            self.client.get_pricing(),
            return_exceptions=True,
        )
        for result in (success_result, error_result):
            if isinstance(result, NewAPIError):
                hint = (
                    "（模型状态需要 PAT 的 log:read 权限）"
                    if result.status == 403
                    else ""
                )
                yield event.plain_result(f"查询模型状态失败：{result.message}{hint}")
                event.stop_event()
                return
            if isinstance(result, Exception):
                logger.error("[fineapi] 拉取模型状态日志异常：%s", result)
                yield event.plain_result("查询模型状态失败：拉取日志时出现异常。")
                event.stop_event()
                return

        success_logs, _success_total = success_result
        error_logs, error_total = error_result

        model_names: list[str] = []
        pricing: list[dict[str, Any]] = []
        if isinstance(pricing_result, Exception):
            logger.info("[fineapi] 读取模型价格表失败（忽略）：%s", pricing_result)
        elif isinstance(pricing_result, list):
            pricing = pricing_result
        for item in pricing:
            name = str(item.get("model_name") or "").strip()
            if name:
                model_names.append(name)
        for item in success_logs + error_logs:
            if isinstance(item, dict):
                name = str(item.get("model_name") or "").strip()
                if name:
                    model_names.append(name)

        analysis = analyze_hour(
            success_logs,
            error_logs,
            model_names,
            query,
            now=end_ts,
            window_minutes=window_minutes,
            min_calls=min_calls,
            top_n=top_n,
            exclude_client_errors=bool(
                self._cfg("model_status.exclude_client_errors", True)
            ),
        )
        analysis["error_log_total"] = error_total
        if not analysis.get("models"):
            if query in {"模型", "全部", "所有"}:
                yield event.plain_result("近 1 小时暂无模型调用数据。")
                event.stop_event()
            elif re.fullmatch(r"[A-Za-z0-9._:+-]+", query):
                # 只对“看起来像模型名”的查询回复，避免拦截其他插件的 /xxx状态
                yield event.plain_result(f"未找到匹配「{query}」的模型。")
                event.stop_event()
            return

        text, markdown = build_status_text(analysis)
        image_url: str | None = None
        if bool(self._cfg("model_status.image", True)):
            image_url = await self._render_status_image(text)

        if cache_seconds > 0:
            self._model_status_cache[cache_key] = (
                now + cache_seconds,
                text,
                markdown,
                image_url,
            )
            if len(self._model_status_cache) > 200:
                cutoff = now
                self._model_status_cache = {
                    key: value
                    for key, value in self._model_status_cache.items()
                    if value[0] > cutoff
                }

        if image_url:
            yield event.image_result(image_url)
        else:
            yield event.plain_result(markdown)
        event.stop_event()

    async def _fetch_log_pages(
        self,
        log_type: int,
        start_ts: int,
        end_ts: int,
        max_pages: int,
        deadline: float | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        items: list[dict[str, Any]] = []
        total = 0
        page_size = 100
        for page in range(1, max_pages + 1):
            remaining: float | None = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.info(
                        "[fineapi] 模型状态日志拉取超过时间预算，type=%s 停在 %s 页",
                        log_type,
                        page - 1,
                    )
                    break
            payload = await self.client.get_logs(
                log_type=log_type,
                start_timestamp=start_ts,
                end_timestamp=end_ts,
                page=page,
                page_size=page_size,
                retries=0 if remaining is not None else 1,
                timeout_seconds=remaining,
            )
            page_items = payload.get("items") or []
            if isinstance(page_items, list):
                items.extend(item for item in page_items if isinstance(item, dict))
            total = int(payload.get("total") or 0)
            if len(page_items) < page_size or page * page_size >= total:
                break
        return items, total

    async def _render_status_image(self, text: str) -> str | None:
        renderer = getattr(self, "text_to_image", None)
        if renderer is None:
            return None
        try:
            url = await renderer(text)
            return str(url) if url else None
        except Exception:
            logger.exception("[fineapi] 模型状态文转图失败，回退为文本")
            return None

    # ---------- 抽奖：用户命令 ----------

    @filter.command("抽奖", alias={"参与抽奖", "join抽奖"})
    async def join_lottery_cmd(self, event: AstrMessageEvent):
        """参与当前会话中正在进行的抽奖。"""
        if not self._allowed(event):
            return
        if not bool(self._cfg("lottery.enabled", True)):
            yield event.plain_result("抽奖功能未开启。")
            return
        await self._ensure_ready()
        umo = self._lottery_umo(event)
        if not umo:
            yield event.plain_result("无法识别当前会话，暂时不能参与抽奖。")
            return
        qq = str(event.get_sender_id()).strip()

        try:
            lottery = await self.store.get_open_lottery(umo)
            if not lottery:
                latest = await self.store.list_lotteries(
                    umo=umo, status="drawn", limit=1
                )
                if latest:
                    winners = await self.store.list_lottery_winners(
                        int(latest[0]["id"])
                    )
                    yield event.plain_result("当前没有进行中的抽奖。")
                    yield self._lottery_history_result(event, latest[0], winners)
                else:
                    yield event.plain_result("当前没有进行中的抽奖。")
                return

            now = int(time.time())
            if int(lottery["draw_at"]) <= now:
                text, sent = await self._draw_lottery(lottery, announce=True)
                yield event.plain_result(
                    "✅ 抽奖已开奖，结果已发送。" if sent else text
                )
                return

            binding = await self.store.get_binding(qq)
            if not binding:
                yield event.plain_result(
                    "参与抽奖前请先绑定星芽账号：\n/绑定 <用户名> <ID>"
                )
                return

            ok, reason = await self.store.join_lottery(
                int(lottery["id"]),
                qq,
                int(binding["newapi_user_id"]),
                str(binding["username"]),
            )
            count = await self.store.count_lottery_entries(int(lottery["id"]))
            if not ok:
                if reason == "joined":
                    yield event.plain_result(
                        f"你已经参与过抽奖 #{lottery['id']} 了，当前 {count} 人参与。"
                    )
                elif reason == "closed":
                    yield event.plain_result("抽奖已到开奖时间，不能再参与。")
                else:
                    yield event.plain_result("参与失败：抽奖不存在或已结束。")
                return

            yield event.plain_result(
                "✅ 参与成功\n"
                f"🎉 抽奖 #{lottery['id']}：{self._lottery_title(lottery)}\n"
                f"🎁 奖品：{self._fmt_yadian_amount(lottery['prize_yadian'])}"
                f"（{int(lottery['winners'])} 人）\n"
                f"⏰ 开奖：{format_draw_time(int(lottery['draw_at']))}\n"
                f"👥 当前 {count} 人参与"
            )
        except Exception:
            logger.exception("[fineapi] 参与抽奖异常 qq=%s", qq)
            yield event.plain_result("参与失败：插件内部错误，请稍后重试。")

    @filter.command("抽奖信息", alias={"抽奖详情", "抽奖状态"})
    async def lottery_info(self, event: AstrMessageEvent):
        """查看当前抽奖或最近一次开奖结果。"""
        if not self._allowed(event):
            return
        if not bool(self._cfg("lottery.enabled", True)):
            yield event.plain_result("抽奖功能未开启。")
            return
        await self._ensure_ready()
        umo = self._lottery_umo(event)
        try:
            lottery = await self.store.get_open_lottery(umo) if umo else None
            if not lottery:
                latest = (
                    await self.store.list_lotteries(umo=umo, status="drawn", limit=1)
                    if umo
                    else []
                )
                if latest:
                    winners = await self.store.list_lottery_winners(int(latest[0]["id"]))
                    yield event.plain_result("当前没有进行中的抽奖。")
                    yield self._lottery_history_result(event, latest[0], winners)
                else:
                    yield event.plain_result("当前没有进行中的抽奖。")
                return

            if int(lottery["draw_at"]) <= int(time.time()):
                text, sent = await self._draw_lottery(lottery, announce=True)
                yield event.plain_result(
                    "✅ 抽奖已开奖，结果已发送。" if sent else text
                )
                return

            count = await self.store.count_lottery_entries(int(lottery["id"]))
            joined = await self.store.get_lottery_entry(
                int(lottery["id"]), str(event.get_sender_id()).strip()
            )
            remaining = max(0, int(lottery["draw_at"]) - int(time.time()))
            lines = [
                f"🎉 进行中的抽奖 #{lottery['id']}",
                f"📝 {self._lottery_title(lottery)}",
                f"🎁 奖品：{self._fmt_yadian_amount(lottery['prize_yadian'])}"
                f"（{int(lottery['winners'])} 人）",
                f"⏰ 开奖：{format_draw_time(int(lottery['draw_at']))}"
                f"（还有 {self._human_remaining(remaining)}）",
                f"👥 已参与：{count} 人",
                "✅ 你已参与" if joined else "👉 发送 /抽奖 参与",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception:
            logger.exception("[fineapi] 抽奖信息异常")
            yield event.plain_result("查询失败：插件内部错误。")

    # ---------- 抽奖：管理命令 ----------

    @filter.command("发抽奖", alias={"创建抽奖", "开抽奖"})
    async def create_lottery_cmd(self, event: AstrMessageEvent):
        """管理员：创建抽奖。

        用法：/发抽奖 [时间] [芽点] [人数] [标题]
        示例：/发抽奖 30m 100 1 周末福利
              /发抽奖 21:30 100 3
              /发抽奖 100 3（30 分钟后开奖）
        """
        if not self._allowed(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("无权限：该命令仅管理员可用。")
            return
        if not bool(self._cfg("lottery.enabled", True)):
            yield event.plain_result("抽奖功能未开启。")
            return
        await self._ensure_ready()
        umo = self._lottery_umo(event)
        if not umo:
            yield event.plain_result("无法识别当前会话，不能创建抽奖。")
            return

        try:
            draw_at, prize, winners, title = self._parse_create_lottery(event)
        except (ValueError, LotteryTimeError) as exc:
            yield event.plain_result(f"创建失败：{exc}")
            return

        try:
            existing = await self.store.get_open_lottery(umo)
            if existing:
                yield event.plain_result(
                    f"当前会话已有进行中的抽奖 #{existing['id']}，"
                    "请先 /开奖 或 /取消抽奖。"
                )
                return

            lottery_id, error = await self.store.create_lottery(
                umo,
                title,
                prize,
                winners,
                draw_at,
                str(event.get_sender_id()).strip(),
                group_id=str(event.get_group_id() or ""),
            )
            if error == "open_exists":
                yield event.plain_result("当前会话已有进行中的抽奖。")
                return
            if not lottery_id:
                yield event.plain_result("创建失败：数据库写入失败。")
                return

            yield event.plain_result(
                f"🎉 抽奖已创建 #{lottery_id}\n"
                f"📝 {title}\n"
                f"🎁 奖品：{self._fmt_yadian_amount(prize)}（{winners} 人）\n"
                f"⏰ 开奖：{format_draw_time(draw_at)}\n"
                "👉 发送 /抽奖 参与"
            )
        except Exception:
            logger.exception("[fineapi] 创建抽奖异常")
            yield event.plain_result("创建失败：插件内部错误。")

    @filter.command("开奖", alias={"立即开奖"})
    async def draw_lottery_cmd(self, event: AstrMessageEvent, lottery_id: str = ""):
        """管理员：立即开奖（不填编号则开当前会话的抽奖）。"""
        if not self._allowed(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("无权限：该命令仅管理员可用。")
            return
        await self._ensure_ready()
        umo = self._lottery_umo(event)
        try:
            lottery = None
            if str(lottery_id or "").strip().isdigit():
                lottery = await self.store.get_lottery(int(lottery_id))
            else:
                lottery = await self.store.get_open_lottery(umo) if umo else None
            if not lottery:
                yield event.plain_result("没有找到可开奖的抽奖。")
                return
            if str(lottery.get("status")) != "open":
                yield event.plain_result("该抽奖已经结束。")
                return
            text, sent = await self._draw_lottery(lottery, announce=True)
            yield event.plain_result(
                "✅ 开奖完成，结果已发送。" if sent else text
            )
        except Exception:
            logger.exception("[fineapi] 手动开奖异常")
            yield event.plain_result("开奖失败：插件内部错误。")

    @filter.command("取消抽奖", alias={"撤销抽奖"})
    async def cancel_lottery_cmd(self, event: AstrMessageEvent, lottery_id: str = ""):
        """管理员：取消当前会话或指定编号的抽奖。"""
        if not self._allowed(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("无权限：该命令仅管理员可用。")
            return
        await self._ensure_ready()
        umo = self._lottery_umo(event)
        try:
            lottery = None
            if str(lottery_id or "").strip().isdigit():
                lottery = await self.store.get_lottery(int(lottery_id))
            else:
                lottery = await self.store.get_open_lottery(umo) if umo else None
            if not lottery:
                yield event.plain_result("没有找到可取消的抽奖。")
                return
            if await self.store.cancel_lottery(int(lottery["id"])):
                yield event.plain_result(f"✅ 已取消抽奖 #{lottery['id']}。")
            else:
                yield event.plain_result("该抽奖已经结束，无法取消。")
        except Exception:
            logger.exception("[fineapi] 取消抽奖异常")
            yield event.plain_result("取消失败：插件内部错误。")

    @filter.command("抽奖管理", alias={"抽奖列表"})
    async def lottery_admin_list(self, event: AstrMessageEvent):
        """管理员：查看当前会话最近的抽奖。"""
        if not self._allowed(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("无权限：该命令仅管理员可用。")
            return
        await self._ensure_ready()
        umo = self._lottery_umo(event)
        try:
            lotteries = await self.store.list_lotteries(umo=umo or None, limit=10)
            if not lotteries:
                yield event.plain_result("还没有抽奖记录。")
                return
            lines = ["📋 抽奖管理"]
            for lottery in lotteries:
                count = await self.store.count_lottery_entries(int(lottery["id"]))
                status = {
                    "open": "进行中",
                    "drawn": "已开奖",
                    "cancelled": "已取消",
                }.get(str(lottery["status"]), str(lottery["status"]))
                lines.append(
                    f"#{lottery['id']} [{status}] {self._lottery_title(lottery)} — "
                    f"{self._fmt_yadian_amount(lottery['prize_yadian'])} / "
                    f"{int(lottery['winners'])} 人 / "
                    f"{format_draw_time(int(lottery['draw_at']))} / {count} 人参与"
                )
            yield event.plain_result("\n".join(lines))
        except Exception:
            logger.exception("[fineapi] 抽奖管理列表异常")
            yield event.plain_result("查询失败：插件内部错误。")

    @filter.command("补发抽奖", alias={"抽奖补发"})
    async def resend_lottery_cmd(self, event: AstrMessageEvent, lottery_id: str = ""):
        """管理员：补发未成功到账的中奖芽点。"""
        if not self._allowed(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("无权限：该命令仅管理员可用。")
            return
        await self._ensure_ready()
        umo = self._lottery_umo(event)
        try:
            lottery = None
            if str(lottery_id or "").strip().isdigit():
                lottery = await self.store.get_lottery(int(lottery_id))
            else:
                latest = await self.store.list_lotteries(
                    umo=umo or None, status="drawn", limit=1
                )
                lottery = latest[0] if latest else None
            if not lottery:
                yield event.plain_result("没有找到已开奖的抽奖。")
                return
            winners = await self.store.list_lottery_winners(int(lottery["id"]))
            pending = [row for row in winners if str(row["state"]) != "granted"]
            if not pending:
                yield event.plain_result(f"抽奖 #{lottery['id']} 的所有奖励都已到账。")
                return

            lines = [f"🔁 补发抽奖 #{lottery['id']}"]
            for row in pending:
                if str(row["state"]) == "unknown":
                    lines.append(
                        f"⚠️ 用户 #{row['newapi_user_id']} 状态未知，可能已到账；仍尝试补发"
                    )
                await self._award_lottery_winner(int(lottery["id"]), row)
                suffix = "✅ 已到账" if row.get("state") == "granted" else "⚠️ 仍失败"
                lines.append(
                    f"{suffix}：用户 #{row['newapi_user_id']} — "
                    f"{self._fmt_yadian_amount(quota_to_yadian(int(row['quota']), self._quota_per_unit()))}"
                )
            yield event.plain_result("\n".join(lines))
        except Exception:
            logger.exception("[fineapi] 补发抽奖异常")
            yield event.plain_result("补发失败：插件内部错误。")

    # ---------- 抽奖：内部实现 ----------

    def _lottery_umo(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "").strip()

    @staticmethod
    def _lottery_title(lottery: dict[str, Any]) -> str:
        return str(lottery.get("title") or "芽点抽奖")

    @staticmethod
    def _fmt_yadian_amount(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.0
        if abs(number - round(number)) < 1e-9:
            return f"{int(round(number))} 芽点"
        return f"{number:.2f} 芽点"

    @staticmethod
    def _human_remaining(seconds: int) -> str:
        seconds = max(0, int(seconds))
        if seconds >= 86400:
            return f"{seconds // 86400} 天"
        if seconds >= 3600:
            return f"{seconds // 3600} 小时"
        if seconds >= 60:
            return f"{seconds // 60} 分钟"
        return f"{seconds} 秒"

    def _parse_create_lottery(self, event: AstrMessageEvent) -> tuple[int, float, int, str]:
        text = str(getattr(event, "message_str", "") or "").strip()
        match = re.search(r"(?:发抽奖|创建抽奖|开抽奖)\s*(.*)$", text)
        rest = str(match.group(1) if match else "").strip()
        parts = rest.split()
        now = int(time.time())

        default_duration = int(self._cfg("lottery.default_duration_minutes", 30) or 30)
        max_days = int(self._cfg("lottery.max_duration_days", 30) or 30)

        draw_at: int | None = None
        consumed = 0
        for span in (2, 1):
            if len(parts) >= span:
                candidate = " ".join(parts[:span])
                try:
                    draw_at = parse_draw_time(
                        candidate,
                        now=now,
                        default_minutes=default_duration,
                        max_days=max_days,
                    )
                    consumed = span
                    break
                except LotteryTimeError:
                    continue
        if draw_at is None:
            draw_at = parse_draw_time(
                "",
                now=now,
                default_minutes=default_duration,
                max_days=max_days,
            )

        default_prize = float(self._cfg("lottery.default_prize_yadian", 100) or 100)
        default_winners = int(self._cfg("lottery.default_winners", 1) or 1)
        prize = default_prize
        winners = default_winners

        if len(parts) > consumed:
            try:
                prize = float(parts[consumed])
            except ValueError as exc:
                raise ValueError("奖品必须是数字（芽点）") from exc
            consumed += 1
        if len(parts) > consumed and re.fullmatch(r"\d{1,3}", parts[consumed]):
            winners = int(parts[consumed])
            consumed += 1

        title = " ".join(parts[consumed:]).strip() or "芽点抽奖"
        title = title[:50]

        max_winners = max(1, int(self._cfg("lottery.max_winners", 20) or 20))
        max_prize = float(self._cfg("lottery.max_prize_yadian", 10000) or 10000)
        if not math.isfinite(prize):
            raise ValueError("奖品必须是有效数字")
        if prize <= 0:
            raise ValueError("奖品必须大于 0 芽点")
        if winners < 1 or winners > max_winners:
            raise ValueError(f"中奖人数必须在 1~{max_winners} 之间")
        if prize > max_prize:
            raise ValueError(f"奖品不能超过 {self._fmt_yadian_amount(max_prize)}")
        if prize < winners:
            raise ValueError("奖品至少每人 1 芽点")

        return int(draw_at), float(prize), int(winners), title

    async def _draw_lottery(
        self, lottery: dict[str, Any], *, announce: bool = True
    ) -> tuple[str, bool]:
        lottery_id = int(lottery["id"])
        async with self._lottery_lock:
            fresh = await self.store.get_lottery(lottery_id)
            if not fresh or str(fresh.get("status")) != "open":
                return "该抽奖已经结束。", False

            entries = await self.store.list_lottery_entries(lottery_id)
            eligible: list[dict[str, Any]] = []
            for entry in entries:
                binding = await self.store.get_binding(str(entry["platform_id"]))
                if binding and int(binding["newapi_user_id"]) == int(
                    entry["newapi_user_id"]
                ):
                    eligible.append(entry)

            selected = pick_winners(eligible, int(fresh["winners"]))
            total_quota = yadian_to_quota(
                float(fresh["prize_yadian"]), self._quota_per_unit()
            )
            if not selected:
                shares: list[int] = []
            else:
                try:
                    shares = split_prize(total_quota, len(selected))
                except ValueError as exc:
                    return f"开奖失败：{exc}", False

            winner_rows: list[dict[str, Any]] = []
            for entry, quota in zip(selected, shares):
                winner_rows.append(
                    {
                        "platform_id": str(entry["platform_id"]),
                        "newapi_user_id": int(entry["newapi_user_id"]),
                        "username": str(entry["username"]),
                        "quota": int(quota),
                        "state": "pending",
                    }
                )

            if not await self.store.finalize_lottery(lottery_id, winner_rows):
                return "该抽奖已被处理。", False

            for row in winner_rows:
                await self._award_lottery_winner(lottery_id, row)

            text = self._lottery_result_text(
                fresh,
                winner_rows,
                entries_count=len(entries),
                eligible_count=len(eligible),
            )
            sent = False
            if announce:
                sent = await self._send_lottery_result(
                    fresh,
                    winner_rows,
                    text,
                    entries_count=len(entries),
                    eligible_count=len(eligible),
                )
            return text, sent

    async def _award_lottery_winner(
        self, lottery_id: int, row: dict[str, Any]
    ) -> None:
        try:
            await self.client.add_quota(int(row["newapi_user_id"]), int(row["quota"]))
        except NewAPIError as exc:
            state = "unknown" if exc.status == 0 else "failed"
            await self.store.update_lottery_winner(
                lottery_id,
                str(row["platform_id"]),
                state,
                reason=str(exc.message)[:200],
            )
            row["state"] = state
            row["reason"] = exc.message
            logger.warning(
                "[fineapi] 抽奖奖励发放失败 lottery=%s user=%s state=%s: %s",
                lottery_id,
                row["newapi_user_id"],
                state,
                exc.message,
            )
        except Exception:
            await self.store.update_lottery_winner(
                lottery_id, str(row["platform_id"]), "unknown", reason="unexpected_error"
            )
            row["state"] = "unknown"
            logger.exception(
                "[fineapi] 抽奖奖励发放异常 lottery=%s user=%s",
                lottery_id,
                row["newapi_user_id"],
            )
        else:
            await self.store.update_lottery_winner(
                lottery_id, str(row["platform_id"]), "granted"
            )
            row["state"] = "granted"

    def _lottery_result_header(
        self,
        lottery: dict[str, Any],
        *,
        entries_count: int,
        eligible_count: int,
    ) -> list[str]:
        lines = [
            f"🎉 抽奖开奖 #{lottery['id']}",
            f"📝 {self._lottery_title(lottery)}",
            f"🎁 奖品：{self._fmt_yadian_amount(lottery['prize_yadian'])}",
        ]
        if entries_count:
            lines.append(f"👥 参与 {entries_count} 人，符合条件 {eligible_count} 人")
        return lines

    @staticmethod
    def _lottery_winner_prefix(index: int) -> str:
        medals = ["🥇", "🥈", "🥉"]
        return medals[index] if index < len(medals) else "•"

    def _lottery_result_text(
        self,
        lottery: dict[str, Any],
        winners: list[dict[str, Any]],
        *,
        entries_count: int,
        eligible_count: int,
    ) -> str:
        lines = self._lottery_result_header(
            lottery,
            entries_count=entries_count,
            eligible_count=eligible_count,
        )
        if not winners:
            lines.append("😢 本次流局：没有符合条件的参与者。")
            return "\n".join(lines)

        failed = 0
        for index, row in enumerate(winners):
            amount = self._fmt_yadian_amount(
                quota_to_yadian(int(row["quota"]), self._quota_per_unit())
            )
            suffix = ""
            if row.get("state") != "granted":
                failed += 1
                suffix = "（发放异常，管理员可 /补发抽奖）"
            # 隐藏星芽用户名，只显示用户 ID
            lines.append(
                f"{self._lottery_winner_prefix(index)} 用户 #{row['newapi_user_id']}"
                f" — {amount}{suffix}"
            )
        if failed:
            lines.append(f"⚠️ {failed} 人奖励未确认到账，管理员可 /补发抽奖 {lottery['id']}")
        return "\n".join(lines)

    def _lottery_result_chain(
        self,
        lottery: dict[str, Any],
        winners: list[dict[str, Any]],
        *,
        entries_count: int,
        eligible_count: int,
    ):
        if MessageChain is None or not winners:
            return None

        chain = MessageChain()
        header = self._lottery_result_header(
            lottery,
            entries_count=entries_count,
            eligible_count=eligible_count,
        )
        chain.message("\n".join(header) + "\n\n")

        failed = 0
        for index, row in enumerate(winners):
            amount = self._fmt_yadian_amount(
                quota_to_yadian(int(row["quota"]), self._quota_per_unit())
            )
            suffix = ""
            if row.get("state") != "granted":
                failed += 1
                suffix = "（发放异常，管理员可 /补发抽奖）"
            chain.message(self._lottery_winner_prefix(index) + " ")
            platform_id = str(row.get("platform_id") or "").strip()
            if platform_id:
                # OneBot 下 qq 就是 QQ 号；name 用「用户」避免暴露星芽用户名
                chain.at(name="用户", qq=platform_id)
            else:
                chain.message(f"用户 #{row['newapi_user_id']}")
            chain.message(f" — {amount}{suffix}\n")
        if failed:
            chain.message(
                f"⚠️ {failed} 人奖励未确认到账，管理员可 /补发抽奖 {lottery['id']}"
            )
        return chain

    def _lottery_history_result(
        self,
        event: AstrMessageEvent,
        lottery: dict[str, Any],
        winners: list[dict[str, Any]],
    ):
        text = self._lottery_result_text(
            lottery,
            winners,
            entries_count=0,
            eligible_count=0,
        )
        display = str(self._cfg("lottery.winner_display", "at") or "at").strip().lower()
        if (
            display == "at"
            and str(lottery.get("group_id") or "").strip()
            and self._lottery_umo(event) == str(lottery.get("umo") or "")
        ):
            chain = self._lottery_result_chain(
                lottery,
                winners,
                entries_count=0,
                eligible_count=0,
            )
            if chain is not None:
                return event.chain_result(chain.chain)
        return event.plain_result(text)

    async def _send_lottery_result(
        self,
        lottery: dict[str, Any],
        winners: list[dict[str, Any]],
        text: str,
        *,
        entries_count: int,
        eligible_count: int,
    ) -> bool:
        umo = str(lottery.get("umo") or "").strip()
        if not umo:
            return False

        display = str(self._cfg("lottery.winner_display", "at") or "at").strip().lower()
        if display == "at" and str(lottery.get("group_id") or "").strip():
            chain = self._lottery_result_chain(
                lottery,
                winners,
                entries_count=entries_count,
                eligible_count=eligible_count,
            )
            if chain is not None:
                try:
                    await self.context.send_message(umo, chain)
                    return True
                except Exception:
                    logger.exception("[fineapi] 抽奖结果 @ 消息发送失败，回退文本")
        return await self._send_text(umo, text)

    async def _lottery_loop(self) -> None:
        interval = max(5, int(self._cfg("lottery.auto_draw_interval_seconds", 15) or 15))
        while not self._stopping:
            try:
                due = await self.store.list_due_lotteries()
                for lottery in due:
                    if self._stopping:
                        break
                    text, _sent = await self._draw_lottery(lottery, announce=True)
                    logger.info(
                        "[fineapi] 自动开奖 #%s：%s",
                        lottery.get("id"),
                        text.splitlines()[0] if text else "",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[fineapi] 自动开奖循环异常")
            await asyncio.sleep(interval)

    async def _send_text(self, umo: str, text: str) -> bool:
        if not umo or MessageChain is None:
            return False
        try:
            await self.context.send_message(umo, MessageChain().message(text))
            return True
        except Exception:
            logger.exception("[fineapi] 主动消息发送失败 umo=%s", umo)
            return False

    # ---------- 管理命令 ----------

    @filter.command("强制解绑", alias={"管理员解绑"})
    async def admin_unbind(self, event: AstrMessageEvent, target_id: str = ""):
        """管理员：强制解除某个平台用户的绑定。"""
        if not self._allowed(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("无权限：该命令仅管理员可用。")
            return
        await self._ensure_ready()
        target = str(target_id or "").strip()
        if not target:
            yield event.plain_result("用法：/强制解绑 <平台用户ID>")
            return
        try:
            binding = await self.store.get_binding(target)
            if not binding:
                yield event.plain_result(f"平台用户 {target} 没有绑定记录。")
                return
            await self.store.delete_binding(target)
            yield event.plain_result(
                f"✅ 已强制解绑平台用户 {target}（原账号 {binding['username']}，"
                f"ID {binding['newapi_user_id']}）。"
            )
        except Exception:
            logger.exception("[fineapi] 强制解绑异常 target=%s", target)
            yield event.plain_result("操作失败：插件内部错误。")

    @filter.command("绑定查询", alias={"查询绑定"})
    async def admin_lookup(self, event: AstrMessageEvent, keyword: str = ""):
        """管理员：按平台用户 ID 或星芽用户 ID 查询绑定。"""
        if not self._allowed(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("无权限：该命令仅管理员可用。")
            return
        await self._ensure_ready()
        keyword = str(keyword or "").strip()
        if not keyword:
            yield event.plain_result("用法：/绑定查询 <平台用户ID或星芽用户ID>")
            return
        try:
            binding = await self.store.find_binding(keyword)
            if not binding:
                if keyword.isdigit():
                    grant = await self.store.get_gift_grant(user_id=int(keyword))
                    if grant:
                        state = GIFT_STATE_LABELS.get(
                            str(grant.get("state")), str(grant.get("state"))
                        )
                        yield event.plain_result(
                            "未找到当前绑定记录，但存在发放台账：\n"
                            f"平台用户：{grant['platform_id']}\n"
                            f"星芽用户 ID：{grant['newapi_user_id']}\n"
                            f"状态：{state}\n"
                            f"额度：{self._fmt_quota(grant.get('quota'))}"
                        )
                        return
                yield event.plain_result(f"未找到与 {keyword} 相关的绑定记录。")
                return
            state = GIFT_STATE_LABELS.get(
                str(binding.get("gift_state")), str(binding.get("gift_state"))
            )
            lines = [
                f"平台用户：{binding['qq']}",
                f"星芽账号：{binding['username']}（ID {binding['newapi_user_id']}）",
                f"绑定时间：{self._fmt_time(binding.get('bound_at'))}",
                f"首绑礼：{state}",
            ]
            if binding.get("gift_quota"):
                lines.append(f"礼物额度：{self._fmt_quota(binding['gift_quota'])}")
            if binding.get("gift_reason"):
                lines.append(f"备注：{binding['gift_reason']}")
            yield event.plain_result("\n".join(lines))
        except Exception:
            logger.exception("[fineapi] 绑定查询异常 keyword=%s", keyword)
            yield event.plain_result("查询失败：插件内部错误。")

    @filter.command("补发首绑礼", alias={"补发礼物"})
    async def admin_regrant(
        self, event: AstrMessageEvent, target_id: str = "", force: str = ""
    ):
        """管理员：补发首绑礼。状态异常时需加 force。"""
        if not self._allowed(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("无权限：该命令仅管理员可用。")
            return
        await self._ensure_ready()
        target = str(target_id or "").strip()
        if not target:
            yield event.plain_result("用法：/补发首绑礼 <平台用户ID> [force]")
            return
        try:
            binding = await self.store.get_binding(target)
            if not binding:
                yield event.plain_result(f"平台用户 {target} 没有绑定记录。")
                return
            force_flag = str(force or "").strip().lower() in {"force", "强制", "1", "true"}
            uid = int(binding["newapi_user_id"])
            if not force_flag and self._is_old_gift_user(uid):
                yield event.plain_result(
                    f"该账号为老用户（ID {uid} ≤ {self._gift_new_user_id_after()}），"
                    "默认不发放；确需补发请加 force。"
                )
                return
            text = await self._try_gift(
                target, uid, force=force_flag, reason="manual"
            )
            yield event.plain_result(text or "未执行发放。")
        except Exception:
            logger.exception("[fineapi] 补发首绑礼异常 target=%s", target)
            yield event.plain_result("操作失败：插件内部错误。")

    # ---------- 首绑礼 ----------

    async def _try_gift(
        self, qq: str, user_id: int, *, force: bool = False, reason: str = "first_bind"
    ) -> str:
        if not bool(self._cfg("gift.enabled", True)):
            return "🎁 首绑礼未开启。"

        binding = await self.store.get_binding(qq)
        if not binding:
            return "找不到绑定记录，请先绑定。"

        if not force and str(binding.get("gift_state") or "none") == "granted":
            return "🎁 首绑礼已经领取过了。"

        # 老用户（ID <= 阈值）不发放注册礼，也不对用户提示；
        # 仍然记录 skipped 便于管理员查询。管理员 force 可人工补发。
        if not force and self._is_old_gift_user(int(user_id)):
            threshold = self._gift_new_user_id_after()
            if str(binding.get("gift_state") or "none") != "skipped":
                await self.store.set_gift_state(
                    qq, "skipped", quota=0, reason=f"old_user<={threshold}"
                )
            return ""

        try:
            amount_usd = float(self._cfg("gift.amount_usd", 1.0) or 0)
        except (TypeError, ValueError):
            amount_usd = 0.0
        quota = usd_to_quota(amount_usd, self._quota_per_unit())
        if quota <= 0:
            return "🎁 首绑礼金额为 0，未发放。"

        # 发放台账独立于绑定关系：解绑/重绑后仍然有效，防止重复领取。
        grant = await self.store.get_gift_grant(qq, user_id)
        if not force and grant:
            grant_state = str(grant.get("state") or "")
            grant_quota = int(grant.get("quota") or quota)
            if grant_state == "granted":
                await self._sync_binding_gift(
                    qq, "granted", grant_quota, "previously_granted"
                )
                return "🎁 首绑礼已经领取过了。"
            if grant_state in {"pending", "unknown"}:
                await self._sync_binding_gift(
                    qq, grant_state, grant_quota, "grant_exists"
                )
                return (
                    "⚠️ 首绑礼状态待核对，为避免重复发放已暂停自动领取。\n"
                    "请联系管理员使用 /补发首绑礼 处理。"
                )

        if force:
            await self.store.add_log(qq, "force_gift", f"uid={user_id} quota={quota}")
        else:
            reserved, reserve_state = await self.store.reserve_gift(
                qq, user_id, quota, reason
            )
            if not reserved:
                if reserve_state == "granted":
                    await self._sync_binding_gift(
                        qq, "granted", quota, "previously_granted"
                    )
                    return "🎁 首绑礼已经领取过了。"
                if reserve_state in {"pending", "unknown"}:
                    return (
                        "⚠️ 首绑礼状态待核对，为避免重复发放已暂停自动领取。\n"
                        "请联系管理员使用 /补发首绑礼 处理。"
                    )
                return "⚠️ 首绑礼发放状态冲突，请联系管理员核对。"

        await self.store.set_gift_state(qq, "pending", quota=quota, reason=reason)
        try:
            await self.client.add_quota(int(user_id), quota)
        except NewAPIError as exc:
            # status=0 表示没有拿到有效 HTTP 响应（超时/连接失败），
            # 无法确认是否已到账，标记 unknown，禁止自动重试。
            if exc.status == 0:
                if not force:
                    await self.store.mark_gift_unknown(
                        qq, user_id, quota, str(exc.message)[:200]
                    )
                await self.store.set_gift_state(
                    qq, "unknown", quota=quota, reason=str(exc.message)[:200]
                )
                logger.warning(
                    "[fineapi] 首绑礼结果未知 qq=%s uid=%s: %s", qq, user_id, exc.message
                )
                return "⚠️ 首绑礼发放结果未知（请求超时），请联系管理员核对后再补发。"

            if not force:
                await self.store.release_gift(qq)
            await self.store.set_gift_state(
                qq, "failed", quota=quota, reason=str(exc.message)[:200]
            )
            logger.warning(
                "[fineapi] 首绑礼发放失败 qq=%s uid=%s status=%s: %s",
                qq,
                user_id,
                exc.status,
                exc.message,
            )
            return f"⚠️ 首绑礼发放失败：{exc.message}"
        except Exception:
            # 未预期的异常同样不能确定是否已到账，标记 unknown 并禁止自动重试。
            if not force:
                await self.store.mark_gift_unknown(qq, user_id, quota, "unexpected_error")
            await self.store.set_gift_state(
                qq, "unknown", quota=quota, reason="unexpected_error"
            )
            logger.exception("[fineapi] 首绑礼发放异常 qq=%s uid=%s", qq, user_id)
            return "⚠️ 首绑礼发放出现异常，请联系管理员核对。"

        completed = await self.store.complete_gift(qq, user_id, quota, reason)
        if not completed:
            logger.warning(
                "[fineapi] 发放台账写入冲突（可能已有其他账号的领取记录）qq=%s uid=%s",
                qq,
                user_id,
            )
        await self.store.set_gift_state(qq, "granted", quota=quota, reason=reason)
        return (
            f"🎁 首绑礼已到账：{format_usd(amount_usd)}"
            f"（{format_yadian(amount_usd * 100)}）"
        )

    async def _sync_binding_gift(
        self, qq: str, state: str, quota: int, reason: str
    ) -> None:
        binding = await self.store.get_binding(qq)
        if binding and str(binding.get("gift_state") or "none") != state:
            await self.store.set_gift_state(qq, state, quota=quota, reason=reason)
