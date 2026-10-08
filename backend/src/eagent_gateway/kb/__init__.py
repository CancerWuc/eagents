# -*- coding: utf-8 -*-
"""KB 知识库模块（from_llm_wiki 01–05）。

分层（写读物理分离 D6/L1）:
- kb.dao / kb.service      管理（写）面：CRUD、上传、编译触发、规划确认
- kb.compile / kb.tasks    编译服务：任务执行器 + 七段管线 + Rev 发布
- kb.planner               非对话式入库规划服务（只读分析 → DRAFT）
- kb.retrieval / kb.runtime 只读检索 Runtime（/internal/kb/*，无写端点）
- kb.parser/chunker/embedder/vectorstore/generator  可插拔 Provider 引擎
"""
