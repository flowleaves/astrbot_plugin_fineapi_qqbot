"""NewAPI 管理 API 客户端。

只使用稳定的 Dashboard 管理接口：
- GET  /api/user/:id
- POST /api/user/manage   (action=add_quota, mode=add)

认证：Authorization: Bearer <个人访问令牌 PAT>
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp


class NewAPIError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class NewAPIClient:
    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        admin_user_id: int = 0,
        timeout_seconds: int = 10,
        max_concurrent_requests: int = 12,
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        if self.base_url and not self.base_url.startswith(("http://", "https://")):
            self.base_url = "https://" + self.base_url
        self.access_token = (access_token or "").strip()
        self.admin_user_id = int(admin_user_id or 0)
        self.timeout = aiohttp.ClientTimeout(total=max(3, int(timeout_seconds or 10)))
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        # 抽奖/绑定集中爆发时，限制同时打到 NewAPI 的请求数，避免 429 与超时。
        self._semaphore = asyncio.Semaphore(
            max(1, int(max_concurrent_requests or 12))
        )

    def configured(self) -> bool:
        return bool(self.base_url and self.access_token)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=self.timeout)
            return self._session

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "fineapi-qqbot/0.4.5 (AstrBot)",
        }
        # 仅旧版 NewAPI 需要；rc50 可填 0 不发送。
        if self.admin_user_id > 0:
            headers["New-Api-User"] = str(self.admin_user_id)
        return headers

    @staticmethod
    def _friendly_http_message(status: int, message: str) -> str:
        if status == 401:
            return "访问令牌无效或已过期，请检查插件配置"
        if status == 403:
            return "访问令牌权限不足，或请求被 Cloudflare/WAF 拦截"
        if status == 404:
            return message or "用户或接口不存在"
        if status == 429:
            return "请求过于频繁，请稍后再试"
        return message or f"站点返回 HTTP {status}"

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
        retries: int = 2,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise NewAPIError("插件未配置站点地址（site.base_url）")
        if not self.access_token:
            raise NewAPIError("插件未配置访问令牌（site.access_token）")

        url = f"{self.base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            try:
                async with self._semaphore:
                    session = await self._get_session()
                    request_timeout = None
                    if timeout_seconds is not None:
                        request_timeout = aiohttp.ClientTimeout(
                            total=max(1.0, float(timeout_seconds))
                        )
                    request_kwargs: dict[str, Any] = {
                        "params": params,
                        "json": payload,
                        "headers": self._headers(),
                    }
                    if request_timeout is not None:
                        request_kwargs["timeout"] = request_timeout
                    async with session.request(method, url, **request_kwargs) as resp:
                        text = await resp.text()
                        content_type = (resp.headers.get("Content-Type") or "").lower()

                        if (
                            resp.status in (429, 500, 502, 503, 504)
                            and attempt < retries
                        ):
                            await asyncio.sleep(0.5 * (attempt + 1))
                            continue

                        if resp.status >= 500:
                            raise NewAPIError(
                                self._friendly_http_message(resp.status, ""), resp.status
                            )

                        if "application/json" not in content_type:
                            raise NewAPIError(
                                f"站点返回了非 JSON 响应（HTTP {resp.status}），"
                                "可能被 Cloudflare 拦截或地址配置错误",
                                resp.status,
                            )

                        try:
                            data = json.loads(text)
                        except ValueError as exc:
                            raise NewAPIError(
                                f"站点返回无法解析的 JSON（HTTP {resp.status}）",
                                resp.status,
                            ) from exc

                        if not isinstance(data, dict):
                            raise NewAPIError("站点返回格式异常", resp.status)

                        if resp.status >= 400 or not data.get("success", False):
                            message = str(data.get("message") or "")
                            raise NewAPIError(
                                self._friendly_http_message(resp.status, message),
                                resp.status,
                                str(data.get("code") or ""),
                            )

                        return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise NewAPIError("连接星芽站点失败，请稍后重试") from exc

        raise NewAPIError("请求星芽站点失败") from last_error

    async def get_user(self, user_id: int) -> dict[str, Any]:
        data = await self._request("GET", f"/api/user/{int(user_id)}")
        user = data.get("data")
        if not isinstance(user, dict):
            raise NewAPIError("站点未返回用户信息")
        return user

    async def add_quota(self, user_id: int, quota: int) -> None:
        # 注意：不做自动重试，避免“请求已生效但响应超时”导致重复发放。
        await self._request(
            "POST",
            "/api/user/manage",
            {
                "id": int(user_id),
                "action": "add_quota",
                "mode": "add",
                "value": int(quota),
            },
            retries=0,
        )

    async def get_logs(
        self,
        *,
        log_type: int | None = None,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
        page: int = 1,
        page_size: int = 100,
        model_name: str | None = None,
        retries: int = 1,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """读取 Dashboard 日志（需要 log:read）。

        返回分页对象：{"page": 1, "page_size": 100, "total": 123, "items": [...]}
        """
        params: dict[str, Any] = {
            "p": max(1, int(page)),
            "page_size": max(1, min(100, int(page_size))),
        }
        if log_type is not None:
            params["type"] = int(log_type)
        if start_timestamp is not None:
            params["start_timestamp"] = int(start_timestamp)
        if end_timestamp is not None:
            params["end_timestamp"] = int(end_timestamp)
        if model_name:
            params["model_name"] = str(model_name)

        data = await self._request(
            "GET",
            "/api/log/",
            params=params,
            retries=retries,
            timeout_seconds=timeout_seconds,
        )
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise NewAPIError("站点未返回日志数据")
        return payload

    async def get_pricing(self) -> list[dict[str, Any]]:
        """读取模型价格表，用于模糊查询时的模型名候选。"""
        data = await self._request("GET", "/api/pricing")
        payload = data.get("data")
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    async def get_perf_summary(self, hours: int = 1) -> dict[str, Any]:
        """读取模型性能汇总（公开/登录接口，不需 log 权限）。"""
        data = await self._request(
            "GET", "/api/perf-metrics/summary", params={"hours": max(1, int(hours))}
        )
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise NewAPIError("站点未返回性能数据")
        return payload

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
