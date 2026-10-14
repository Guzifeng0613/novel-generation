# 小说续写编辑部

本项目（novel-orchestrator）是一个单用户、本地运行的多 Agent 小说续写编排器。后端状态机负责拆分、总结、章纲审批、分章写作、九维审校、最多三轮定向返工、主 A 仲裁和最终交付；模型只接收明确上下文并返回文本或结构化 JSON，不拥有文件或调度权限。

## 技术栈

- 后端：Python 3.12+、FastAPI、SQLAlchemy 2、Alembic、Pydantic、uvicorn
- 前端：React、TypeScript、Vite 7
- 依赖管理：uv（后端）、npm（前端）
- 存储：SQLite（索引与运行状态）+ 内容寻址的本地 artifact 目录

## 环境

- Windows 10/11 与 PowerShell 7 (`pwsh.exe`)
- Python 3.12+
- Node.js 20.19+ 或 22.12+（Vite 7 的运行要求）
- 演示模式不需要 API key；真实模型模式需要 OpenAI、DeepSeek 二者至少一个 key

## 安装

```powershell
pwsh.exe -File .\scripts\bootstrap.ps1
Copy-Item .env.example .env
```

所有 npm 调用都通过 `npm.cmd`；脚本会拒绝 Python 3.12 以下的解释器。

## 安全演示（假 Provider）

```powershell
pwsh.exe -File .\scripts\start.ps1 -Demo
```

`-Demo` 强制使用程序内置的确定性假 Provider，不发出 OpenAI/DeepSeek 请求，即使后端环境里已有 key 也不会计费。默认把 SQLite、artifact、项目和交付全部隔离到 `data/demo/`；也可显式指定另一个目录：

```powershell
pwsh.exe -File .\scripts\start.ps1 -Demo -DataDir .\data-demo-qa
```

浏览器打开 `http://127.0.0.1:8000`。演示数据不是正式素材，切勿把 `data/demo/` 当成正式项目目录。

## 真实 Provider（显式开启）

密钥只放在根目录 `.env` 或启动后端的服务器进程环境中，不会进入浏览器、SQLite、日志或导出包：

```powershell
$env:OPENAI_API_KEY = "..."       # 或写入 .env
$env:DEEPSEEK_API_KEY = "..."     # 两者可只配一个
pwsh.exe -File .\scripts\start.ps1 -AllowRealApi -DataDir .\data-live
```

`-AllowRealApi` 是启动层的计费授权；脚本在没有任何服务端 key 时会拒绝启动。它不替代每轮预算：创建续写时应填写费用上限，批准章纲时会同时冻结 `max_cost`、`max_tokens`、`max_calls` 与 `max_wall_time`。用真实 API 做冒烟验证时，请使用独立 `-DataDir`、4,000 字最小轮次和明确的小额费用上限。预算达到 80% 会告警，达到 100% 停止派发新调用。

支持 `openai-only`、`deepseek-only` 与 `hybrid-quality` 三种路由预设。默认模型名可通过 `NOVEL_OPENAI_MODEL` 和 `NOVEL_DEEPSEEK_MODEL` 覆盖。不要创建 `VITE_OPENAI_API_KEY`、`VITE_DEEPSEEK_API_KEY` 等前端变量。

兼容 OpenAI/DeepSeek 协议的中转服务可分别通过 `NOVEL_OPENAI_BASE_URL` 和 `NOVEL_DEEPSEEK_BASE_URL` 配置；地址应包含服务要求的 `/v1` 前缀。项目不会把这些服务端配置暴露给浏览器。

开发模式同样要求明确选择 Provider 模式；Vite 页面在 5173，API 在 8000：

```powershell
pwsh.exe -File .\scripts\dev.ps1 -Demo
# 或：pwsh.exe -File .\scripts\dev.ps1 -AllowRealApi -DataDir .\data-live-dev
```

## 生产构建、托管与迁移

`start.ps1` 每次先执行 `python -m alembic upgrade head`，再运行 `npm.cmd --prefix frontend run build`，最后由 FastAPI 在同一个 `127.0.0.1` 地址托管 `frontend/dist` 和 `/api`。确认已有最新构建时可加 `-SkipBuild`；若 `frontend/dist/index.html` 不存在，脚本会拒绝跳过构建。

`NOVEL_HOST` 和 `NOVEL_PORT` 在生产脚本中有效（开发代理固定使用 127.0.0.1:8000）。本项目当前是本机源码分发，不生成 EXE 或容器镜像；可恢复项目包与阅读包由通过终审后的交付页导出。

## 使用流程

1. 在项目书架导入 UTF-8、UTF-16 或 GB18030 编码的 `.txt/.md` 小说。
2. 运行拆分/总结，生成统一的九维故事状态。
3. 新建续写轮次，设置 4,000–150,000 中文字符目标和模型路由。
4. 审阅章纲与 Canon 冲突；批准前不会派发正文写作调用。
5. 监控逐章写作、五组并行审校、定向修订与主编裁决。
6. 全部质量门禁通过后下载正文、QA、记忆快照、阅读包或可恢复项目包。

暂停只阻止新调用，允许在途结果隔离落盘；取消保留部分产物但不提升分支记忆。若调用已经发出而进程在结果持久化前退出，任务会标为 `indeterminate`，重试前必须明确确认可能重复计费。

## 数据与迁移

默认数据目录是 `data/`。SQLite 仅保存索引和运行状态，大型产物保存在内容寻址的 artifact 目录。JSON 是正式事实来源，Markdown/TXT 是可重建视图。

```powershell
python -m alembic upgrade head
```

服务首次启动也会创建缺失表；已有数据的结构升级必须使用 Alembic。若设置 `NOVEL_DATA_DIR`，应让 `NOVEL_DATABASE_URL` 保持未设置，使数据库与其余数据一起隔离；只有明确需要独立数据库路径时才覆盖后者。

## 测试

```powershell
pwsh.exe -File .\scripts\test.ps1
```

默认自动化测试全部使用 mock/假 Provider，不会读取 key 或产生真实费用。真实 Provider 验证通过 `start.ps1 -AllowRealApi` 手动显式开启，并受该续写轮次的批准预算约束；当前仓库没有会自动计费的 live-test 命令。

## 目录

- `backend/app/`：API、状态机、Provider、契约、质量门禁和导出
- `backend/prompts/`：精炼且版本化的运行提示词
- `backend/migrations/`：Alembic 数据库迁移
- `backend/tests/`：pytest 自动化测试（全部使用 mock/假 Provider）
- `frontend/src/`：React 编辑部工作台和项目自定义组件
- `scripts/`：`bootstrap.ps1`、`start.ps1`、`dev.ps1`、`test.ps1` 等引导与启动脚本
- `docs/`：架构与界面设计规格
- `docs/reference-prompts/`：原始 Skill 和续写/总结提示词的只读参考副本
- `data/`：运行时数据（SQLite、artifact、项目与交付），不入库，仅保留 `.gitkeep`
- `alembic.ini`、`pyproject.toml`、`uv.lock`：迁移与后端依赖配置
