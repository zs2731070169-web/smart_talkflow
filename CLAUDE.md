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

- **LLM**:`LLM_PROVIDER`(`openai`/`anthropic`)+ `LLM_API_KEY`/`LLM_MODEL`/`LLM_BASE_URL`/`LLM_TIMEOUT`/`LLM_TEMPERATURE`。
- **MySQL**:`MYSQL_DATABASE`/`MYSQL_USER`/`MYSQL_PASSWORD`;`settings.mysql_conf` 给出 `mysql+asyncmy://...`。
- **下游 OA**:`OA_BASE_URL`/`OA_API_KEY`/`OA_DELEGATION_SECRET`(服务账号 + operator 代签 HMAC)。
- **认证**:`AUTH_DEV_MODE`(默认 `True` 开发态信任请求头;`False` 走 SSO/JWT)。
- **崩溃恢复**:`process_heartbeat_timeout`(无心跳超时秒,判崩溃)+ `process_recovery_interval`(watchdog 扫描间隔)。~~`process_max_attempts`~~ 已废弃(不重跑,无 attempt)。

## 架构总览

分层 + 渐进解耦。请求级对象每请求构建、用完即弃;基础设施是进程级单例。

| 层 | 目录 | 职责 |
|---|---|---|
| 配置 | `conf/` | pydantic-settings 单例 |
| 入口 | `main.py` | FastAPI + lifespan |
| LLM 引擎 | `engine/` | 厂商无关 LLM 抽象(parser 骨架) |
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

**workflow_engine.py**:generator-based 工作流引擎。业务 workflow 用**同步 generator** 声明步骤(`yield step(...)`),引擎 `drive` 驱动:

- **`Compensate(Exception)`**:引擎↔业务补偿协议。drive 失败时 `gen.throw(Compensate)`,业务 `except Compensate` yield 补偿步。
- **`Step`**(dataclass:`func`/`args`/`kwargs`/`name`):`func` 返回 `StepResult`(adapter 层已转好),引擎**不 import `AdapterResponse`**。
- **`step(func, *args, name="", **kwargs)`**:yield 工厂。
- **`StepResult`**(dataclass:`ok`/`data`/`error`/`name`/`step_id`):引擎与副作用的唯一契约。
- **`drive(workflow, arguments, *, on_step=None)→WorkflowResult`**:gen=create;循环 `gen.send(result.data)`;`not ok` → `_compensate`(gen.throw);StopIteration → return 文案。
- **`_exec_step(step, step_no)→StepResult`**:flush_heartbeat → _step_meta(反射推断 adapter/action/input_params)→ create_step → set_step_id → step.execute() → finish_step(每步存 result_data)→ set_step_id(None)。
- **`compensate(gen, step_results, fail, on_step)`**:throw Compensate + 驱动补偿步(_exec_step + update_compensation)。
- **`replay_steps(workflow, arguments, process_id)`**:重建 generator,replay DB 已成功步 result_data 回灌闭包变量,定位失败点。

**base.py**:`BaseWorkflow`(abstract `create(arguments)→Generator[Step, Any, str]`;`execute(arguments)` 局部 import `drive` 委托)+ `WorkflowRegistry` + `WorkflowResult`。

**dispatcher.py**:`execute(workflow, inputs)` → 认证(get_operator)→ 权限(is_allowed)→ 幂等(checker.check)→ `workflow.execute(inputs)`(→ base.execute → drive)→ finalize(checker.completed/failed)。**execute 不接 context**(B 方案:operator 在 ContextVar)。

**downgrade.py**:`handle_processes(registry)`——detect(超时)→ acquire_recovery_lock(抢权)→ 重建 operator/arguments → replay_steps → compensate(throw 补偿)→ transition_status(failed/completed)。**不重跑 adapter**(replay 用 DB result_data 回灌,零下游副作用)。

**idempotency.py**:`Status(StrEnum: RUNNING/COMPLETED/FAILED)` + `IdempotencyChecked`(process + is_new + complete_result/error/message)。`check()` 返 `is_new=True`(首次放行)或按 status 短路(completed→历史结果;failed→失败;running/非终态→拒绝)。**无 Action 枚举、无 reject、无 attempt 计数、无 _FROM_* 集合**。

**workflow/meeting_room.py**:`create(arguments)` —— `try: booking_id = yield step(submit, ...); yield step(approve, ...); yield step(update, ...); return "已预订"` / `except Compensate: yield step(cancel, booking_id); return "已取消"`。

### 适配器(`adapters/`)

- `base.py`:`BaseAdapter._call_action(request)→AdapterResponse`(HTTP + 代签 + 日志,**不落库**);`step_call(request)→StepResult`(**resp = _call_action → 落 AdapterCallLog → 转 StepResult**)。**落库在 step_call**,不在 _call_action。adapter action(`submit_booking` 等)调 `step_call` 返 `StepResult`。
- `oa_adapter/oa_base.py`:`OAAdapter`(yudao 协议:`is_success` 判 `code==0`;`extract_result` 取 `data`)。
- `oa_adapter/oa_meeting_room.py`:4 个 action 返 `StepResult`(`step_call` 内转)。

### 运行时(`runtime/`)

- `context.py`:`OperatorContext` + `RequestContext`(operator + trace_id + process_id + step_id)+ ContextVar。`get_operator()`/`get_process_id()`/`get_step_id()` 无需层层透传。
- `runner.py`:`build_runtime()`(启动装配)+ `Runtime.run()`(每请求执行)。
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

## 当前进度与缺口

- **generator 引擎已落地**:`workflow_engine.py`(drive/_exec_step/compensate/replay_steps)+ `base.py`(create/execute)+ `meeting_room.py`(create generator)+ `adapters/base.py`(step_call)。详见 `SSD/工作流引擎设计方案.md`。
- **端到端链路已跑通**:`test_dispatcher_e2e.py`(dispatcher.execute → engine drive → adapter mock OA → MySQL)验证全成功 / 补偿 / 幂等短路 3 场景。
- **崩溃恢复(降级)已落地**:`downgrade.py`(handle_processes replay + 补偿,不重跑)+ `heartbeat.py` watchdog。详见 `SSD/流程崩溃自动恢复落地计划.md`。
- **幂等已简化**:`Status(StrEnum)` + `is_new`;无 Action/reject/attempt/_FROM_*。
- **parser 仍为骨架**:`IntentParser.run()` 的 LLM 调用未实现——当前主线缺口。
- **空骨架**:`resolver.py`(身份补全)、`services/email.py`/`memory.py`、crm/ecs/erp adapter。

## 测试(`tests/`)

- 标准 **`unittest`**(非 pytest),异步 `IsolatedAsyncioTestCase`。
- ⚠️ `test_llm_client.py` 是**真实 LLM 冒烟测试**(消耗 token)。
- ⚠️ **DB 集成测试**(test_dispatcher_e2e / test_idempotency / test_step_tracker)需 MySQL 运行(`docker compose up -d`)。**多个 DB 集成 test method 在同一 test class 会因 asyncmy QueuePool 跨 loop 冲突——合并到单个 test method 或每 test 重建引擎**。
- `test_meeting_room_workflow`:generator 引擎(mock adapter + tracker,无 DB)。
- `test_dispatcher_e2e`:端到端(dispatcher → engine → adapter mock OA → MySQL,3 场景)。

## 数据库

`docker-compose.yml` 起 MySQL 8 + Redis 7,`db/` 挂载到 `/docker-entrypoint-initdb.d/`。**改 SQL 后 `down -v && up`**。容器 `utf8mb4_unicode_ci`。`process_step.result_data` **每步都写**(供 replay);`process` 有 `heartbeat_at`/`operator_context`。
