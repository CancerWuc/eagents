# -*- coding: utf-8 -*-
"""K8s 客户端封装（higress-mcp-gateway 03 T04）。

- 凭证：集群内自动走 in-cluster ServiceAccount（RBAC 最小权限，01 §5.3）；
  集群外（本地/dev 直连）用 kubeconfig（EAGENT_KUBECONFIG 或 ~/.kube/config），
  按 profile.kube_context 选择 context。
- 只访问三类对象：McpBridge/WasmPlugin（CustomObjectsApi，CRD）与
  ConfigMap/pods（CoreV1Api）；不安装组件、不改 CRD（边界见 03 §1）。
- 写路径全部 read-modify-write：replace 携带 resourceVersion，共存期与
  console 双写冲突会得到 K8s 409 → 映射 GATEWAY_CONFLICT（03 §4.2）。
- kubernetes 依赖懒加载：模块导入不需要安装该库（单测 mock 本层）。
"""
from __future__ import annotations

import logging
import threading

from .. import config
from .errors import (
    GatewayConflictError,
    GatewayK8sError,
    GatewayK8sForbiddenError,
    GatewayResourceNotFoundError,
)

logger = logging.getLogger("eagent.gateway.k8s")

MCPBRIDGE_GROUP = "networking.higress.io"
MCPBRIDGE_VERSION = "v1"
MCPBRIDGE_PLURAL = "mcpbridges"
WASMPLUGIN_GROUP = "extensions.higress.io"
# Higress 2.2.3 实测：WasmPlugin 走 v1alpha1；老/新版本可能注册为 v1，
# 首次访问时按 v1alpha1 → v1 顺序探测并缓存（02 基准 §0 勘误记录）
WASMPLUGIN_VERSION_CANDIDATES = ("v1alpha1", "v1")
WASMPLUGIN_PLURAL = "wasmplugins"

AI_ROUTE_LABEL = "higress.io/config-map-type"
AI_ROUTE_LABEL_VALUE = "ai-route"

_CONFIG_LOCK = threading.Lock()
_CONFIG_LOADED = False


def _load_kube_config_once(kube_context: str) -> None:
    """加载 K8s 凭证（进程内一次）：先 in-cluster，失败回退 kubeconfig。"""
    global _CONFIG_LOADED
    with _CONFIG_LOCK:
        if _CONFIG_LOADED:
            return
        import kubernetes.config  # 懒加载：单测 mock 本模块时不需要安装

        try:
            kubernetes.config.load_incluster_config()
            logger.info("k8s config: in-cluster ServiceAccount")
        except Exception:  # noqa: BLE001
            cfg_file = config.settings.gateway_kubeconfig or None
            kubernetes.config.load_kube_config(
                config_file=cfg_file, context=kube_context or None,
            )
            logger.info(
                "k8s config: kubeconfig%s",
                f" (context={kube_context})" if kube_context else "",
            )
        _CONFIG_LOADED = True


def _reset_config_for_tests() -> None:
    """测试隔离用：清掉进程级加载标记（context 切换场景同样复用）。"""
    global _CONFIG_LOADED
    with _CONFIG_LOCK:
        _CONFIG_LOADED = False


def _is_api_exception(exc: Exception) -> bool:
    """K8s API 异常判定。

    kubernetes>=30 引入类型化子类（NotFoundException/ConflictException...），
    类名不再是 ApiException，因此优先 isinstance；老版本回退按类名判定。
    """
    try:
        from kubernetes.client.exceptions import ApiException

        return isinstance(exc, ApiException)
    except ImportError:
        return type(exc).__name__ == "ApiException"


def _translate(exc: Exception) -> Exception:
    """K8s ApiException → 平台错误码（03 §4.2 映射表）。"""
    status = getattr(exc, "status", None)
    reason = str(getattr(exc, "reason", "") or "")
    if status == 403:
        return GatewayK8sForbiddenError(
            f"K8s 拒绝访问（{reason or 'forbidden'}）",
        )
    if status == 404:
        return GatewayResourceNotFoundError("K8s 资源不存在")
    if status == 409:
        return GatewayConflictError(
            "资源版本冲突（可能 console 或他人并发修改），请重试",
        )
    return GatewayK8sError(f"K8s API 调用失败（HTTP {status}）")


class GatewayK8sClient:
    """单 profile（一个 Higress 环境）的资源读写客户端。"""

    def __init__(self, higress_ns: str, kube_context: str = "") -> None:
        self.ns = higress_ns
        self.kube_context = kube_context or ""
        self._custom = None
        self._core = None
        self._networking = None
        self._wasm_version: str | None = None

    # ------------------------------------------------------------------
    # 底层 API（懒初始化；单测直接替换这两个属性即可 mock）
    # ------------------------------------------------------------------
    def _apis(self):
        _load_kube_config_once(self.kube_context)
        if self._custom is None:
            from kubernetes import client as k8s_client

            api = k8s_client.ApiClient()
            self._custom = k8s_client.CustomObjectsApi(api)
            self._core = k8s_client.CoreV1Api(api)
            self._networking = k8s_client.NetworkingV1Api(api)
        return self._custom, self._core

    def _wasm_ver(self) -> str:
        """解析本集群 WasmPlugin 的 API version（v1alpha1 → v1 探测，缓存）。"""
        if self._wasm_version:
            return self._wasm_version
        custom, _ = self._apis()
        last_exc: Exception | None = None
        for ver in WASMPLUGIN_VERSION_CANDIDATES:
            try:
                custom.list_namespaced_custom_object(
                    WASMPLUGIN_GROUP, ver, self.ns, WASMPLUGIN_PLURAL,
                    limit=1,
                )
                self._wasm_version = ver
                return ver
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        raise GatewayK8sError(
            "WasmPlugin CRD 不可用（extensions.higress.io 无可用版本）",
        ) from last_exc

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if _is_api_exception(exc):
                raise _translate(exc) from exc
            raise GatewayK8sError(
                f"K8s API 调用失败（{type(exc).__name__}）",
            ) from exc

    # ------------------------------------------------------------------
    # 连通性探测（T04）：pods 就绪 + 版本推导（RBAC 只有 pods 只读）
    # ------------------------------------------------------------------
    def connectivity_check(self) -> dict:
        """返回 {connected, controller, gateway, console, higressVersion}。

        console 可能已被收敛停用（T21），absent 不算不健康。
        """
        try:
            pods = self.list_pods()
        except Exception as exc:  # noqa: BLE001
            return {
                "connected": False,
                "controller": "unknown",
                "gateway": "unknown",
                "console": "unknown",
                "higressVersion": None,
                "error": getattr(exc, "message", str(type(exc).__name__)),
            }
        components = _component_status(pods)
        version = _higress_version(pods)
        return {
            "connected": True,
            **components,
            "higressVersion": version,
        }

    def list_pods(self) -> list[dict]:
        _, core = self._apis()
        resp = self._call(core.list_namespaced_pod, namespace=self.ns)
        return [
            {
                "name": p.metadata.name,
                "images": [c.image for c in p.spec.containers],
                "ready": bool(p.status.container_statuses) and all(
                    cs.ready for cs in (p.status.container_statuses or [])
                ),
                "phase": p.status.phase,
            }
            for p in resp.items
        ]

    # ------------------------------------------------------------------
    # McpBridge（MCP 上游注册表）
    # ------------------------------------------------------------------
    def get_mcpbridge(self, name: str = "default") -> dict | None:
        custom, _ = self._apis()
        try:
            return self._call(
                custom.get_namespaced_custom_object,
                MCPBRIDGE_GROUP, MCPBRIDGE_VERSION, self.ns,
                MCPBRIDGE_PLURAL, name,
            )
        except GatewayResourceNotFoundError:
            return None

    def replace_mcpbridge(self, obj: dict) -> dict:
        custom, _ = self._apis()
        return self._call(
            custom.replace_namespaced_custom_object,
            MCPBRIDGE_GROUP, MCPBRIDGE_VERSION, self.ns,
            MCPBRIDGE_PLURAL, obj["metadata"]["name"], obj,
        )

    # ------------------------------------------------------------------
    # WasmPlugin（key-auth / ai-proxy 消费者与 provider）
    # ------------------------------------------------------------------
    def get_wasmplugin(self, name: str) -> dict | None:
        custom, _ = self._apis()
        try:
            return self._call(
                custom.get_namespaced_custom_object,
                WASMPLUGIN_GROUP, self._wasm_ver(), self.ns,
                WASMPLUGIN_PLURAL, name,
            )
        except GatewayResourceNotFoundError:
            return None

    def replace_wasmplugin(self, obj: dict) -> dict:
        custom, _ = self._apis()
        return self._call(
            custom.replace_namespaced_custom_object,
            WASMPLUGIN_GROUP, self._wasm_ver(), self.ns,
            WASMPLUGIN_PLURAL, obj["metadata"]["name"], obj,
        )

    def list_wasmplugins(self) -> list[dict]:
        custom, _ = self._apis()
        resp = self._call(
            custom.list_namespaced_custom_object,
            WASMPLUGIN_GROUP, self._wasm_ver(), self.ns,
            WASMPLUGIN_PLURAL,
        )
        return resp.get("items", [])

    # ------------------------------------------------------------------
    # ConfigMap（ai-route / domain）
    # ------------------------------------------------------------------
    def get_configmap(self, name: str) -> dict | None:
        _, core = self._apis()
        try:
            return self._call(core.read_namespaced_config_map, name, self.ns)
        except GatewayResourceNotFoundError:
            return None

    def upsert_configmap(self, name: str, labels: dict, data: dict) -> dict:
        """存在则更新 data（保留其他键），不存在则创建。"""
        _, core = self._apis()
        existing = self.get_configmap(name)
        if existing is None:
            body = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": self.ns,
                             "labels": labels},
                "data": data,
            }
            return self._call(core.create_namespaced_config_map, self.ns, body)
        existing.data = {**(existing.data or {}), **data}
        if not existing.metadata.labels:
            existing.metadata.labels = {}
        existing.metadata.labels.update(labels)
        return self._call(
            core.replace_namespaced_config_map, name, self.ns, existing,
        )

    def delete_configmap(self, name: str) -> bool:
        _, core = self._apis()
        if self.get_configmap(name) is None:
            return False
        self._call(core.delete_namespaced_config_map, name, self.ns)
        return True

    def list_configmaps_by_label(self, label_key: str, label_value: str) -> list[str]:
        """按 label 列出 ConfigMap 名单（ai-route 反显用）。"""
        _, core = self._apis()
        resp = self._call(
            core.list_namespaced_config_map,
            namespace=self.ns,
            label_selector=f"{label_key}={label_value}",
        )
        return [item.metadata.name for item in resp.items]

    # ------------------------------------------------------------------
    # Ingress（console 翻译产物：路由真正生效的载体，02 基准 §3a）
    # ------------------------------------------------------------------
    def upsert_ingress(self, body: dict) -> dict:
        self._apis()
        net = self._networking
        name = body["metadata"]["name"]
        try:
            self._call(net.read_namespaced_ingress, name, self.ns)
        except GatewayResourceNotFoundError:
            return self._call(net.create_namespaced_ingress, self.ns, body)
        return self._call(
            net.replace_namespaced_ingress, name, self.ns, body,
        )

    def delete_ingress(self, name: str) -> bool:
        self._apis()
        net = self._networking
        try:
            self._call(net.read_namespaced_ingress, name, self.ns)
        except GatewayResourceNotFoundError:
            return False
        self._call(net.delete_namespaced_ingress, name, self.ns)
        return True


# ===========================================================================
# 探测辅助（纯函数，单测直接覆盖）
# ===========================================================================
def _component_status(pods: list[dict]) -> dict:
    """按 pod 名前缀推导组件状态：ready / not_ready / absent。"""
    out = {}
    for comp in ("higress-controller", "higress-gateway", "higress-console"):
        matching = [p for p in pods if p["name"].startswith(comp)]
        if not matching:
            out[comp.removeprefix("higress-")] = "absent"
        elif any(p["ready"] for p in matching):
            out[comp.removeprefix("higress-")] = "ready"
        else:
            out[comp.removeprefix("higress-")] = "not_ready"
    return out


def _higress_version(pods: list[dict]) -> str | None:
    """从 higress-controller 容器镜像 tag 提取版本（higress:2.2.3）。"""
    for p in pods:
        if not p["name"].startswith("higress-controller"):
            continue
        for img in p["images"]:
            if ":" in img and "/" in img:
                tag = img.rsplit(":", 1)[1]
                if tag and tag[0].isdigit():
                    return tag
    return None


def client_for_profile(profile: dict) -> GatewayK8sClient:
    return GatewayK8sClient(
        higress_ns=profile["higress_ns"],
        kube_context=profile.get("kube_context") or "",
    )
