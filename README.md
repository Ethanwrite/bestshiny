# AI Director Platform V1

一个可运行的、以“镜头是状态转换”为核心的 AI 导演工作台。系统把剧本编译为
`Project → Episode → Scene → Shot → Candidate`，只允许通过质量检查的候选结果写入正式时间线。

## 已实现

- Narrative Compiler：场景、对白/动作事件、单一主动作镜头、输入/输出时间线状态与前后镜头链。
- Character Identity：用户上传并确认角色主图；确认版本不可修改，换图会创建 v2/v3。
- Continuity Engine：基于可解释风险向量选择紧接上一镜、尾帧加参考图或重新固定人物/场景。
- Generation Policy：能力矩阵、策略降级、供应商 fallback，并按偏好、可靠性、延迟和已验收成本路由。
- Generation Gateway：数据库幂等、账号 slot、后台 worker、未知付费请求状态保护、重试与恢复事件。
- Candidate + QA：一个镜头可有多个候选；分层文件/身份/摄影/动作检查、硬门槛、漂移与遮挡指标。
- Commit Pipeline：仅 `PASS` 可采用；落时间线快照、提取尾帧、传播下一镜输入状态并结算成本。
- Media：SHA-256 去重、资产血缘、供应商媒体绑定、本地与 S3/R2/MinIO 兼容存储。
- Prompt/Skill：原始提示词矫正差异、事实保持、按场景/人物/摄影/供应商约束编译并记录版本。
- Web：真实三栏导演工作台，支持剧本编译、镜头重生成、候选对比、质量检查、采用、人物换图与
  身份版本锁定、提示词整理、连续性判断；桌面和手机响应式布局。

源码审计与迁移边界见 [docs/source-audit.md](docs/source-audit.md)。

## 一键启动

需要 Docker Desktop 或兼容的 Docker Engine：

```bash
cp .env.example .env
docker compose up --build
```

- 导演工作台：<http://localhost:3000>
- API 文档：<http://localhost:8080/docs>
- MinIO 控制台：<http://localhost:9001>

Compose 包含 Web、FastAPI、后台 worker、PostgreSQL + pgvector、MinIO 及自动建桶任务。

## 本地开发

需要 Python 3.12+、`uv`、FFmpeg：

```bash
cp .env.example .env
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn video_platform_api.main:app --reload --port 8080
```

另一个终端运行：

```bash
uv run video-platform-worker
```

Web 可通过 `python3 -m http.server 18081 --directory apps/web` 本地查看；开发模式 API 地址为
`http://127.0.0.1:18080`，Docker 内由 Nginx 的 `/api` 反向代理访问。

## 核心流程

1. 创建项目和 Episode，录入剧本并调用 `/v1/episodes/{id}/compile`。
2. 创建人物档案，上传 `CHARACTER_MASTER`，由用户确认身份版本。
3. 对镜头调用 `/v1/shots/{id}/generate`；系统编译提示词、解析供应商能力并创建持久化候选/任务。
4. worker 完成供应商调用后下载并登记资产，候选进入质量检查。
5. `/validate` 运行级联 QA；只有 `PASS` 的候选可 `/commit`。
6. Commit 提取尾帧、持久化状态快照、传播下一镜输入并记录实际/浪费/重试成本。

所有生成调用都通过 Gateway；Agent、Web 和业务核心不能直接访问供应商客户端。

## Google Flow 浏览器 Worker

1. Chrome 打开 `chrome://extensions`，启用开发者模式，加载 `apps/browser-worker-extension/`。
2. 通过 `/v1/accounts` 创建 Google Flow 账号记录，并在扩展中填写返回的账号 ID。
3. 用户在浏览器正常登录 Google Flow。扩展只在已登录上下文中执行请求。
4. 需要交互验证时，用户点击扩展中的“Authorize next generation”。

系统不会绕过 CAPTCHA、登录、风控或平台访问控制。需要人工操作时任务进入
`WORKER_NEEDS_USER_ACTION`，不会盲目发起第二次付费请求。

## 质量门禁

```bash
uv run ruff format --check . --exclude references
uv run ruff check . --exclude references
uv run mypy
uv run pytest -q
docker compose config -q
```

当前自动化回归覆盖叙事编译幂等、身份版本不可变、连续性三种策略、能力 fallback/降级、供应商路由、
身份漂移与遮挡、QA 硬失败、候选提交、尾帧链、成本、账号调度、付费幂等与浏览器 worker 恢复。

## 真实限制

- Google Flow 的真实付费端到端需要用户自己的有效账号、项目和主动授权；仓库测试不会消费积分。
- Seedance、官方 Veo、Grok、Omni、Kling、Runway 是能力矩阵与诚实的未配置适配器，不伪装成已接通。
- Narrative Compiler V1 是确定性解析器，不是完整 NLP/LLM 剧本理解；复杂剧本需人工调整镜头。
- pgvector 表与检索接口已建立，V1 embedding 是可复现的轻量向量，不宣称具备语义模型质量。
- QA 支持证据驱动的分层判定；未接入专用视觉模型时，不足的视觉证据会要求人工复核，不会假装通过。
- Web 已支持上传/替换角色主图，但“用图片模型自动生成多个人物候选图”的产品流程尚未接通。
- 平台有 `User/Workspace` 领域表和可选 API Key 防护；完整注册登录、找回密码、组织成员与商业计费尚未完成。
