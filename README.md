# eAgent GitHub

eAgent 的核心代码重建基线：Vue 管理台、Python/FastAPI 服务注册与治理、
AgentScope ReAct 总控、A2A 1.0、HiAgent 接入、知识库、权限与运行审计。

| 目录 | 内容 |
|---|---|
| `backend/src/eagent_gateway/` | 后端业务模块与运行时 |
| `backend/src/eagent_sdk/` | Python 调用 SDK |
| `backend/contracts/` | API OpenAPI 与运行时 JSON Schema |
| `backend/db/` | 数据库基线和增量迁移 |
| `frontend/src/` | Vue 管理台业务源码 |
| `backend/.deps/` | 当前运行必需的 AgentScope wheel |
| `third-party-licenses/` | 随附运行依赖的许可证 |

## 启动

1. 将 `backend/config.env.example` 复制为 `backend/.env.local`。
2. 设置 `MYSQL_ROOT_PASSWORD`、`EAGENT_DB_PASSWORD`、`EAGENT_ADMIN_TOKEN` 和
   `ADMIN_PASSWORD`。模型与 HiAgent 服务地址/密钥按需设置。
3. 在仓库根目录执行：

```sh
docker compose --env-file backend/.env.local up -d --build
```

管理台默认为 `http://localhost:5174`，使用 `admin` 和自行设置的
`ADMIN_PASSWORD` 登录；后端健康检查为 `http://localhost:8189/healthz`。
已有本地服务占用端口时，修改配置中的 `EAGENT_FRONTEND_PORT` 和
`EAGENT_BACKEND_PORT`，同时调整 `EAGENT_PUBLIC_URL`。

Docker Compose 项目名为 `eagents-github`，容器为 `eagents-github-mysql`、
`eagents-github-backend`、`eagents-github-frontend`；应用镜像为
`eagents-github-backend:local`、`eagents-github-frontend:local` 和
`eagents-github-mysql:5.7`，网络为 `eagents-github-network`。
MySQL 镜像基于官方 `mysql:5.7` 构建，使用独立的 GitHub 镜像名。
数据卷为 `eagents-github-mysql-data`、`eagents-github-kb-data`，数据库与账号
均为 `eagent_github`，与旧版独立。数据库表结构由后端首启自举并执行迁移，
不挂载含固定旧库名的 `schema.sql` 到 MySQL 初始化目录。
前端标题、登录页和导航栏均带 GitHub 标识；代码模块名和 API 协议字段保持兼容。
Compose 启用内嵌性能测试 worker，并等待后端健康后启动前端。

查看状态与停止当前 GitHub 部署（保留数据卷）：

```sh
docker compose --env-file backend/.env.local ps
docker compose --env-file backend/.env.local down
```

`backend/.env.local` 被 Git 忽略，仅保存本机凭据。未配置模型、HiAgent、
Pluto 或 Kubernetes/Higress 时，相应外部功能不可完整使用。

也可在 `backend/` 内通过 `pip install -r requirements.txt` 安装运行依赖，
将配置提供为环境变量后执行 `python -m eagent_gateway.main`。
前端在 `frontend/` 内执行 `pnpm install --frozen-lockfile` 和 `pnpm dev`；
`EAGENT_BACKEND` 可覆盖 Vite 的后端代理地址。

## 重建范围

本基线来自 2026-10-08 的现有工作区，保留当前主线业务代码。
产品内置的评测、性能管理与模拟中心属于现有业务功能，随业务源码保留。
原仓库的测试用例、测试夹具、Mock、联调脚本、历史 Wiki、部署凭据、
数据库/知识库运行数据、构建产物及旧 Git 历史均未导入。
当前没有合入原生动态 Agent Team 功能分支。

HiAgent 地址须显式配置；新基线已去掉原部署的默认服务地址和个人工号。
AgentScope 使用原工作区随附的 `2.0.1` wheel，以保持现有 API 兼容；
其 Apache-2.0 许可证随仓库保留，未经验证不要直接替换依赖版本。
