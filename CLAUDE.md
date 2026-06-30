# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

`smart_talkflow` 是**业务无关的 Agent 编排平台**:把用户的一句自然语言,翻译成对已有传统业务系统(OA/ERP/CRM)的确定工作流调用,并在每一步留下可追溯的执行痕迹。平台**绝不复制业务数据**——员工/部门/邮箱等主数据归各传统系统所有,平台只负责「编排、执行、审计」。

设计上的贯穿示例是**员工入职**;**当前注册的示例流程是「会议室预订」**(`MeetingRoomBookingWorkflow`:提交→审批→更新使用状态)。项目按四阶段演进,**当前处于阶段一(MVP)**。完整设计见 `SSD/`(`工作流引擎设计方案.md` 是 generator 引擎的现行总纲)。

## 运行环境与命令(关键)

### 包根是 `src/`,从项目根运行

运行时包路径根是 `src/`:代码内 import 一律**无 `src.` 前缀**。**始终在项目根目录运行,设 `PYTHONPATH=src`**:

```bash
PYTHONPATH=src python -m infra.database        # 以模块方式运行任意代码
PYTHONPATH=src python -c "import conf; print(conf.settings.llm_model)"
```

### 常用命令

```bash
uv sync                                            # 安装/同步依赖(用 uv 管理虚拟环境)
docker compose up -d                               # 起 MySQL 8 + Redis 7(首次启动自动执行 db/*.sql 建表)
docker compose down -v && docker compose up -d     # 改了 SQL 后必须清卷重建

# 跑服务(入口 src/main.py):
PYTHONPATH=src uv run uvicorn main:app --port 8000 --reload

# 测试(标准库 unittest,异步用例继承 IsolatedAsyncioTestCase):
PYTHONPATH=src python -m unittest discover -s tests                          # 全部
PYTHONPATH=src python -m unittest tests.test_meeting_room_workflow          # 单个模块
PYTHONPATH=src python -m unittest tests.test_dispatcher_e2e -v              # 端到端链路(需 MySQL)
```

## 配置(`src/conf/config.py`)

用 `pydantic-settings`,`Settings` 在模块导入时实例化为单例 `settings`。**必填项缺失会在 `import conf` 时直接抛错**——「启动即失败」。

- **LLM**:`LLM_PROVIDER`(`openai`/`anthropic`)+ `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_TIMEOUT`;**意图/回复两套模型**——意图理解 `INTENT_LLM_NAME`/`INTENT_LLM_TEMPERATURE`/`INTENT_LLM_MAX_TOKENS`,回复生成 `REPLY_LLM_NAME`(留空回退 `INTENT_LLM_NAME`)/`REPLY_LLM_TEMPERATURE`/`REPLY_LLM_MAX_TOKENS`。`_required_non_blank` 只强校验意图理解那套 + provider/base_url/api_key/timeout。
- **MySQL**:`MYSQL_DATABASE`/`MYSQL_USER`/`MYSQL_PASSWORD`;`settings.mysql_conf` 给出 `mysql+asyncmy://...`。
- **下游 OA**:`OA_BASE_URL`/`OA_API_KEY`/`OA_DELEGATION_SECRET`(服务账号 + operator 代签 HMAC)。
- **认证**:`AUTH_DEV_MODE`(默认 `True` 开发态信任请求头;`False` 走 SSO/JWT)。
- **崩溃恢复**:`process_heartbeat_timeout`(无心跳超时秒,判崩溃)+ `process_recovery_interval`(watchdog 扫描间隔)。~~`process_max_attempts`~~ 已废弃(不重跑,无 attempt)。
- **提示词仓库(可选)**:`IS_GIT_REPO`/`GIT_REPO_URL`/`GIT_BRANCH`/`GIT_INTENT_RELATIVE_PATH`/`GIT_REPLY_RELATIVE_PATH`(远程按 intent/reply 分流路径;关闭时用 `.prompt/custom/` 本地文件 + 内置默认降级)。

## 架构总览

分层 + 渐进解耦。请求级对象每请求构建、用完即弃;基础设施是进程级单例。

| 层 | 目录 | 职责 |
|---|---|---|
| 配置 | `conf/` | pydantic-settings 单例 |
| 入口 | `main.py` | FastAPI + lifespan |
| LLM 引擎 | `engine/` | 厂商无关 LLM 流式抽象(`base_client` 协议)+ 对话编排器(`query.Query`:意图/回复两段 + 并发执行) |
| 提示词 | `prompts/` | 阶段化系统提示词:来源选择 + 运行时拼装(`PromptType`:intent/reply) |
| 认证 | `security/` | SSO JWT(RS256)验签 |
| API | `api/` | `/chat` SSE 流式 |
| **编排** | `orchestrator/` | **generator 引擎**(`workflow_engine`)+ workflow 抽象(`base`)+ 执行编排(`dispatcher`)+ 失联降级(`downgrade`)+ 幂等(`idempotency`)+ 具体 workflow |
| 运行时 | `runtime/` | 请求级上下文 + 装配工厂 + SSE + 心跳看门狗 |
| 适配器 | `adapters/` | 封装下游 HTTP(`step_call` 转 `StepResult`) |
| 服务 | `services/` | 代签凭证 |
| 权限 | `permission/` | RBAC(`workflow_role` 表) |
| 数据访问 | `repository/` | ORM + process/step tracker |
| 基础设施 | `infra/` | DB/HTTP/日志/异常/Redis |

### 编排层(`orchestrator/`)—— generator 引擎(核心)

**workflow_engine.py**:generator-based 工作流引擎。业务 workflow 用**同步 generator** 声明步骤(`yield step(...)`),引擎 `drive` 驱动。**全链路流式**:`drive` / `BaseWorkflow.execute` / `dispatcher.execute` 都是 `AsyncGenerator`,逐步 `yield StepResult`、末尾 `yield WorkflowResult`。

- **`Compensate(Exception)`**:引擎↔业务补偿协议。drive 失败时 `gen.throw(Compensate)`,业务 `except Compensate` yield 补偿步。
- **`Step`**(dataclass:`func`/`args`/`kwargs`/`name`):`func(process_ctx, *args, **kwargs)→StepResult`(adapter 层已转好),引擎**不 import `AdapterResponse`**。
- **`step(func, *args, name="", **kwargs)`**:yield 工厂。
- **`StepResult`**(frozen dataclass:`ok`/`data`/`error`/`name`/`step_id`/`is_compensation`):引擎与副作用的唯一契约;`is_compensation` 标记补偿步(drive 正常步为 False,`compensate` 用 `replace(..., is_compensation=True)` 给补偿步补 True)。
- **`ProcessContext`**(dataclass:`process_id`/`step_id`):单次 process 的**执行级追踪上下文,显式线程穿透** drive/compensate/`_exec_step`/`Step.execute`(取代用 ContextVar 持有 step_id)。`_exec_step` 每步把新建的 `step_id` 写回 `process_ctx.step_id`,供该步内 adapter 落 `AdapterCallLog` 关联。
- **`drive(workflow, arguments, process_ctx)→AsyncGenerator[StepResult|WorkflowResult]`**:gen=create;循环 `gen.send(result.data)` 回灌闭包;每步 `yield result`;`not ok` → 转 `compensate`(throw);StopIteration → 末尾 `yield WorkflowResult(output=...)`;未捕获异常 → 末尾 `yield WorkflowResult(is_error=True)`。
- **`_exec_step(step, step_no, process_ctx)→StepResult`**:flush_heartbeat → `_step_meta`(反射推断 adapter/action/input_params,**跳过首参 process_ctx**)→ create_step → `process_ctx.step_id=step_id` → `step.execute(process_ctx)` → finish_step(每步存 result_data)。异常归一为 `StepResult(ok=False)`。
- **`compensate(gen, step_results, fail_step_result, process_ctx)→AsyncGenerator`**:`gen.throw(Compensate)` 触发 create 补偿分支,`_exec_step` 驱动补偿步并 `yield`(`is_compensation=True`),直到 StopIteration 末尾 `yield WorkflowResult(is_error=True, compensated=True)`;create 无 `except Compensate` → 直接失败。
- **`replay_steps(workflow, arguments, process_id)→(step_results, fail_step, generator)`**:重建 generator → DB `list_completed_steps` 取 result_data → `gen.send` 回灌闭包到失败点(`fail_step=None` 表示当初全成功)。

**base.py**:`BaseWorkflow`(abstract `create(arguments)→Generator[Step, Any, str]` + abstract `business_key(arguments)→str | None` 流程级幂等键;`execute(arguments, process_id)` 是 **async generator**,局部 import `drive` + 构造 `ProcessContext(process_id=process_id)` 委托)+ `WorkflowRegistry` + `WorkflowResult`(`output`/`is_error`/`metadata`)。

**dispatcher.py**:`execute(workflow, inputs)` 是 **async generator**:认证(`get_operator`)→ 权限(`is_allowed`)→ 幂等(`checker.check(IdempotencyCheckRequest(...))`,命中终态直接 `yield WorkflowResult` 短路)→ `async for result in workflow.execute(inputs, process_id): yield result`(透传 StepResult + 末尾 WorkflowResult)→ `_finalize`(按末尾 `WorkflowResult.is_error` 调 `checker.completed/failed`)。**operator 在 ContextVar**,execute 不接 context。

**downgrade.py**:`handle_processes(registry)`——detect(超时)→ acquire_recovery_lock(抢权)→ 重建 `RequestContext`(operator/trace_id)+ `set_request_context` → replay_steps → `fail_step is not None` 时 `compensate(generator, ..., ProcessContext(process_id))`(throw 补偿,`async for` 消费靠副作用)→ transition_status(failed;`fail_step is None` 则 completed)。**不重跑 adapter**(replay 用 DB result_data 回灌,零下游副作用)。

**idempotency.py**:`Status(StrEnum: RUNNING/COMPLETED/FAILED)` + `IdempotencyCheckRequest`(business_key/input_params/trace_id/created_by/operator_context)+ `IdempotencyChecked`(process + is_new + error/message)+ `IdempotencyChecker`(check/completed/failed)。`check()` 返 `is_new=True`(首次放行)或按 status 短路(completed→历史结果;failed→失败;running/非终态→拒绝)。**无 Action 枚举、无 reject、无 attempt 计数、无 _FROM_* 集合**。

**workflow/meeting_room.py**:`business_key` = `operator.user_id_room_id_起止时间`;`create(arguments)` —— `try: booking_id = yield step(submit, ...); yield step(approve, booking_id=...); yield step(update, ...); return "已预订"` / `except Compensate: if booking_id: yield step(cancel, booking_id); return "已取消"`。

### 引擎层(`engine/`)—— 对话编排 + 厂商无关 LLM 流式抽象

- **`client/base_client.py`**:`SupportsStreamingMessages` 协议(唯一 LLM 接口,`stream_message(ApiMessageRequest)→AsyncGenerator[ApiTextDeltaEvent | ApiMessageCompleteEvent]`)+ 统一入参 `ApiMessageRequest`(model/messages/system_prompt/max_tokens/tools)。`client/llm_client.py` 提供 `OpenAIClient`/`AnthropicApiClient` 两实现,屏蔽 SDK 差异;`client/messages.py` 定义 `ConversationMessage`/`TextBlock`/`ToolUseBlock`/`ToolResultBlock`。
- **`stream_event.py`**:对外流式事件协议 `StreamEvent = AssistantTextDelta | ToolExecutionStarted | ToolExecutionCompleted | ToolProgress | AssistantTurnComplete`。`ToolProgress` 是**步级进度**(step_name/step_id/is_compensation/error),把 drive 每一步的 StepResult 透传到前端。
- **`query.py`** `Query.run(context)→AsyncGenerator[StreamEvent]`:**两段式 LLM 编排**——① 意图理解(`_stream_chat` 传 `tools=registry.to_api_schema()`,LLM 以 function-calling 返回 `tool_use`)→ ② 校验(`get_workflow` + `input_model.model_validate`,未知工具/参数错误直接造 error `ToolResultBlock`)→ ③ **并发执行**(`asyncio.create_task` 每个 workflow + `Queue` fan-in,`_stream_process_step` 透传 `ToolProgress`、收集 `ToolExecutionCompleted` 对应的 tool_result)→ ④ 回复生成(`_stream_chat` 不传 tools,基于 tool_result 流式生成最终回复)。**支持一次对话并发触发多个 workflow**。

### 适配器(`adapters/`)

- `base.py`:`BaseAdapter._call_action(request)→AdapterResponse`(HTTP + 代签 + 日志,**不落库**);`step_call(process_ctx, request)→StepResult`(**resp = _call_action → 落 AdapterCallLog(关联 process_id/step_id)→ 转 StepResult**)。**落库在 step_call**,不在 _call_action。adapter action(`submit_booking` 等)签名 `async def action(self, process_ctx, *biz_args)→StepResult`(`process_ctx` 由 `Step.execute` 注入,转交 `step_call`)。
- `oa_adapter/oa_base.py`:`OAAdapter`(yudao 协议:`is_success` 判 `code==0`;`extract_result` 取 `data`)。
- `oa_adapter/oa_meeting_room.py`:4 个 action 返 `StepResult`(`step_call` 内转)。

### 提示词(`prompts/`)

分层:`system_prompt.py`(来源层)+ `context.py`(运行时入口)+ `environment.py`(环境信息)+ `__init__.py`(公共出口)。

- **`PromptType`(StrEnum)**:`INTENT`/`REPLY`,贯穿所有提示词接口(**勿用裸字符串** `"intent"`/`"reply"`)。
- **`system_prompt.py`**:`get_base_system_prompt(prompt_type)`(内置默认模板,**业务无关**)+ `build_system_prompt(prompt_type, env, *, custom_prompt=None)`(**仅来源选择**:远程仓库 > custom_prompt > 默认,三级降级)+ `_resolve_remote_path`(远程路径按阶段分流)。
- **`context.py`**:`build_runtime_system_prompt(prompt_type, *, env)`(**运行时主入口**):内联加载 `.prompt/custom/<type>_system_prompt.md` 作为 custom_prompt → 委托 `build_system_prompt`。
- **`__init__.py`**:`PromptType`/`EnvironmentInfo`/`build_runtime_system_prompt`/`build_system_prompt`/`get_base_system_prompt` 统一导出。

### 运行时(`runtime/`)

- `context.py`:`OperatorContext`(user_id/roles/tenant_id/name,可 `to/from_operator_context` 序列化供降级重建)+ `ModelContext`(provider/model/temperature/max_tokens)+ `RequestContext`(operator + intent_model/reply_model + api_client + intent/reply_system_prompt + messages + workflow_registry + trace_id)+ ContextVar。访问器仅 `get_request_context()`/`get_operator()`(**无 process_id/step_id 访问器**——执行级追踪走 `ProcessContext` 显式穿透,不进 ContextVar)。
- `runner.py`:`build_runtime(RuntimeBundle)`(启动装配:init_engine/init_redis + 注册 workflow + 构 dispatcher + 构 `Query` + 按 provider 选 LLM 客户端)+ `Runtime.run(operator, user_input)→AsyncIterator[str]`(每请求:注入系统提示词 `build_runtime_system_prompt(INTENT/REPLY, env=...)` → 构 `RequestContext` + `set_request_context` → `Query.run` → **把每个 `StreamEvent` 序列化成 SSE `data: {json}\n\n`**)。router 直接 `StreamingResponse(runtime.run(...))`;SSE 事件类型见 runner 内 `*SseEvent` 模型(text/tool_started/tool_progress/tool_completed/turn_complete/unknown 兜底)。
- `heartbeat.py`:`heartbeat_watchdog(registry)` —— 周期 `await handle_processes(registry)`。

### 数据访问(`repository/`)

- `process_tracker.py`:`acquire_or_create` + `transition_status(process_id, target, from_status: str, extra)`(**单前驱 str**,非集合) + `flush_heartbeat` + `detect` + `acquire_recovery_lock`。**无 reset_process、无 increment_attempt**。
- `step_tracker.py`:`create_step` + `finish_step(result_data=...)`(每步存) + `update_compensation` + `list_completed_steps`。

## 需要读多文件才能理解的设计

### 1. generator 驱动 + 闭包补偿(核心)

业务 `create(arguments)` 是同步 generator:`booking_id = yield step(submit, ...); yield step(approve, booking_id=booking_id)`。引擎 `drive` 用 `gen.send(result.data)` 回灌闭包变量(booking_id)。失败时 `gen.throw(Compensate)` 在 yield 处抛 → 业务 `except Compensate` 捕获 → `yield step(cancel, booking_id)` 补偿。**补偿步用闭包变量(booking_id),无需 yields/RollbackResults/命名产出**。

### 2. AdapterResponse→StepResult 转换(adapter 层)

adapter action 返回 `StepResult`(`step_call` 内 `_call_action → AdapterResponse → StepResult`)。**引擎从不 import `AdapterResponse`**——引擎只认 `StepResult`(分层解耦)。

### 3. 降级 replay(不重跑)

失联恢复用 `replay_steps`:重建 generator → DB `list_completed_steps` 取 `result_data` → `gen.send` 回灌闭包变量到失败点 → `compensate` throw 补偿。**不重调 adapter**(零下游副作用)。

### 4. 幂等(`Status(StrEnum)` + `is_new`)

`check()` 返 `IdempotencyChecked`(process + is_new)。dispatcher:is_new=True → 放行;否则按 status 短路(completed→历史结果;failed→返回失败;running/非终态→拒绝)。`completed/failed` 用 `transition_status(target, from_status="running")`。

### 5. 代签凭证(服务账号 + operator HMAC)

adapter `_call_action` 经 `credential_provider` 取服务账号 api-key + operator 代签头(HMAC-SHA256)。下游 yudao `AgentDelegationFilter` 校验。`utils/api_key_util.py` 生成 api-key + SHA-256 哈希。

### 6. 全链路 Trace ID

`trace_id_context: ContextVar` + `new_trace_id()`。日志 / DB 记录 / 对外 HTTP 头 `X-Trace-Id` 统一串联。

### 7. DB 会话约定(`infra/database.py`)

`async with db_session() as session:` 正常退出 commit,异常 rollback 重抛。`expire_on_commit=False` + `autoflush=False`(**写库手动 flush**)。

### 8. 提示词加载链路(阶段分流 + 三级降级)

**workflow 清单不进 system prompt**:`engine/query.py` 意图理解阶段用 `tools=registry.to_api_schema()` 以 function-calling 形式把所有 workflow 声明给 LLM;reply 阶段 `tools=None`。因此系统提示词**业务无关、不枚举 workflow**,只承载意图判断规则与回复规范。

**运行时拼装链路**:`Runtime.run` → `build_runtime_system_prompt(prompt_type, env=...)`(`prompts/context.py`)→ 内联读 `.prompt/custom/<prompt_type>_system_prompt.md`(缺失/空白则 custom=None)→ `build_system_prompt(prompt_type, env, custom_prompt=...)` → 三级降级:**远程 git 仓库 > 本地 custom > 内置默认**(`get_base_system_prompt`)。

**`.prompt/` 目录**:`custom/`(本地自定义提示词,运行时可编辑、每请求加载) + `remote/`(远程仓库克隆缓存;`is_git_repo=True` 时由 `_fetch_remote_prompt` 浅克隆/强制覆盖)。两者解耦、互不覆盖。

**远程路径按阶段分流**:`EnvironmentInfo.git_intent_relative_path` / `git_reply_relative_path` 各自指定,留空回退阶段默认文件名(`intent_system_prompt.md` / `reply_system_prompt.md`)。**无通用 `git_relative_path`**。

### 9. 全链路流式编排(Query → dispatcher → drive → SSE)

`Runtime.run` → `Query.run`(async gen,产 `StreamEvent`)→ 透传 `dispatcher.execute` / `drive`(都是 async gen,产 `StepResult | WorkflowResult`)。**步级进度**:`drive` 每完成一步 `yield StepResult` → dispatcher 透传 → `Query._stream_process_step` 包成 `ToolProgress` → `Runtime.run` 序列化成 SSE `tool_progress` 行。多个 workflow 经 `asyncio.create_task` + `Queue` 并发执行、fan-in 汇总;意图(带 tools)/ 回复(不带 tools)两段 LLM 调用夹在中间。详见 `SSD/对话编排链路计划书.md`。

## 当前进度与缺口

- **generator 引擎已落地(全链路流式)**:`workflow_engine.py`(drive/compensate/_exec_step/replay_steps,均为 async gen + `ProcessContext` 穿透)+ `base.py`(create/business_key/execute)+ `meeting_room.py` + `adapters/base.py`(`step_call(process_ctx, ...)`)。详见 `SSD/工作流引擎设计方案.md`。
- **对话编排链路已落地**:`engine/query.py` `Query.run` 两段式 LLM(意图带 tools / 回复不带)+ 多 workflow 并发执行 + 步级 `ToolProgress` 流式;`engine/client/` 厂商无关流式抽象(`OpenAIClient`/`AnthropicApiClient`)。详见 `SSD/对话编排链路计划书.md`。**早期 `IntentParser` 已移除**(仅 runner/dispatcher 个别 docstring 残留提及),意图理解改由 LLM 原生 function-calling 承担。
- **端到端链路已跑通**:`test_dispatcher_e2e.py`(dispatcher.execute → drive → adapter mock OA → MySQL)验证全成功 / 补偿 / 幂等短路。
- **崩溃恢复(降级)已落地**:`downgrade.py`(handle_processes replay + 补偿,不重跑)+ `heartbeat.py` watchdog。详见 `SSD/失活流程降级落地计划.md`。
- **幂等已简化**:`Status(StrEnum)` + `is_new` + `IdempotencyCheckRequest`;无 Action/reject/attempt/_FROM_*。
- **提示词加载已分层落地**:`prompts/` 包(`system_prompt` 来源层 + `context` 运行时入口)+ `PromptType` 枚举 + `.prompt/custom/` 本地自定义 + 远程按 intent/reply 分流。详见 `SSD/提示词加载架构设计与落地计划.md`。
- **空骨架(当前主要缺口)**:`resolver.py`(身份补全)、`services/email.py`/`memory.py`、crm/ecs/erp adapter。

## 测试(`tests/`)

- 标准 **`unittest`**(非 pytest),异步 `IsolatedAsyncioTestCase`。
- ⚠️ `test_llm_client.py` 是**真实 LLM 冒烟测试**(消耗 token)。
- ⚠️ **DB 集成测试**(test_dispatcher_e2e / test_idempotency / test_step_tracker / test_database)需 MySQL 运行(`docker compose up -d`)。**多个 DB 集成 test method 在同一 test class 会因 asyncmy QueuePool 跨 loop 冲突——合并到单个 test method 或每 test 重建引擎**。
- `test_meeting_room_workflow`:generator 引擎(mock adapter + tracker,无 DB)。
- `test_verify_query_streaming`:`Query.run` 流式编排(mock LLM client + dispatcher,验证 tool_use → 并发执行 → tool_result → StreamEvent,**无 DB 无真实 LLM**)。
- `test_prompts_context`:`build_runtime_system_prompt` 三级降级 + intent/reply 分流(临时目录 mock `.prompt/custom/`)。
- `test_dispatcher_e2e`:端到端(dispatcher → engine → adapter mock OA → MySQL)。

## 数据库

`docker-compose.yml` 起 MySQL 8 + Redis 7,`db/` 挂载到 `/docker-entrypoint-initdb.d/`。**改 SQL 后 `down -v && up`**。容器 `utf8mb4_unicode_ci`。`process_step.result_data` **每步都写**(供 replay);`process` 有 `heartbeat_at`/`operator_context`。
