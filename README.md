# AI Director Platform V1

面向 AI 短剧与商业视觉生产的可运行平台。产品提供两种入口，但共用同一套资产、记忆、路由、生成网关、成本与评估基础设施：

- **乘客模式（Passenger Seat）**：用户直接选择图片或视频、模型、时长、清晰度与参考素材；图片提示词可单独整理、撤销，再提交生成。
- **自动导演（Autopilot）**：系统把剧本编译为 `Project → Episode → Scene → Shot → Candidate`，为镜头装配上下文、选择模型并执行质量门禁；内部视频提示词默认收起。

接手开发前必须先读 [文档导航](docs/README.md)、[开发交接](docs/DEVELOPMENT_HANDOFF_2026-08-20.md)、[当前完整架构](CURRENT_ARCHITECTURE.md) 和 [产品需求台账](docs/PRODUCT_REQUIREMENTS_LEDGER.md)。Visual Runtime 的逐阶段实施记录见 [docs/VISUAL_RUNTIME_IMPLEMENTATION.md](docs/VISUAL_RUNTIME_IMPLEMENTATION.md)，源码与 Skill 审计见 [docs/source-audit.md](docs/source-audit.md) 和 [docs/skill-research.md](docs/skill-research.md)。

> **当前 Phase II 工作树仍不可发布。** 它是未提交 WIP；2026-08-21 最新完整测试为 `348 passed, 39 warnings`，Ruff format/lint、Mypy、Node syntax 已全绿。剩余阻断见下文和开发交接更新。下文中的 `205 passed` 是 Phase II 之前稳定 MVP 基线的历史证据。

## 当前已实现

- Commercial Auth：邮箱注册/登录/退出、PBKDF2-SHA256 强哈希密码、只存哈希的有效期会话、工作空间 `OWNER / ADMIN / EDITOR / VIEWER` 权限与项目/素材/生成路由租户隔离。
- Narrative Compiler：场景、对白/动作事件、单一主动作镜头、输入/输出状态与前后镜头链。
- Passenger/Autopilot 共用 `VisualProductionRuntime`、`GenerationGateway`、`MediaRegistry` 与持久化任务，不存在第二套生成引擎。
- Image Prompt Corrector：只服务用户可见的图片提示词，记录原文、修改结果、变化说明与撤销数据；不参与 Autopilot 视频编译。
- Video Prompt Runtime：`CanonicalShotSpec → VideoShotPromptCompiler → ModelAdapter`；Kling、Veo、Seedance、Grok、Wan 分别生成模型专用提示词与 payload。
- Model Registry/Router：视频质量先验保留在版本化 JSON；逻辑模型、provider model ID、信任等级与套餐角色绑定持久化到数据库。路由先执行能力与 `AssetCriticality` 硬门禁，再输出可解释候选、扣分和降级原因；`EDGE` 永远不能处理 canonical/hero/important 资产。
- Model live kill switch：Gateway 在排队前与付费调用边界都核对持久模型身份和 `enabled/live_enabled`，最终检查与 Job CAS 同事务；重启不会覆盖管理员禁用状态，请求 metadata 也无法解锁。
- Asset Registry：逻辑资产与不可变版本覆盖人物、场景、商品、道具等素材；只有显式提升才会改变 canonical 版本，数据库触发器防止跨资产链接、未记录切换和历史改写。
- Memory/Context：L0/L1/L2 记忆、metadata-first 检索、可选 Voyage Multimodal embedding、文本/图片/视频预算装配。
- Evaluation/Retry：结构化质量维度、关键失败、`ACCEPT / RETRY_SAME_MODEL / RETRY_REWRITE_PROMPT / SWITCH_MODEL / REJECT` 与有界重试计划。
- Metrics/Benchmark/Trace：生产指标、基准测试结果与镜头级生产 trace；自适应路由默认关闭。
- Credits admission + lifecycle：所有已认证的公开生成入口都由服务端解析套餐/模型角色/部署可用性/信任/估价；Free 在同一交易中创建 Job、积分预占、CostRecord 和幂等记录。完成时结算，明确的提交前终态会原子退回，跨过付费边界的不确定结果则冻结并进入内部审计对账，不盲退、不盲重试。
- Candidate + QA + Commit：一个镜头可有多个候选；自动证据不足时可由有写权限的真实用户填写理由并显式确认，形成独立审计记录，再单独采用；采用后原子写入唯一正式候选、时间线快照、尾帧与成本记录。
- Timeline v2 + 三镜 Fixture：只允许 COMMITTED source 向未开工 target 传播，并重基下镜 planned output；Autopilot 使用服务端 input/output state ID + hash 围栏，时间线变化后会在创建 Job/预占积分前要求重新规划。离线回归已完整走过 3 Candidates/Jobs/MP4 outputs/QA/commits/end frames/snapshots/accepted costs，但不等于真实 Provider 或真实视觉 QA。
- 不可洗白来源：Provider 参考素材上传只写独立 binding，不覆盖 `MediaAsset` 生成来源；角色 identity、canonical promotion 与 candidate commit 都在终点复核 Provider trust。
- Media：内容字节按 SHA-256 共享，镜头/候选来源链保持独立；供应商媒体上传带并发 claim、租约和付费边界 fencing，本地与 S3/R2/MinIO 存储共用同一注册表。
- Workbench：注册/登录遮罩、自主创作/智能导演双模式、中文画面描述优化、人物主参考 v1/v2 重新提交、场景/产品/道具通用版本上传及指定镜头重做入口；界面只显示通俗说法，内部合同和模型指令默认收起。

## 一键启动（Docker）

> 下列命令是 MVP 的运行方式。`0023` 的 SQLite recovery 阻断已修复，但仍不要直接对现有混合 schema 开发库执行；应先读交接文档、备份并在临时库验证。

需要 Docker Desktop 或兼容的 Docker Engine：

```bash
cp .env.example .env
export CREDENTIAL_ENCRYPTION_KEY="$(python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
export PLATFORM_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
docker compose up --build
```

- 工作台：<http://localhost:3000>
- API 文档：<http://localhost:8080/docs>
- MinIO 控制台：<http://localhost:9001>

Compose 包含 Web、FastAPI、后台 worker、PostgreSQL + pgvector、MinIO 及自动建桶任务。首次启动或升级旧数据库前，应先备份数据库并确认迁移：

```bash
uv run alembic current
uv run alembic upgrade head
```

当前迁移代码链已追加到 `0024_workspace_credit_lifecycle`。其中到 `0020_provider_media_upload_claim` 的迁移曾在 PostgreSQL 17 + pgvector 0.8.6 上验证 fresh install、从 `0012` 升级、JSON embedding 转 `vector(16)` 与 `alembic check`；`0021`–`0024` 尚未做真实 PostgreSQL 验证。2026-08-21 已验证 fresh SQLite、两种历史 recovery snapshot、带数据的 `0023 → 0024 → 0023 → 0024` 往返和 `alembic check`。

默认本地 `data/platform.db` 的 Alembic stamp 仍为 `0020`，同时已有部分 `0021`–`0023` 新表，但 `workspaces` 缺少新列，属于混合 schema。必须先备份和审计，不要手工 stamp 或盲目升级。旧版 `local@ai-director.invalid` 工作空间保持隔离，普通注册不会自动认领；如需转移，必须调用下文说明的受保护内部接口。

## 本地开发

> 当前忽略的 `data/platform.db` 是混合 schema，默认启动可因 `workspaces.plan_tier/credit_balance` 缺列失败。在修复前，只对临时新库运行下列迁移；不要对该开发库盲目 upgrade/stamp。

需要 Python 3.12+、`uv`、FFmpeg：

```bash
cp .env.example .env
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn video_platform_api.main:app --reload --port 18080
```

另一个终端运行 worker：

```bash
uv run video-platform-worker
```

再启动静态 Web：

```bash
python3 -m http.server 18081 --directory apps/web
```

- 本地 Web：<http://127.0.0.1:18081>
- 本地 API 文档：<http://127.0.0.1:18080/docs>

静态开发页默认访问 `http://127.0.0.1:18080`；Docker 中由 Nginx 把 `/api` 转发到 8080。

## 关键环境变量

完整模板见 [.env.example](.env.example)。

| 变量 | 默认值/作用 | 生产建议 |
| --- | --- | --- |
| `DATABASE_URL` | 本地 SQLite；Compose 覆盖为 PostgreSQL | 使用受管数据库并先备份再迁移 |
| `STORAGE_BACKEND` | `local`，也支持 S3 兼容存储 | 配置私有 bucket、最小权限密钥 |
| `MAX_UPLOAD_BYTES` | 单文件默认最多 100 MiB，流式超限即中止并清理临时文件 | 按套餐设置更小上限；项目总配额另行接入计费账本 |
| `PUBLIC_BASE_URL` | `http://localhost:8080` | 设置为实际 HTTPS API 地址 |
| `MODEL_CONFIG_ROOT` | `./config/video-models` | 把模型配置纳入版本发布 |
| `MODEL_INFRASTRUCTURE_CONFIG` | `./config/model-registry/defaults.json` | 版本化发布逻辑模型、信任等级与套餐角色默认绑定；运行时管理员配置存入数据库 |
| `PLATFORM_API_KEY` | 仅供内部/管理员路由的服务密钥；空值时 fail-closed | 必须设置高熵密钥；不可交给浏览器 Worker |
| `DEPLOYMENT_ENVIRONMENT` | 未设置时按 `production` 处理 | 生产保持 `production`；只有本地才用 `development` |
| `AUTH_REQUIRED` | 默认 `true`，要求用户登录 | 生产必须为 `true`；显式关闭会拒绝启动 |
| `AUTH_SESSION_TTL_DAYS` | 默认 30 天 | 根据企业安全策略调整（最多 90 天） |
| `CREDENTIAL_ENCRYPTION_KEY` | 开发态空值使用进程级随机临时密钥 | 生产必须使用高熵 Fernet key，缺失或弱密钥会拒绝启动 |
| `WEB_ORIGINS` | 仅本地开发来源 | 只列出可信 Web Origin |
| `VOYAGE_API_KEY` | 空值不调用 Voyage | 与 `FEATURE_VOYAGE_MEMORY` 一起灰度启用 |
| `VOYAGE_MULTIMODAL_MODEL` | `voyage-multimodal-3.5` | 更换模型前验证 embedding 兼容性 |
| `MEMORY_EMBEDDING_DIMENSION` | `512` | 允许 256/512/1024/2048；变更需重建对应向量 |
| `MEMORY_MAX_*` | 上下文字符、token、图片、视频预算 | 按供应商限制压测后调整 |
| `MAX_AUTO_RETRIES` | `2` | 结合成本上限与人工升级策略设置 |
| `GENERATION_POLL_INTERVAL_SECONDS` | `2` 秒，远程生成任务的轮询间隔 | 过低会对 provider 造成忙轮询；过高会延迟完成检测 |
| `GENERATION_CLAIM_LEASE_SECONDS` | `300` 秒，生成提交/轮询/完成落库的数据库租约 | 必须大于单次轮询与媒体下载超时；不得用于绕过未知付费请求保护 |
| `FLOW_API_BASE/FLOW_API_KEY/FLOW_PROJECT_ID` | Google Flow 运行配置 | 仅使用用户合法账号、项目与主动授权 |

## Feature flags

所有高风险自动化默认关闭。环境变量提供全局默认值，数据库可设置全局或项目级 override；项目级优先。

| Flag | 环境变量 | 打开后的行为 |
| --- | --- | --- |
| `voyage_memory` | `FEATURE_VOYAGE_MEMORY` | Autopilot 自动检索/写入项目记忆；有 Voyage key 时使用远程 embedding，否则使用本地确定性降级 |
| `auto_evaluation` | `FEATURE_AUTO_EVALUATION` | 生成完成后触发结构化评估 |
| `auto_retry` | `FEATURE_AUTO_RETRY` | 根据评估结果执行最多 `MAX_AUTO_RETRIES` 次修复；不会绕过未知付费请求保护 |
| `adaptive_router` | `FEATURE_ADAPTIVE_ROUTER` | 把已有生产指标与 benchmark 调整加入模型评分 |
| `wan3` | `FEATURE_WAN3` | 允许实验性 Wan 配置参与候选；仍要求真实 provider transport 已注册 |

运行时查询与修改：`GET /internal/feature-flags`、`PUT /internal/feature-flags/{name}`。这些接口与业务 API 使用相同的 Bearer API Key 防护。

## 主要 API

API 的完整请求/响应 schema 以 `/docs` 为准。普通用户使用登录会话 token；`/internal/*`与供应商账号管理使用 `PLATFORM_API_KEY`；浏览器 Worker 只使用管理员发行的、绑定 worker/account/provider 的可撤销专用凭证。三者均不可混用。

### 用户与生成

| Method | Path | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/auth/register`、`/api/auth/login` | 创建账号/工作空间或登录 |
| `GET/POST` | `/api/auth/me`、`/api/auth/logout` | 查看当前账号或撤销当前会话 |
| `POST` | `/api/prompt/correct` | 图片提示词整理与可撤销修订 |
| `POST` | `/api/pricing/estimate` | 生成前成本与积分估算 |
| `GET` | `/api/workspaces/{workspace_id}/credits` | 查询余额、冻结额、生命周期聚合与最近事件 |
| `POST` | `/api/passenger/generate` | 乘客模式提交图片/视频任务 |
| `POST` | `/v1/shots/{shot_id}/generate` | 自动导演生成镜头候选 |
| `GET` | `/v1/generations/{job_id}` | 查询生成任务 |
| `POST` | `/api/generations/{job_id}/promote` | 把完成结果新增为资产版本，并可显式提升为 canonical |
| `POST` | `/v1/shots/{shot_id}/candidates/{candidate_id}/human-review` | 对需要人工确认且没有硬失败的候选填写理由并显式确认；仍需另行采用 |

### 资产

| Method | Path | 说明 |
| --- | --- | --- |
| `POST` | `/api/assets` | 创建逻辑资产 |
| `GET` | `/api/projects/{project_id}/assets` | 按项目/类型列资产 |
| `GET` | `/api/assets/{asset_id}` | 查看资产与版本 |
| `POST` | `/api/assets/{asset_id}/versions` | 新增不可变版本 |
| `POST` | `/api/assets/{asset_id}/versions/{version_id}/promote` | 显式切换 canonical 版本 |

### 内部运行时与观测

| Method | Path | 说明 |
| --- | --- | --- |
| `POST` | `/internal/router/video` | 获取可解释的视频模型排名 |
| `POST` | `/internal/memory/index`、`/internal/memory/search` | 写入/检索多层记忆 |
| `GET` | `/internal/memory/characters/{entity_id}`、`/internal/memory/scenes/{scene_id}`、`/internal/memory/state` | 查询人物、场景与当前状态 |
| `POST` | `/internal/evaluate/video`、`/internal/generations/{job_id}/evaluate` | 评估规格或已生成任务 |
| `POST` | `/internal/retry/plan` | 返回有界重试/换模型计划 |
| `POST` | `/internal/models/metrics` | 写入追加式生产指标 |
| `GET/POST` | `/internal/benchmarks`、`/internal/benchmarks/results` | 读取 benchmark 清单/写入结果 |
| `GET` | `/internal/shots/{shot_id}/traces` | 查询镜头生产 trace |
| `POST` | `/internal/workers/credentials` | 管理员发行绑定 worker/account/provider 的专用凭证（明文只返回一次） |
| `POST` | `/internal/workers/credentials/{credential_id}/revoke` | 撤销 Worker 凭证，已连接 WebSocket 也会在下一次循环被关闭 |
| `POST` | `/internal/auth/legacy-workspaces/claim` | 将隔离的 V1 本地工作空间显式转移给已存在用户，要求幂等键并写入审计记录 |
| `POST` | `/internal/provider-media-bindings/{binding_id}/reconcile` | 对结果不确定的供应商素材上传执行“验证已有 ID”或“确认远端未创建”，全程审计且禁止盲目重传 |
| `POST` | `/internal/generations/{job_id}/credit-reconcile` | 仅 `PLATFORM_API_KEY`；对不确定的原预占确认 Provider 已接受或未创建，金额由服务端派生并强制幂等/审计 |
| `POST` | `/internal/provider-budget-reservations/{reservation_id}/reconcile` | 仅 `PLATFORM_API_KEY`；根据账单证据结算或释放 `UNCERTAIN` Provider USD 预算，强制显式确认、证据引用与幂等键 |
| `POST` | `/v1/workers/{worker_id}/socket-ticket` | Worker 用专用 Bearer 凭证换取短期一次性 WebSocket ticket |

## 安全与降级原则

- `PLATFORM_API_KEY` 为空时，内部/管理员 HTTP 路由返回 503；生产环境中的弱 key 也会拒绝启动。Worker 凭证只能注册、心跳、领取命令与回传结果；不能访问内部 QC/管理路由。WebSocket 使用一次性短期 ticket 或 Authorization header，不在 URL query 传密钥。
- 密码经 PBKDF2-SHA256（60 万轮）加随机 salt 存储，会话仅保存 SHA-256 token 哈希并带过期/撤销时间；数据路由按工作空间成员身份与角色校验。
- 普通注册永远只创建新的空工作空间，不会自动认领升级前的本地数据。Legacy 项目默认属于隔离账号且对新用户不可见；只能通过 `PLATFORM_API_KEY` 保护的内部路由、指定已存在的活跃用户并提供幂等键后转移，结果会持久化审计。
- 生产环境缺少合法高熵 `CREDENTIAL_ENCRYPTION_KEY` 时拒绝启动。本地开发的空 key 降级为每进程随机临时 key，重启后旧凭据不可解密，不能用于共享或生产。
- 用户上传仅允许验证过的 PNG/JPEG/WebP/MP4/MOV/WebM；网关在 multipart 解析前和流式接收时限制大小，下载设置 `nosniff` 与安全 Content-Disposition。供应商返回的媒体 URL 只允许 HTTPS/已配置主机，逐跳校验 DNS/重定向并拒绝私网、loopback 与云元数据地址。
- Voyage key 缺失时不会伪造远程调用，而是使用可复现的本地 embedding 以保持流程可测；该降级不具备真正的多模态语义质量，也不能作为身份一致性的唯一裁判。
- 专用视觉评审器缺失或证据不足时，不会伪装成高置信通过；需要人工确认或返回修复/拒绝决策。
- Gateway 对状态未知的付费提交不盲目重发；自动重试使用新幂等键并受次数上限控制。
- 多 worker 通过数据库 CAS claim token 与可过期租约串行化付费提交、供应商轮询和完成落库；跨过付费边界后失联必须先人工对账，不会自动再提交。
- 供应商参考素材首次上传按素材、Provider、账号原子领取；付费边界后的未知结果保持 `NEEDS_RECONCILIATION`，只能通过受保护接口验证远端 ID，或由管理员明确确认远端未创建后再释放重传资格，所有操作写审计记录。
- Google Flow Worker 只在用户已登录且主动授权的浏览器上下文工作，不绕过 CAPTCHA、登录、风控或平台访问控制。
- 模型 Adapter 只负责编译模型专用 prompt/payload，不等于供应商 transport 已接通。

## 质量门禁

```bash
uv run ruff format --check . --exclude references
uv run ruff check . --exclude references
uv run mypy
uv run pytest -q
docker compose config -q
```

### Phase II 之前的稳定基线

截至 2026-08-20，完整测试套件为 **205 passed**。新增回归覆盖注册/登录/会话撤销、工作空间角色与跨租户阻断、生产认证不可关闭、Worker 专用凭证绑定/撤销/过期与一次性 WebSocket ticket、上传主动内容/伪装 MIME/请求大小阻断、媒体下载 SSRF/私网 DNS/重定向/流式限额、受保护媒体访问、中文图片描述事实保持/撤销、模型路由、五种视频 Adapter、canonical 资产与数据库不变量、人工复核审计、记忆检索与预算、评估/重试、指标/benchmark、Passenger/Autopilot 共用运行时，以及多 worker 提交/轮询/完成竞态、候选正式采纳竞态、成本记录幂等、相同字节独立来源链、供应商媒体并发上传/人工恢复、取消/重启容量释放、历史预约迁移与终态不可回退。

真实浏览器已在 1440px、1024px 与 390px 验证注册、退出/重新登录、双模式切换、中文画面描述优化/恢复原文、人物主参考 v1→v2、场景资产 v1→v2 与显式正式版切换；三个断点无横向溢出，控制台无 error/warning。

### 当前 Phase II WIP 门禁

最新完整结果是 **348 passed, 39 warnings**。Ruff format/lint（209 files）、Mypy（117 source files）、JS syntax、fresh SQLite migration、historical/populated recovery 和 Alembic check 通过。完整证据见 [开发交接](docs/DEVELOPMENT_HANDOFF_2026-08-20.md)。

## 尚未完成或需要真实环境验证

- Seedance、官方 Veo、Grok、Omni、Kling、Runway/Wan 目前主要是能力配置、Adapter 或诚实的未配置 provider slot；不能把 payload 编译测试等同于真实供应商端到端。
- Voyage Multimodal 的客户端和安全降级已实现，但没有 key 时测试只验证本地 provider；上线前需做真实配额、延迟、错误码与数据合规验证。
- 生成积分的 Reserve → Generate → Settle / Refund → Reconcile 已闭环；RunAPI `UNCERTAIN` USD 预算也已有强证据的内部人工对账。未完成的是充值/购买、grant 周期、过期、管理员调账，以及 Provider invoice/USD 的自动采集和独立验真。`CostRecord` 不是工作空间钱包事实源。50 积分与默认 8 秒 Seedance 约 87 积分的矛盾仍待产品决策。
- Google Flow 仍需首次自动创建/固定账号与远端 Project、限制迁移及审计；当前 schema 未强制每个本地项目/provider 唯一 READY binding，也未禁止远端 Project 被多个本地项目复用，因此不可开启 Flow 商用 live。
- 用户会话 token 目前保存在浏览器 `sessionStorage`，尚未迁移为 `HttpOnly`/`Secure` cookie；公网发布前还需配套 CSRF、设备会话和安全审计策略。
- Web 已接通生成结果确认、选择已有/新建逻辑资产、人物/场景/产品修改图重新提交与显式 canonical 提升；完整版本对比和 promotion 审计历史的专用页仍待完善。
- 基础注册/登录、会话撤销与工作空间 RBAC 已完成；邮箱验证、找回密码、MFA、成员邀请/移除管理页、登录限流、异常风控、设备会话管理与完整安全审计事件仍需在公网商用前完成。
- 单文件上传有流式限制，但项目级总存储配额尚未接入套餐或计费账本。
- Adaptive router 需要真实样本与 benchmark 结果才能超过静态先验；低样本期应保持 feature flag 关闭并人工审阅。
