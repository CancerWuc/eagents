# -*- coding: utf-8 -*-
"""实例目录抽象（from_nekiro/06 §5，P1-2）。

一期实现: registry.mysql_provider.MysqlInstanceDirectory（同步 SQL，
与现有 Database 栈一致）；二期接 Nacos/K8s 时可在 Provider 内做异步
适配层。二期可加 observe/watch。
"""
from __future__ import annotations

from typing import Protocol

from .model import Instance, InstanceInput


class InstanceDirectory(Protocol):
    """Provider-neutral 实例注册表接口（一期同步实现）。"""

    def snapshot(self, service_id: str) -> list[Instance]:
        """该服务当前全部未过期实例（任意状态）。"""
        ...

    def ready_instances(self, service_id: str) -> list[Instance]:
        """该服务 READY 且租约有效的实例（路由候选）。"""
        ...

    def has_instances(self, service_id: str) -> bool:
        """该服务是否注册过实例（未注册 = 未启用实例发现）。"""
        ...

    def register(self, instance: InstanceInput) -> Instance:
        ...

    def heartbeat(self, instance_id: str, ttl_seconds: int) -> Instance:
        ...

    def deregister(self, instance_id: str) -> Instance:
        ...
