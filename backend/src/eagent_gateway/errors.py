# -*- coding: utf-8 -*-
"""平台统一错误（from_nekiro/07-fail-closed-errors.md 错误码表）。

- code / http_status / safe_message 固定在类上；message 缺省取 safe_message。
- message 必须是安全文案：只允许本仓库代码显式传入的操作性提示
  （如 "slug 已存在"），不得透传异常字符串、堆栈、SQL、内部地址。
- trace_id 贯穿日志与响应，是 P0-4 调用血缘的锚点。
- 运行时不 fallback：失败必须以明确的 code 暴露，不伪装成功。
"""
from __future__ import annotations


class EAgentError(Exception):
    code = "INTERNAL_ERROR"
    http_status = 500
    safe_message = "服务内部错误"

    def __init__(
        self,
        message: str | None = None,
        *,
        trace_id: str | None = None,
        details: list[dict[str, str]] | None = None,
    ):
        super().__init__(message or self.safe_message)
        self.message = message or type(self).safe_message
        self.trace_id = trace_id
        # details 仅用于字段级提示（VALIDATION_ERROR），不含原始输入值
        self.details = details

    def payload(self) -> dict:
        body: dict = {
            "code": self.code,
            "message": self.message,
            "traceId": self.trace_id,
        }
        if self.details:
            body["details"] = self.details
        return body


class ValidationError(EAgentError):
    code = "VALIDATION_ERROR"
    http_status = 400
    safe_message = "请求参数不合法"


class HiAgentInputsValidationError(EAgentError):
    """HiAgent Inputs 不符合注册契约（fail-fast，不发起远端调用）。"""

    code = "HIAGENT_INPUTS_INVALID"
    http_status = 400
    safe_message = "缺少必填参数"


class HiAgentConversationBusyError(EAgentError):
    """同一 HiAgent 会话已有 update→chat 临界区正在执行。"""

    code = "HIAGENT_CONVERSATION_BUSY"
    http_status = 409
    safe_message = "当前会话正在处理上一条请求，请稍后重试"


class UnauthenticatedError(EAgentError):
    code = "UNAUTHENTICATED"
    http_status = 401
    safe_message = "缺少或无效的身份凭证"


class ForbiddenError(EAgentError):
    code = "FORBIDDEN"
    http_status = 403
    safe_message = "无权访问该资源"


class MasterInvocationForbiddenError(ForbiddenError):
    """禁止 Agent 调用 Master（只允许业务根请求进入 Master）。"""

    code = "MASTER_TO_MASTER_FORBIDDEN"
    safe_message = "禁止从 Agent 调用主 Agent"


class NotFoundError(EAgentError):
    code = "NOT_FOUND"
    http_status = 404
    safe_message = "资源不存在"


class ConflictError(EAgentError):
    code = "CONFLICT"
    http_status = 409
    safe_message = "状态冲突"


class ServiceDisabledError(ConflictError):
    """服务已禁用（P0-5 起在调用链上使用）。"""

    code = "SERVICE_DISABLED"
    safe_message = "服务已禁用"


class PayloadTooLargeError(EAgentError):
    code = "PAYLOAD_TOO_LARGE"
    http_status = 413
    safe_message = "请求体过大"


class DependencyError(EAgentError):
    code = "DEPENDENCY_ERROR"
    http_status = 502
    safe_message = "远端依赖调用失败"


class InstanceUnavailableError(EAgentError):
    code = "INSTANCE_UNAVAILABLE"
    http_status = 503
    safe_message = "没有可用实例"


class GatewayTimeoutError(EAgentError):
    code = "TIMEOUT"
    http_status = 504
    safe_message = "调用超时"


class RequestCanceledError(EAgentError):
    code = "CANCELED"
    http_status = 499
    safe_message = "请求已取消"


class RateLimitedError(EAgentError):
    code = "RATE_LIMITED"
    http_status = 429
    safe_message = "尝试过于频繁，请稍后再试"


# code -> HTTP 状态映射（契约测试断言与 07 文档错误码表一致）
_ERROR_TYPES = (
    ValidationError,
    UnauthenticatedError,
    ForbiddenError,
    MasterInvocationForbiddenError,
    NotFoundError,
    ConflictError,
    ServiceDisabledError,
    PayloadTooLargeError,
    DependencyError,
    InstanceUnavailableError,
    GatewayTimeoutError,
    RequestCanceledError,
    RateLimitedError,
    EAgentError,
)
ERROR_STATUS: dict[str, int] = {
    cls.code: cls.http_status for cls in _ERROR_TYPES
}


# 工具错误码识别与 HTTP 状态映射是两个不同契约。HiAgentAPIError 会按远端
# 失败阶段动态设置 code，不能依赖 ERROR_STATUS 判断是否允许透传。
HIAGENT_RUNTIME_MESSAGES: dict[str, str] = {
    "HIAGENT_INPUTS_INVALID": "业务参数缺失或不符合要求",
    "HIAGENT_CONVERSATION_BUSY": "当前会话正在处理上一条请求，请稍后重试",
    "HIAGENT_INVALID_RESPONSE": "返回内容不符合协议",
    "HIAGENT_EMPTY_RESPONSE": "未返回有效内容",
    "HIAGENT_INCOMPLETE_RESPONSE": "响应未完整结束",
    "HIAGENT_UPSTREAM_ERROR": "上游执行失败",
    "HIAGENT_RESPONSE_TOO_LARGE": "返回内容超过大小限制",
    "HIAGENT_UPDATE_FAILED": "会话参数更新失败",
}
RUNTIME_ERROR_CODES = frozenset(ERROR_STATUS) | frozenset(HIAGENT_RUNTIME_MESSAGES)
RETRYABLE_RUNTIME_ERROR_CODES = frozenset({"HIAGENT_CONVERSATION_BUSY"})


def is_runtime_error_code(code: str | None) -> bool:
    return bool(code and code in RUNTIME_ERROR_CODES)


def runtime_error_is_retryable(code: str | None) -> bool:
    return bool(code and code in RETRYABLE_RUNTIME_ERROR_CODES)


def runtime_error_message(
    code: str,
    *,
    service: str | None = None,
    detail: str | None = None,
) -> str:
    """返回可对外展示的稳定文案；仅参数校验可追加本地生成的字段详情。"""
    if code == "INTERNAL_ERROR":
        return "处理失败，请稍后重试。"
    if code == "HIAGENT_INPUTS_INVALID":
        message = HIAGENT_RUNTIME_MESSAGES[code]
        if detail:
            message = f"{message}：{detail}"
    elif code in HIAGENT_RUNTIME_MESSAGES:
        message = HIAGENT_RUNTIME_MESSAGES[code]
    elif code == "TIMEOUT":
        message = "调用超时，请根据 traceId 确认执行状态"
    elif code == "DEPENDENCY_ERROR":
        message = "调用失败，请稍后重试"
    elif code == "INSTANCE_UNAVAILABLE":
        message = "当前没有可用实例"
    elif code == "SERVICE_DISABLED":
        message = "已被禁用"
    elif code == "VALIDATION_ERROR":
        message = "调用参数不合法"
    elif code == "RATE_LIMITED":
        message = "调用过于频繁，请稍后重试"
    else:
        message = next(
            (cls.safe_message for cls in _ERROR_TYPES if cls.code == code),
            "处理失败，请稍后重试。",
        )
    return f"子服务 {service} {message}" if service else message
