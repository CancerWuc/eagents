# -*- coding: utf-8 -*-
"""Higress 网关配置控制面（higress-mcp-gateway 专题 01/02/03）。

eAgent 作为 Higress 的配置控制面：管理 profile / MCP 服务（McpBridge
registry）/ 路由（ai-route ConfigMap）/ consumer（key-auth WasmPlugin），
四步写序列见 sync.py；不代理网关流量（数据面仍直连 Higress LB）。
"""
