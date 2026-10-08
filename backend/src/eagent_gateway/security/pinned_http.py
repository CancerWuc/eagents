# -*- coding: utf-8 -*-
"""SSRF 防护（设计 §10.2 / §15.1）：连接时固定已校验地址。

解决 TOCTOU / DNS-rebinding：传统「先校验后请求」中 httpx 会对 hostname
二次解析，攻击者可让两次解析返回不同结果。此处把解析+校验+连接收敛到
同一个 transport 内完成，并把 TCP 连接固定到校验通过的 IP，TLS 的 SNI
与证书校验仍使用原 hostname。

用法：所有出站 HTTP（注册/刷新 Card、A2A 工具调用）一律经
`guarded_async_client()` 创建 httpx client。
"""
import asyncio
import ipaddress
import socket
from typing import Any

import httpx


class AddressBlockedError(ValueError):
    """目标地址未通过 SSRF 校验。"""


def check_ip_allowed(ip_str: str, *, allow_private: bool) -> None:
    ip = ipaddress.ip_address(ip_str)
    if allow_private:
        return
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise AddressBlockedError(f"禁止访问内网/保留地址: {ip}")


def _check_resolved(infos: list, *, allow_private: bool) -> list[str]:
    """getaddrinfo 结果的统一校验：全部地址都必须合法，否则整体拒绝。

    比 `resolve_first_allowed`（取第一个合法地址）严格：解析出公网 + 内网
    混合地址的主机名在这里直接被拒，用于注册/探测前的预检。两者语义不同，
    不可互相替换。"""
    ips = sorted({info[4][0] for info in infos})
    for ip_str in ips:
        check_ip_allowed(ip_str, allow_private=allow_private)
    return ips


def check_hostname(
    host: str, *, allow_private: bool = False,
) -> list[str]:
    """同步解析并校验 hostname，返回允许的 IP 列表（全部地址都必须合法）。

    ⚠️ `socket.getaddrinfo` 是阻塞调用：**只能在同步上下文里用**
    （FastAPI 的同步路由跑在线程池里，不影响事件循环）。协程里请改用
    `check_hostname_async`，否则解析失败的主机名会把整个事件循环冻住
    （不可达主机在 Windows 上单次解析约 2 秒）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise AddressBlockedError(f"主机名解析失败: {host}") from exc
    return _check_resolved(infos, allow_private=allow_private)


async def check_hostname_async(
    host: str, *, allow_private: bool = False,
) -> list[str]:
    """`check_hostname` 的异步版：校验语义完全相同（全部地址都必须合法），
    只是把解析交给事件循环的 executor，不阻塞协程。"""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise AddressBlockedError(f"主机名解析失败: {host}") from exc
    return _check_resolved(infos, allow_private=allow_private)


async def resolve_first_allowed(
    host: str, *, allow_private: bool = False,
) -> str:
    """异步解析 hostname，返回第一个通过校验的 IP（用于固定连接）。"""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise AddressBlockedError(f"主机名解析失败: {host}") from exc
    for info in infos:
        ip_str = info[4][0]
        try:
            check_ip_allowed(ip_str, allow_private=allow_private)
        except AddressBlockedError:
            continue
        return ip_str
    raise AddressBlockedError(
        f"主机 {host} 解析出的全部地址均被 SSRF 策略拒绝",
    )


async def resolve_first(host: str) -> str:
    """异步解析 hostname，返回第一个 IP（**无策略校验**）。

    仅用于豁免主机（`allowed_hosts()`，hiagent-integration/01 §3.4）的
    固定连接：跳过私网地址段校验，但解析+固定 IP 仍然保留（防 DNS-rebinding
    的连接固定语义不因豁免而丢失）。"""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise AddressBlockedError(f"主机名解析失败: {host}") from exc
    if not infos:
        raise AddressBlockedError(f"主机名解析结果为空: {host}")
    return infos[0][4][0]


def allowed_hosts() -> frozenset[str]:
    """SSRF 主机级豁免清单（hiagent-integration/01 §3.4）。

    hiagent 是内网服务，默认的私网地址段校验会拦掉它。豁免**只放开地址段
    校验**：固定 IP、禁重定向、超时、响应大小上限全部保留。集合 =
    HIAGENT_URL 主机 ∪ EAGENT_HIAGENT_ALLOW_HOST 显式清单。实例级 base_url
    若指向其他主机须显式加入 env——不做「注册行静默开门」，注册时的连通性
    测试会让未豁免主机立即暴露（fail-closed）。"""
    from urllib.parse import urlparse

    from ..config import settings

    hosts = {h.strip().lower() for h in settings.hiagent_allow_hosts if h.strip()}
    try:
        parsed = urlparse(settings.hiagent_url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        host = ""
    if host:
        hosts.add(host)
    return frozenset(hosts)


class SSRFGuardAsyncTransport(httpx.AsyncHTTPTransport):
    """出站请求统一走：解析 → 校验 → 固定 IP 连接（同一处完成，无 TOCTOU）。

    allowed_hosts 中的主机（hiagent 等，见 `allowed_hosts()`）豁免**地址段
    校验**，但仍解析并固定 IP——豁免不等于裸奔。"""

    def __init__(
        self, *, allow_private: bool = False,
        allowed_hosts: frozenset[str] = frozenset(),
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self._allow_private = allow_private
        self._allowed_hosts = {h.lower() for h in allowed_hosts}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if not host:
            raise AddressBlockedError("请求缺少主机名")
        exempt = host.lower() in self._allowed_hosts
        # 已是 IP 字面量：直接校验
        try:
            ipaddress.ip_address(host.strip("[]"))
            if not exempt:
                check_ip_allowed(
                    host.strip("[]"), allow_private=self._allow_private,
                )
            return await super().handle_async_request(request)
        except ValueError:
            pass

        if exempt:
            pinned_ip = await resolve_first(host)
        else:
            pinned_ip = await resolve_first_allowed(
                host, allow_private=self._allow_private,
            )
        # TCP 连接固定到校验通过的 IP；Host 头 / SNI / 证书校验保持原 hostname
        original_netloc = request.url.netloc.decode("ascii")
        # copy_with(host=...) 会保留原端口，并正确处理 IPv6 方括号。
        request.url = request.url.copy_with(host=pinned_ip)
        request.headers["host"] = original_netloc
        request.extensions["sni_hostname"] = host
        return await super().handle_async_request(request)


def guarded_async_client(**kw: Any) -> httpx.AsyncClient:
    """创建带 SSRF 防护的 httpx AsyncClient（网关内所有出站请求使用）。"""
    from ..config import settings

    kw.setdefault("timeout", 30.0)
    kw.setdefault("follow_redirects", False)
    transport = SSRFGuardAsyncTransport(
        allow_private=settings.allow_private_addresses,
        allowed_hosts=allowed_hosts(),
    )
    return httpx.AsyncClient(transport=transport, **kw)
