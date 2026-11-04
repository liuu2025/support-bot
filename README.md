# 退款客服 · AI 退款处理智能体

一个处理电商退款请求的 AI 客服：智能体查询订单、核对退款政策，解释不符合条件的原因，或准备**待客户确认的退款 / 人工审核申请**。客户点击确认卡片后才执行。

本仓库的本地密钥、数据库密码和运行数据放在被 Git 忽略的 `private/` 目录；安全边界见 [SECURITY.md](SECURITY.md)。

核心设计原则：**模型准备方案，客户明确授权，代码复核执行**。模型没有执行退款的工具；退款记录只有在客户点击确认、后端复核订单与政策快照通过后才写入数据库。确定性政策引擎（`app/policy/engine.py`）持续校验，模型无法绕过确认或退款规则。

超出自动审批上限的申请在客户确认提交后成为**待人工处理的工单**。人工客服通过受权限保护的审核接口查看订单、批准或拒绝，结果写回退款台账（`decided_by=human`）并推送到同一客户页面。

当前主入口为「退款工作台」，辅助入口为「模型设置」。客户身份固定，退款与转人工都必须先通过确认卡片授权。公开安全设计见 [docs/安全设计.md](docs/安全设计.md)。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · SQLAlchemy 2 (async) · Alembic · Postgres |
| 智能体 | LangGraph（`agent` ⇄ `tools` 两节点 ReAct） |
| 缓存/并发 | Redis（限流、会话锁、信号量、每日 token 预算、pub/sub） |
| 前端 | React 18 · Vite · TypeScript · Tailwind |
| 可观测 | Prometheus 指标 · 结构化日志 · 推理事件流（SSE） |

## 目录结构

```
backend/
  app/
    agent/          LangGraph 图、工具、系统提示词、注入检测、执行主循环
    policy/         退款政策引擎（纯函数，无 IO）+ 政策文档
    db/             模型、会话、种子数据
    main.py         HTTP 接口与路由组装
  evals/            黄金评测集（YAML 用例 + 指标 + 报告）
  tests/            单元与集成测试
  alembic/          数据库迁移
frontend/
  src/pages/        Chat（退款任务主页面）、Settings（模型连接）
  src/components/   确认卡片、执行时间线和运行信息等
loadtest/           Locust 压测脚本、不变量检查、本地假模型服务
docs/               安全设计（护栏清单：威胁模型 / 分层防御 / 如何验证）
docker-compose.yml  一键启动整套服务
```

## 本地运行

前置：Docker + Docker Compose。

```bash
docker compose --env-file private/support-bot.env up -d --build   # 首次构建并执行迁移与种子数据
docker compose --env-file private/support-bot.env ps -a          # migrate 服务执行完会退出（Exited 0），属正常
```

启动后：

| 地址 | 用途 |
|---|---|
| http://localhost:7100/chat | 客户对话界面（主入口） |
| http://localhost:7100/settings | 模型设置（填服务地址与 API Key、选择模型） |
| http://localhost:7101/docs | 接口文档 |
| http://localhost:7101/metrics | Prometheus 指标 |

> 本地默认端口是 7100 / 7101，并且只绑定到回环地址。若需要自定义端口，请复制 `docker-compose.override.example.yml` 后显式传入；不要把开发服务直接暴露到公网。

**第一次启动必须先配模型**：这套系统跑的是真实大模型，没有内置的假模型。
打开 http://localhost:7100/settings → 填服务地址、API Key →
点「获取模型」→ 从下拉里选一个 → 保存。保存后立即生效，不用重启容器。

## 模型设置（OpenAI 兼容）

只走一种协议：**OpenAI 兼容**（`POST {base}/chat/completions` + `GET {base}/models`）。
官方 OpenAI、DeepSeek、GLM、硅基流动、以及各种中转站都用同一个协议，因此配置只有三项：
服务地址、API Key、模型。

| 设计 | 说明 |
|---|---|
| 模型不手填 | 点「获取模型」由服务端请求 `{地址}/models`，下拉选择；地址只填域名时自动补 `/v1`，带路径的（如 `/api/paas/v4`）原样保留 |
| 密钥不回显 | API Key 用 Fernet 加密存库，接口只返回 `has_api_key`；再次保存时留空即保持原密钥 |
| 即时生效 | 每次保存 `version + 1`，客户端与 LangGraph 图按版本号重建，多副本在下一轮请求跟上，无需重启 |
| 测试连接 | 真发一次工具调用请求：本项目依赖 function calling，只聊天不调工具的模型直接报错并说明原因 |
| 方案预设 | 常用的地址存为「方案」，一键应用/改名/删除 |
| 地址不设限 | 本地/内网地址照填，不做白名单：这是自托管项目，常见做法就是连本机的 Ollama、局域网推理机、或 compose 网络里的模型容器；能改这个配置的又必须是不带 scope 的管理员。若将来做成多人共享服务，应在此加一层地址白名单 |
| 权限 | 全站唯一一处影响所有人的配置，只认**不带 scope** 的管理员令牌 |

几种常见填法（都能直接粘进「服务地址」）：

| 场景 | 服务地址 |
|---|---|
| 云厂商 | `https://api.deepseek.com`（自动补成 `…/v1`）、`https://open.bigmodel.cn/api/paas/v4` |
| 中转站 | 你拿到的 `https://xxx.com/v1` |
| 本机 Ollama | `http://127.0.0.1:11434`（注意：容器里访问宿主机要用 `http://host.docker.internal:11434`） |
| 局域网推理机 | `http://192.168.1.20:8000/v1` |
| compose 里的模型容器 | `http://模型服务名:端口/v1` |

也可以不用界面，用环境变量做首次引导（`MODEL_BASE_URL` / `MODEL_API_KEY` / `MODEL_NAME`），
界面里保存的值优先。没配置时对话不会报错，而是明确提示"请管理员在模型设置里配置"。

相关接口：

```
GET    /api/admin/model-config                  (管理员) 当前配置（不含密钥）
PUT    /api/admin/model-config                  (管理员) {"base_url","model_name","api_key","timeout_s"}
DELETE /api/admin/model-config                  (管理员) 清除，回退到环境变量
POST   /api/admin/model-config/models           (管理员) {"base_url","api_key"} → 拉取模型列表
POST   /api/admin/model-config/test             (管理员) 测试连接（真实工具调用）
GET/POST/PATCH/DELETE /api/admin/model-config/presets[/{id}]  (管理员) 方案管理
POST   /api/admin/model-config/presets/{id}/apply             (管理员) 应用方案
```

停止与清理：

```bash
docker compose --env-file private/support-bot.env down           # 停止，保留数据库卷
docker compose --env-file private/support-bot.env down -v        # 停止并清空数据库（下次启动会重新灌种子数据）
```

## 本地演示：固定身份、同一页面

打开主页面后，系统为当前浏览器会话准备一位固定客户「林晓」和三笔独立示例订单，不需要选择用户或切换管理角色。接口仍校验 JWT 身份与订单归属；本地初始化只在 `ENV=dev`、`AUTH_MODE=dev` 开放。

| 场景 | 操作 | 预期 |
|---|---|---|
| 正常退款 | 选择订单，先咨询或发起申请 | 咨询只查询；申请生成确认卡片，点「确认退款」后才更新状态 |
| 特价不可退 | 选择特价订单，提出退款诉求 | 解释规则，不显示退款确认按钮 |
| 大额转人工 | 选择大额订单，发起申请 | 点「确认提交人工审核」后创建工单，同页显示等待状态 |

确认卡包含订单号、商品、金额、本次操作与校验原因，有效期 15 分钟。取消、后续提问或切换订单会结束旧申请。确认按钮直接请求后端，不再调用大模型；后端重新核验归属、金额、订单与规则快照，重复确认返回原收据。

刷新或进入模型设置再返回，会恢复当前对话和卡片。「重新体验」创建新的隔离示例订单，保留原有记录，不重置旧客户订单。历史种子客户仍留在数据库中，不进入主界面。

## 工具与授权边界

模型仅有 `get_my_profile`、`list_orders`、`get_order`、`check_refund_eligibility`、`prepare_refund`。原 `issue_refund` 和 `escalate_to_human` 不再提供给模型。

```
客户诉求 → 查询订单 → 校验资格 → 准备申请 → 等待客户确认
                                         ↓
POST /api/me/refund-proposals/{id}/decision
  confirm：后端复核并退款 / 创建人工工单
  cancel：取消本次申请，订单状态不变
```

查询与准备不写退款台账。用户即使在聊天中输入「确认」，也不会授权执行。确认与取消的结果会持久化到同一会话，右侧执行记录展示实际事件。

## 转人工闭环（人机协同）

这是"AI 建议、人决定"的完整链路，不是一句"已转交人工"就结束：

```
客户提问 → 智能体查订单 → 政策判定「可退但超过 500 元」
        → 客户点击「确认提交人工审核」
        → 生成工单（refunds 行: decision=escalated, escalation_status=pending）
        → 有管理权限的人工客服通过审核接口查看订单与诉求
        → 人工客服提交「批准 / 拒绝」（可写备注）
        → 台账改写: decision=approved|denied, decided_by=human, resolved_at/resolved_by/note
        → 客户在对话里实时收到结果（SSE 推送，无需刷新）
```

要点：

| 设计 | 说明 |
|---|---|
| 状态机 | `pending → approved \| denied`，一张工单只能被处理一次（行锁 + 409 兜底） |
| 权限分离 | 工作台只认**不带 scope** 的管理员令牌；演示用的 scoped 令牌只能看自己的工单，不能自己批自己的退款 |
| 钱只有一条路 | 只有客服批准的工单才会把订单标记为已退款，且数据库唯一索引保证一单一退 |
| 幂等 | 重复处理返回 409；被人工拒绝后，智能体不会再把同一订单重新排队 |
| 可观测 | `hitl_escalations_pending`、`hitl_resolutions_total{decision}`、`hitl_resolution_seconds` |

相关接口（角色见括号）：

```
GET  /api/agent/escalations?status=pending   (管理员) 工单队列
GET  /api/agent/escalations/{id}             (管理员) 工单详情 + 客户对话
POST /api/agent/escalations/{id}/resolve     (管理员) {"decision":"approved|denied","note":"..."}
GET  /api/agent/escalations/stats            (管理员) 队列深度与平均处理时长
GET  /api/me/escalations                     (客户)   我自己的工单与进度
GET  /api/me/conversations/{id}/stream       (客户)   会话实时事件（处理结果推送）
```

演示转人工时，客户始终留在退款工作台。创建工单与人工批准是两个步骤，客户无法自行批准自己的退款；人工审核接口继续保留原有管理权限校验。

## 安全设计

威胁模型（提示词注入 / 跑题烧 token / 洪水式 DoS）、16 层防御的逐条说明、参数默认值、
每种触发下用户看到什么、以及如何验证，都写在 **[docs/安全设计.md](docs/安全设计.md)**。
一句话版本：模型只准备申请，客户确认后才由后端复核并执行。模型没有直接执行退款或创建人工工单的工具。

## 当前状态与限制

- 只支持 OpenAI 兼容协议，且**只跑真实大模型**（没有内置假模型/降级模型）：没配置模型时对话会被拒绝并提示去配置。
- 退款只到"记录台账 + 标记订单已退款"：**没有接入支付/退款网关**。
- 转人工已有完整闭环（工单、管理审核接口、结果回传、处理时长统计），但**没有**：客服登录体系（复用管理员角色）、工单分配与认领、超时 SLA 提醒、站外通知（邮件/短信）。
- 模型配置是"单模型"：没有按任务分配不同模型（如小模型判意图、大模型决策），也没有负载均衡/多密钥轮询。
- 会话记忆只保留最近若干条消息，没有跨会话长期记忆。

## 测试与评测

测试和评测都在容器里跑（镜像只装了运行依赖，所以挂载源码并在容器内装开发依赖，不动本机环境）：

```bash
# 单元/集成测试：159 passed, 1 skipped（不需要 API Key：模型用测试替身）
docker compose --env-file private/support-bot.env run --rm --no-deps --user root backend \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"

# 黄金评测集：跑真实模型（需要配置模型服务，会产生调用费用）
docker compose --env-file private/support-bot.env run --rm --no-deps --user root \
  -e MODEL_BASE_URL=https://api.deepseek.com/v1 -e MODEL_API_KEY=replace-with-local-key -e MODEL_NAME=deepseek-chat \
  backend sh -c "pip install -q -r requirements-dev.txt && python -m evals.run --no-opik"
```

评测集覆盖边界、注入、多轮三类场景，指标为确定性判定（金额安全、政策一致、工具正确、无虚假批准），
外加一个 LLM 语气评分。评测脚本没有离线模式——它存在的意义就是衡量真实模型的表现。

