# -*- coding: utf-8 -*-
"""KB Embedding Provider（03 §3.1 embed 段 + §6 S8 工程纪律）。

- BuiltinHashEmbedder：内置确定性 embedding（bigram 词袋哈希 + L2 归一化），
  无外网依赖；dev/测试/离线部署默认，语义局部性足够支撑检索链路验证。
- OpenAICompatEmbedder：openai-compatible /embeddings 端点（生产）。
- S8 纪律（批量/信号量/重试/缓存）由 compile.embed_stage 承担，本模块只算向量。
"""
from __future__ import annotations

import hashlib
import logging
import math

from ..config import settings

logger = logging.getLogger("eagent.kb.embed")


class EmbedError(RuntimeError):
    pass


def _bigrams(tokens: list[str]) -> list[str]:
    out = []
    for i in range(len(tokens) - 1):
        out.append(tokens[i] + tokens[i + 1])
    return out


def tokenize_for_embed(text: str) -> list[str]:
    """CJK 单字 + bigram + ASCII 词（小写）。"""
    cjk = [ch for ch in text if "\u4e00" <= ch <= "\u9fff"]
    words = []
    cur = []
    for ch in text.lower():
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            cur.append(ch)
        else:
            if cur:
                words.append("".join(cur))
                cur = []
    if cur:
        words.append("".join(cur))
    return cjk + _bigrams(cjk) + words


class BuiltinHashEmbedder:
    """确定性哈希词袋向量：同义文本余弦相似度高，跨语言/无关文本接近正交。"""

    name = "builtin-hash"

    def __init__(self, dimension: int = 512):
        self.dimension = dimension
        self._salt = "eagent-kb-embed-v1"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        tokens = tokenize_for_embed(text)
        for tok in tokens:
            h = hashlib.sha256((self._salt + tok).encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % self.dimension
            sign = 1.0 if h[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class OpenAICompatEmbedder:
    """openai 兼容 /embeddings（SSRF 防护 transport 出站）。"""

    def __init__(self, *, base_url: str, api_key: str, model: str,
                 dimension: int | None = None, timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimension = dimension
        self.timeout_s = timeout_s

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from ..security.pinned_http import guarded_async_client

        payload: dict = {"model": self.model, "input": texts}
        if self.dimension:
            payload["dimensions"] = self.dimension
        async with guarded_async_client(timeout=self.timeout_s) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        if resp.status_code != 200:
            raise EmbedError(
                f"embedding 端点返回 {resp.status_code}",
            )
        data = resp.json().get("data") or []
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        vectors = [item["embedding"] for item in ordered]
        if len(vectors) != len(texts):
            raise EmbedError("embedding 返回条数与输入不一致")
        return vectors


def build_embedder(embedding_config: dict):
    """按 KB embedding_config（+全局 env 缺省）构造 embedder。

    优先级（部署只设环境变量即可，见 07 §5.4）:
      model    = KB.embedding_config.model > EAGENT_KB_EMBED_MODEL
      endpoint = KB.embedding_config.endpoint > EAGENT_KB_EMBED_BASE_URL
      key      = EAGENT_KB_EMBED_API_KEY（仅环境变量，不落库）
    规则: model 为空或 builtin-hash 开头 → 内置确定性 embedder；
    否则需要 endpoint+key，缺任一 → 回退内置（记日志，fail-safe）。
    """
    model = str(embedding_config.get("model") or settings.kb_embed_model or "")
    if not model or model.startswith("builtin-hash"):
        dim = embedding_config.get("dimension") or 512
        return BuiltinHashEmbedder(int(dim))
    endpoint = embedding_config.get("endpoint") or settings.kb_embed_base_url
    key = settings.kb_embed_api_key
    if endpoint and key:
        return OpenAICompatEmbedder(
            base_url=str(endpoint), api_key=key, model=model,
            dimension=embedding_config.get("dimension"),
        )
    logger.warning(
        "KB 配置了远端 embedding model=%s 但缺 endpoint/key"
        "（env EAGENT_KB_EMBED_BASE_URL / EAGENT_KB_EMBED_API_KEY），回退内置", model,
    )
    return BuiltinHashEmbedder(int(embedding_config.get("dimension") or 512))
