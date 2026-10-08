# -*- coding: utf-8 -*-
"""网关配置面错误码（higress-mcp-gateway 03 §4.2）。

沿用 KB 先例（kb/runtime.py）：只定义模块级子类改写 code/safe_message，
不改写全局 ERROR_STATUS（该表与 from_nekiro/07 错误码表做契约对账）；
错误处理器直接读 http_status，天然兼容。
"""
from __future__ import annotations

from .. import errors


class GatewayK8sForbiddenError(errors.ForbiddenError):
    """K8s API 返回 403（RBAC 不足或 token 失效）。"""

    code = "GATEWAY_K8S_FORBIDDEN"
    safe_message = "K8s 拒绝访问（RBAC 权限不足或凭证失效）"


class GatewayResourceNotFoundError(errors.NotFoundError):
    """K8s 资源不存在（McpBridge/WasmPlugin/ConfigMap）。"""

    code = "GATEWAY_RESOURCE_NOT_FOUND"
    safe_message = "网关资源不存在"


class GatewayConflictError(errors.ConflictError):
    """双写冲突（console 共存期外部改动 / 资源被占用）。"""

    code = "GATEWAY_CONFLICT"
    safe_message = "网关资源状态冲突"


class GatewayK8sError(errors.DependencyError):
    """其他 K8s API 故障（网络/版本不兼容等）。"""

    code = "GATEWAY_K8S_ERROR"
    safe_message = "K8s API 调用失败"


class GatewayConfigError(errors.EAgentError):
    """控制面自身配置缺失（主密钥未配置等，fail-secure）。"""

    code = "GATEWAY_CONFIG_MISSING"
    http_status = 500
    safe_message = "网关控制面配置缺失"
