# -*- coding: utf-8 -*-
"""standalone 部署包生成器（standalone-deployment 01 §7.1，S-06/S-07）。

POST /api/v1/agents/{master_id}:export-standalone 的编排实现：
校验（一期边界）→ 导出 KB 快照（导出即对账）→ 合并导出 card.json
（**剔除 model_config 的 api_key**）→ deploy_digest 三件套联合锁定 →
组装 tar.gz 部署包 → 登记 eag_standalone_export + 审计。

部署包布局（tar 根 = <slug>-standalone/）:
  card.json                     ← 合并导出（prompt/react_config/model_config
                                   去 api_key/kb_bindings/release 元数据）
  kb_snapshot/index.json        ← 多 KB 快照根（§4.1）
  kb_snapshot/<slug>/...        ← manifest/wiki/graph/chunks/sources
  Dockerfile.standalone         ← 与仓库根文件逐字节一致（同步测试锁定）
  deploy/standalone.yaml        ← 已填 slug/image/digest 的部署清单
  .env.example                  ← env 模板（IMAGE_TAG/DEPLOY_DIGEST/密钥占位）
  README.md                     ← 构建/运行指引（build 指引，§7.1）
"""
from __future__ import annotations

import io
import json
import tarfile
import tempfile
from pathlib import Path

from .errors import ConflictError, NotFoundError
from .kb import dao as kb_dao
from .kb import snapshot as kb_snapshot
from .persistence.db import Database, new_id, now
from .runtime import master_service
from .runtime.model_factory import parse_model_config
from .runtime.prompt_middleware import get_published_prompt
from .standalone_card import (
    CARD_FORMAT,
    card_digest_of,
    compute_deploy_digest,
    snapshot_root_hash,
)

# ---------------------------------------------------------------------------
# 部署包内模板（与仓库根 Dockerfile.standalone / deploy/standalone.yaml
# 保持逐字节一致——tests/test_standalone_export.py 同步测试锁定；平台镜像
# 内无仓库文件，导出时以本常量为唯一事实源）
# ---------------------------------------------------------------------------
DOCKERFILE_STANDALONE = """\
# eAgent 独立部署容器（standalone-deployment 01 §6.1，S-04）
# 构建上下文: eagent-manager/
#   docker build -f Dockerfile.standalone \\
#     --build-arg IMAGE_TAG=eagent-standalone:v0.1.0 \\
#     -t eagent-standalone:v0.1.0 .
#   BASE_IMAGE 可换可达镜像源（默认 daocloud；如已有本地基础镜像直接指名）。
#
# 与平台镜像（Dockerfile）的差异：
# - 只带运行时所需（agentscope + fastapi/uvicorn/pydantic/httpx + kb 检索核心），
#   不含管理面路由 / KB 写服务 / k8s client / openpyxl；
# - **不装 MySQL driver（pymysql）与 sqlalchemy**——容器内无任何 DB 路径
#   （retrieval 经 SearchBackend，dao 全部延迟导入且不会触发）；
# - IMAGE_TAG 构建期烘焙为 ENV（deploy_digest 三件套分量，01 §7.1）。
#
# TODO（二期·更轻形态）：CI 预建引擎镜像推 ACR（eagent-standalone-engine:<version>，
#   不可变 tag/digest 锚定），部署包退化为纯配置包（card.json + kb_snapshot/ +
#   deploy yaml + env），本 Dockerfile 仅作自证参考——同引擎版本 N 个 Agent 共享
#   镜像，部署方从 build+run 简化为 run，deploy_digest 镜像分量更硬。
#   一期前提（交付口径须讲明）：构建上下文依赖 eagent-manager 源码树
#   （src/ + pyproject.toml + .deps/agentscope wheel），部署方需拿到与锁定版本
#   对应的源码；无仓库权限的对外交付需随包附带源码树。
ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim
FROM ${BASE_IMAGE}

ARG IMAGE_TAG=eagent-standalone:dev
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \\
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    EAGENT_STANDALONE_IMAGE_TAG=${IMAGE_TAG}

WORKDIR /app

COPY .deps/agentscope-2.0.1-py3-none-any.whl /tmp/
COPY pyproject.toml /app/
COPY src /app/src

RUN pip install /tmp/agentscope-2.0.1-py3-none-any.whl \\
    && pip install "fastapi>=0.115" "uvicorn[standard]>=0.30" \\
       "httpx>=0.27" "pydantic>=2.7" \\
    && pip install --no-deps /app

# 部署包约定挂载点（card / KB 快照只读挂载，§6.1 数据目录）
#   /app/deploy/card.json          ← EAGENT_STANDALONE_CARD_PATH
#   /app/deploy/kb_snapshot/       ← EAGENT_STANDALONE_KB_SNAPSHOT_DIR
VOLUME ["/app/deploy"]

EXPOSE 8080
CMD ["python", "-m", "eagent_gateway.standalone_main"]
"""

DEPLOY_YAML_TEMPLATE = """\
# eAgent 独立部署容器模板（standalone-deployment 01 §6，S-04）
# 用法：export 端点产出的部署包含 card.json + kb_snapshot/ + 本清单；
# 部署方填充 env 后 kubectl apply。
#
# 三件套联合锁定（§7.1）：IMAGE_TAG（构建期烘焙进镜像 ENV）+
# EAGENT_STANDALONE_DEPLOY_DIGEST（启动校验，错配 fail-closed 拒绝启动）。
apiVersion: apps/v1
kind: Deployment
metadata:
  name: eagent-standalone-{slug}      # ← 替换为 Agent slug
  labels:
    app: eagent-standalone
    agent: {slug}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: eagent-standalone
      agent: {slug}
  template:
    metadata:
      labels:
        app: eagent-standalone
        agent: {slug}
    spec:
      containers:
        - name: standalone
          image: {image_tag}                # ← 引擎版本（deploy_digest 分量）
          ports:
            - containerPort: 8080
          env:
            - name: EAGENT_STANDALONE_AGENT_ID
              value: "{master_id}"           # 审计/日志标识
            - name: EAGENT_STANDALONE_CARD_PATH
              value: /app/deploy/card.json
            - name: EAGENT_STANDALONE_KB_SNAPSHOT_DIR
              value: /app/deploy/kb_snapshot
            - name: EAGENT_STANDALONE_DEPLOY_DIGEST
              value: "{deploy_digest}"      # ← export 输出；不符拒绝启动
            # 模型 key 一律 env 注入（不落镜像/快照/card.json，§8 凭据）
            - name: EAGENT_LLM_API_KEY
              valueFrom:
                secretKeyRef:
                  name: eagent-standalone-llm
                  key: api-key
            # card 未声明模型时，EAGENT_LLM_* 整套生效（BASE_URL/MODEL 可加）
            - name: EAGENT_LLM_PROVIDER_NAME
              value: "glm"
            - name: EAGENT_LLM_BASE_URL
              value: "https://open.bigmodel.cn/api/paas/v4"
            - name: EAGENT_LLM_MODEL
              value: "glm-4.7"
          volumeMounts:
            - name: deploy-pkg
              mountPath: /app/deploy
              readOnly: true                # 快照只读挂载（§6.1/§8）
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 2
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 30
      volumes:
        - name: deploy-pkg
          persistentVolumeClaim:
            claimName: eagent-standalone-{slug}   # ← 部署包（card+快照）
            readOnly: true
---
apiVersion: v1
kind: Service
metadata:
  name: eagent-standalone-{slug}
spec:
  selector:
    app: eagent-standalone
    agent: {slug}
  ports:
    - port: 80
      targetPort: 8080
"""


def _env_example(card_digest: str, snap_hash: str, image_tag: str,
                 deploy_digest: str, master_id: str) -> str:
    return f"""\
# eAgent standalone 部署 env 模板（export 产物，按需填充后注入容器）
# 三件套（与 deploy_digest 联合锁定，错配则容器拒绝启动）：
EAGENT_STANDALONE_IMAGE_TAG={image_tag}
EAGENT_STANDALONE_DEPLOY_DIGEST={deploy_digest}
# 人工核对用摘要（引擎运行时对文件实算，不读这两个值）：
#   CARD_DIGEST={card_digest}
#   SNAPSHOT_HASH={snap_hash}
EAGENT_STANDALONE_AGENT_ID={master_id}
EAGENT_STANDALONE_CARD_PATH=/app/deploy/card.json
EAGENT_STANDALONE_KB_SNAPSHOT_DIR=/app/deploy/kb_snapshot

# 模型凭据（不落镜像/快照/card.json；card 声明模型时必填）
EAGENT_LLM_API_KEY=
EAGENT_LLM_PROVIDER_NAME=glm
EAGENT_LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
EAGENT_LLM_MODEL=glm-4.7
"""


def _readme(slug: str, image_tag: str, deploy_digest: str) -> str:
    return f"""\
# {slug} standalone 部署包

由 eAgent 平台 export 生成（standalone-deployment 01 §7）：容器内 =
总控 ReAct + KB 只读快照检索 + 模型调用，不依赖平台进程与 MySQL。

- 引擎镜像 tag：`{image_tag}`（构建期烘焙，deploy_digest 分量）
- deploy_digest：`{deploy_digest}`（card + KB 快照 + 镜像 tag 联合锁定；
  启动校验，错配/篡改 fail-closed 拒绝启动）

## 构建（在 eagent-manager 仓库根，部署包 Dockerfile 与仓库一致）

    docker build -f Dockerfile.standalone \\
      --build-arg IMAGE_TAG={image_tag} \\
      -t {image_tag} .

## 运行（Docker）

    docker run -p 8080:8080 \\
      -v $PWD/card.json:/app/deploy/card.json:ro \\
      -v $PWD/kb_snapshot:/app/deploy/kb_snapshot:ro \\
      -e EAGENT_STANDALONE_CARD_PATH=/app/deploy/card.json \\
      -e EAGENT_STANDALONE_KB_SNAPSHOT_DIR=/app/deploy/kb_snapshot \\
      -e EAGENT_STANDALONE_IMAGE_TAG={image_tag} \\
      -e EAGENT_STANDALONE_DEPLOY_DIGEST={deploy_digest} \\
      -e EAGENT_LLM_API_KEY=<部署方按 card 声明供 key> \\
      {image_tag}

## 运行（K8s）

填好 `.env` 密钥后 `kubectl apply -f deploy/standalone.yaml`
（card.json + kb_snapshot/ 需先放入 PVC，挂载点 /app/deploy）。

## 接口

- `POST /chat`：`{{"message": "...", "context_id": "..."}}`
- `GET /healthz`：健康检查（含 kb_tools/model_from/snapshot_format_version）
"""


# ---------------------------------------------------------------------------
# 导出编排（S-06）
# ---------------------------------------------------------------------------
def export_master_standalone(
    db: Database, *, master_id: str, image_tag: str, user: str = "admin",
) -> dict:
    """校验 → 快照 → card → digest → tar.gz → 登记；返回描述 + 包字节。

    失败语义 fail-closed：master 不存在 404；含 A2A 子服务绑定 409（一期
    边界 §1，不静默降级）；KB 绑定未 READY 409（部署包工具箱不得与平台
    分叉）。返回 {export(card/摘要), package(tar.gz bytes), filename}。
    """
    master = master_service.get_master(db, master_id)
    if not master:
        raise NotFoundError("Agent Team 不存在")
    if not master["enabled"]:
        raise ConflictError("Agent Team 已禁用，不能导出")

    # 一期边界（§1/§7.1）：含 A2A 子服务绑定的 release 拒绝导出（容器内
    # 无 A2ATool 依赖），不做静默降级
    a2a_bindings = db.query_all(
        "SELECT tool_name FROM eag_master_binding"
        " WHERE master_id=:mid AND enabled=1",
        {"mid": master_id},
    )
    if a2a_bindings:
        tools = ", ".join(b["tool_name"] for b in a2a_bindings[:8])
        raise ConflictError(
            f"该 Agent 含 A2A 子服务绑定（{tools}），一期仅支持"
            "「纯 KB + 模型」Agent 的独立部署（多 Agent 捆绑导出见设计 §10）",
        )

    # KB 绑定集（授权边界 §8）：仅启用的 READY 且 current_rev 非空可导出；
    # 绑定了但不可用 → 409（fail-closed，不静默缺工具）
    kb_bindings = kb_dao.kb_bindings_for_target(db, "MASTER", master_id)
    ready = [b for b in kb_bindings
             if b.get("kb_status") == "READY" and b.get("current_rev")]
    not_ready = [b for b in kb_bindings if b not in ready]
    if not_ready:
        raise ConflictError(
            "KB 绑定不可用（未 READY 或无 current_rev），不能导出: "
            + ", ".join(b["slug"] for b in not_ready),
        )

    with tempfile.TemporaryDirectory(prefix="eagent-export-") as tmp:
        tmpdir = Path(tmp)
        snap_root = tmpdir / "kb_snapshot"
        entries = []
        for b in ready:
            kb_snapshot.export_kb_snapshot(
                db, b["kb_id"], snap_root / b["slug"], rev="current",
            )
            entries.append({
                "slug": b["slug"], "dir": b["slug"],
                "kb_id": b["kb_id"], "rev": b["current_rev"],
            })
        kb_snapshot.write_snapshot_index(snap_root, entries)

        # card.json：合并导出（api_key 剔除；prompt 未发布时为 None →
        # 容器 middleware 回退默认种子提示词，与平台 run_master 语义一致）
        prompt_text, prompt_version_id = get_published_prompt(db, master_id)
        model_cfg = parse_model_config(master.get("model_config_json")) or {}
        version = "standalone-" + master["updated_at"].strftime("%Y%m%d%H%M%S")
        card = {
            "card_format": CARD_FORMAT,
            "agent": {
                "master_id": master_id,
                "slug": master["slug"],
                "display_name": master["display_name"],
                "prompt_text": prompt_text,
                "prompt_version_id": prompt_version_id or "",
                "react_config": json.loads(master["react_config_json"] or "{}"),
                "model_config": {
                    k: model_cfg.get(k) or ""
                    for k in ("provider", "base_url", "model")
                },
            },
            "kb_bindings": [b["slug"] for b in ready],
            "release": {
                "master_id": master_id,
                "agent_version": version,
                "image_tag": image_tag,
                "created_by": user,
            },
            "snapshot_format_version":
                kb_snapshot.SNAPSHOT_FORMAT_VERSION,
        }
        card_path = tmpdir / "card.json"
        card_path.write_text(
            json.dumps(card, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )

        card_digest = card_digest_of(card_path)
        snap_hash = snapshot_root_hash(snap_root)
        deploy_digest = compute_deploy_digest(
            card_digest, snap_hash, image_tag,
        )
        package = _build_tarball(
            tmpdir, card_path, snap_root, slug=master["slug"],
            image_tag=image_tag, deploy_digest=deploy_digest,
            card_digest=card_digest, snap_hash=snap_hash,
            master_id=master_id,
        )

    export_row = register_export(
        db, master_id=master_id, agent_version=version,
        image_tag=image_tag, card_digest=card_digest,
        snapshot_hash=snap_hash, deploy_digest=deploy_digest, user=user,
    )
    return {
        "export": export_row,
        "package": package,
        "filename": f"{master['slug']}-standalone-{version}.tar.gz",
    }


def register_export(
    db: Database, *, master_id: str, agent_version: str, image_tag: str,
    card_digest: str, snapshot_hash: str, deploy_digest: str, user: str,
) -> dict:
    """S-07：导出登记（eag_standalone_export）+ 平台审计。"""
    export_id = new_id("sex_")
    db.execute(
        """INSERT INTO eag_standalone_export
             (export_id, master_id, agent_version, image_tag, card_digest,
              snapshot_hash, deploy_digest, state, created_by, created_at)
           VALUES (:eid, :mid, :v, :img, :cd, :sh, :dd, 'EXPORTED', :u, :ts)""",
        {"eid": export_id, "mid": master_id, "v": agent_version,
         "img": image_tag, "cd": card_digest, "sh": snapshot_hash,
         "dd": deploy_digest, "u": user, "ts": now()},
    )
    kb_dao.audit(
        db, user=user, action="standalone.export",
        resource_type="standalone_export", resource_id=export_id,
        detail={"master_id": master_id, "agent_version": agent_version,
                "deploy_digest": deploy_digest, "image_tag": image_tag},
    )
    db.commit()
    return get_export(db, export_id)


def get_export(db: Database, export_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_standalone_export WHERE export_id=:eid",
        {"eid": export_id},
    )


def list_exports(db: Database, master_id: str) -> list[dict]:
    """产物与配置状态关联可查（S-07 验收）。"""
    return db.query_all(
        "SELECT * FROM eag_standalone_export WHERE master_id=:mid"
        " ORDER BY created_at DESC LIMIT 100",
        {"mid": master_id},
    )


# ---------------------------------------------------------------------------
# tar.gz 组装（元数据归零：不落构建机 uid/mtime；字节序按 name 排序）
# ---------------------------------------------------------------------------
def _build_tarball(
    tmpdir: Path, card_path: Path, snap_root: Path, *, slug: str,
    image_tag: str, deploy_digest: str, card_digest: str, snap_hash: str,
    master_id: str,
) -> bytes:
    root = f"{slug}-standalone"
    files: list[tuple[str, Path | bytes]] = [
        (f"{root}/card.json", card_path.read_bytes()),
        (f"{root}/Dockerfile.standalone", DOCKERFILE_STANDALONE.encode("utf-8")),
        (
            f"{root}/deploy/standalone.yaml",
            DEPLOY_YAML_TEMPLATE.format(
                slug=slug, image_tag=image_tag,
                deploy_digest=deploy_digest, master_id=master_id,
            ).encode("utf-8"),
        ),
        (
            f"{root}/.env.example",
            _env_example(
                card_digest, snap_hash, image_tag, deploy_digest, master_id,
            ).encode("utf-8"),
        ),
        (
            f"{root}/README.md",
            _readme(slug, image_tag, deploy_digest).encode("utf-8"),
        ),
    ]
    for p in sorted(snap_root.rglob("*")):
        if p.is_file():
            files.append(
                (f"{root}/kb_snapshot/{p.relative_to(snap_root).as_posix()}",
                 p.read_bytes()),
            )

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tf:
        for name, data in sorted(files, key=lambda t: t[0]):
            payload = data if isinstance(data, bytes) \
                else Path(data).read_bytes()
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()
