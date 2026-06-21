# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

`smart_talkflow` 是**业务无关的 Agent 编排平台**:把用户的一句自然语言,翻译成对已有传统业务系统(OA/ERP/CRM)的确定工作流调用,并在每一步留下可追溯的执行痕迹。平台**绝不复制业务数据**——员工/部门/邮箱等主数据归各传统系统所有,平台只负责「编排、执行、审计」。

设计上的贯穿示例是**员工入职**:LLM 解析意图+参数 → 按 姓名+部门 回查 HR 主数据补全业务唯一键(身份证号)→ 幂等校验 → 顺序执行 建档→开户→授权→邮箱 → 每步输入/输出/外部 HTTP 调用全部落库。**但当前代码里实际注册的示例流程是「会议室预订」**(`MeetingRoomBookingWorkflow`:提交→审批→更新使用状态),入职仅是 SSD 设计蓝本。项目按四阶段演进,**当前处于阶段一(MVP)**。完整设计见 `SSD/`。

## 运行环境与命令(关键)

### 包根是 `src/`,从项目根运行

运行时包路径根是 `src/`:代码内 import 一律**无 `src.` 前缀**(`from conf.config import settings`、`from engine.client.llm_client import OpenAIClient`)。因此 `src/` 必须在 `sys.path` 上。

**推荐:始终在项目根目录运行,并设 `PYTHONPATH=src`**——因为测试在项目根 `tests/` 下,从 `src/` 目录跑 `python -m unittest tests.xxx` 会找不到 `tests` 包:

```bash
PYTHONPATH=src python -m infra.database        # 以模块方式运行任意代码
PYTHONPATH=src python -c "import conf; print(conf.settings.llm_model)"
```

### 常用命令

```bash
uv sync                                            # 安装/同步依赖(用 uv 管理虚拟环境,见 uv.lock)
docker compose up -d                               # 起 MySQL 8 + Redis 7(首次启动自动执行 db/*.sql 建表,仅数据卷为空时执行)
docker compose down -v && docker compose up -d     # 改了 SQL 后必须清卷重建,否则建表脚本不再执行

# 跑服务(入口是 src/main.py,从项目根启动):
PYTHONPATH=src uv run uvicorn main:app --port 8000 --reload

# 测试(用标准库 unittest,异步用例继承 IsolatedAsyncioTestCase)
PYTHONPATH=src python -m unittest discover -s tests                 # 全部
PYTHONPATH=src python -m unittest tests.test_config                 # 单个模块
PYTHONPATH=src python -m unittest tests.test_idempotency.IdempotencyCheckerTest.test_xxx   # 单个用例
```

## 配置(`src/conf/config.py`)

用 `pydantic-settings`,`Settings` 在模块导入时实例化为单例 `settings`。**必填项缺失会在 `import conf` 时直接抛错**——「启动即失败」。改任何会触发配置加载的代码前,确保 `.env` 已从 `.env.example` 复制且必填项非空。

- **LLM**:`LLM_PROVIDER`(`openai`/`anthropic`)+ `LLM_API_KEY`/`LLM_MODEL`/`LLM_BASE_URL`/`LLM_TIMEOUT`/`LLM_TEMPERATURE` 全套必填;切换厂商只改 provider 及对应变量。
- **MySQL**:`MYSQL_DATABASE`/`MYSQL_USER`/`MYSQL_PASSWORD` 必填;`settings.mysql_conf` 直接给出拼好的 `mysql+asyncmy://...` 连接串。
- **下游 OA**:`OA_BASE_URL`/`OA_API_KEY`/`OA_DELEGATION_SECRET` 必填(服务账号技术认证 + operator 代签 HMAC,见下「代签凭证」)。api-key 与 delegation-secret 是**两个不同的随机密钥**(`secrets.token_urlsafe(32)`),用 `utils/api_key_util.py` 生成;yudao 侧 `OA_API_KEY` 存 **SHA-256 哈希**(`yudao.agent.api-key-hash`),`OA_DELEGATION_SECRET` 明文共享(HMAC 验签需原密钥)。
- **认证模式**:`AUTH_DEV_MODE`(默认 `True`)。**置 `False` 走生产态 SSO**,此时 `SSO_ISSUER`/`SSO_JWKS_URI`/`REDIS_URL` 必填。
- **Redis**:`REDIS_URL` 默认 `redis://127.0.0.1:6379/0`,用于 JWKS 公钥缓存等 KV 场景。
- ⚠️ `_required_non_blank` 校验器把 `SQL_LOG`、`TZ` 也纳入非空判断——若把 `SQL_LOG` 置 `False` 会在导入时报「mysql 配置不能为空」(falsy 拦截)。本地 `.env` 默认 `SQL_LOG=True` 不受影响,知道这一行为即可。

## 架构总览

分层 + 渐进解耦。请求级对象每请求构建、用完即弃;基础设施是进程级单例。

| 层 | 目录 | 状态 | 职责 |
|---|---|---|---|
| 配置 | `conf/` | ✅ | pydantic-settings 单例,启动即校验 |
| 入口 | `main.py` | ✅ | FastAPI 装配 + lifespan(`build_runtime` 一次装配,停机释放 DB 引擎与 redis) |
| LLM 引擎 | `engine/` | ✅ | 厂商无关的 LLM 抽象层(parser 仍为骨架) |
| 认证 | `security/` | ✅ | SSO JWT(RS256)验签:JWKS 公钥拉取 + redis 缓存 |
| API | `api/` | ✅ | `/chat` 路由(SSE 流式)+ 认证依赖 + 请求 DTO |
| 编排 | `orchestrator/` | ✅ | workflow 注册 + 调度执行(认证→权限→校验→幂等→执行→状态机) |
| 运行时 | `runtime/` | ✅ | 请求级上下文 + 启动装配工厂 + SSE 序列化 |
| 适配器 | `adapters/` | ✅(oa) | 封装传统系统 HTTP 调用(错误码归一 + 留痕);oa 已实现,crm/ecs/erp 骨架 |
| 服务 | `services/` | ✅(credential) | 代签凭证、邮箱、记忆等;`email`/`memory` 骨架 |
| 权限 | `permission/` | ✅ | 层 A RBAC:`WorkflowRoleChecker` 查 `workflow_role` 表 + redis 缓存 |
| 基础设施 | `infra/` | ✅ | DB/HTTP/日志/异常/幂等/Redis 全部完成 |
| 编排解析 | `engine/parser.py` | 🚧 | `IntentParser.run()` **仍为骨架(只有签名)** |
| 身份补全 | `orchestrator/resolver.py` | 🚧 | 空(查 HR 主数据、重名反问,未实现) |

> 🚧 表示空文件或仅有签名/占位,改动前先确认有无实质逻辑。

### LLM 引擎层(`engine/`)—— 厂商无关抽象

屏蔽 OpenAI 与 Anthropic 的协议差异:

- `client/messages.py`:统一会话与内容块模型。`ConversationMessage(role, content: list[ContentBlock])`,`ContentBlock` 用 `Field(discriminator="type")` 区分 `TextBlock` / `ToolUseBlock` / `ToolResultBlock`。**无论底层是 OpenAI `tool_calls` 还是 Anthropic `tool_use`,对外都归一为 `ToolUseBlock`。**
- `client/base_client.py`:`ApiMessageRequest`(入参)+ `ApiTextDeltaEvent`/`ApiMessageCompleteEvent`(流式事件)+ `SupportsInvokeMessages` Protocol(统一接口 `async stream_message(request) -> AsyncGenerator[ApiStreamEvent]`)。
- `client/llm_client.py`:`OpenAIClient` 与 `AnthropicApiClient` 各自实现 `stream_message`,逐 chunk 流式拉取、按 OpenAI 的 `index` 聚合增量 tool_calls,最终吐出统一的 `ApiStreamEvent`。
- `prompts/system_prompt.py`:系统提示词**三级降级**(见下)+ 末尾追加「当前可用工作流」清单供主控 LLM 选择。
- `prompts/envirement.py`:`EnvironmentInfo` 从 `settings` 读 git 仓库信息。
- `stream_event.py`:**编排层**消费的流式事件(`AssistantTextDelta`/`ToolExecutionStarted`/`ToolExecutionCompleted`),与 **client 层**的 `ApiStreamEvent` 是两套类型,不要混淆。
- `parser.py`:`IntentParser.run()` 骨架(意图解析+参数提取+工作流调用决策)。**当前只有签名,未实现**——见下「当前进度与缺口」。

### 认证层(`security/` + `api/deps.py`)

- `security/jwks_client.py`:`JwksKeyResolver` 从 `SSO_JWKS_URI` 拉取 JWKS、按 token header 的 `kid` 定位 RS256 公钥;JWKS 经 redis 缓存(TTL=`SSO_JWKS_CACHE_TTL`),`kid` 未命中时强制刷新一次(应对密钥轮换)。模块级单例 `jwks_resolver`。
- `api/deps.py`:`get_current_operator(request)` 是 FastAPI 依赖,按 `AUTH_DEV_MODE` 二选一解析操作人 → 注入 `OperatorContext`:
  - **开发态**(`True`):直接信任请求头 `X-Operator-Userid`/`X-Operator-Tenant`/`X-Operator-Roles`。
  - **生产态**(`False`):取 `Authorization` Bearer token,经 JWKS 验签后取 `sub`/`roles`/`tenant_id`。
  - 解析失败一律 `raise UnauthorizedException`(401)。
  - 核心解析逻辑(`resolve_operator` / `resolve_operator_from_sso`)是框架无关纯函数,可单测。

### 编排层(`orchestrator/`)

- `base.py`:`BaseWorkflow`(抽象基类:`name`/`description`/`input_model`/`business_key()`/`execute()`/`to_api_schema()`/`is_allowed()`)+ `WorkflowRegistry`(按 name 注册、查询、`to_api_schema()` 暴露给 LLM 作 tools)+ `WorkflowExecutionContext`/`WorkflowResult`。**`is_allowed()` 是 `async`**:从 `workflow_role` 表(经 `permission.workflow_role_checker`,DB + redis 缓存)查本流程允许角色,**空集=全员可用,非空比对 `operator.roles`**——角色策略**配置化**(不硬编码在 workflow 类,运维改表 + `invalidate` 即生效)。**注意:`base.py` 在 `orchestrator/` 下,不在 `workflow/` 下。**
- `dispatcher.py`:`WorkflowDispatcher.dispatch(name, arguments, context, max_retry)`——完整调度链,见下「调度链」。
- `workflow/meeting_room.py`:当前唯一注册的 `MeetingRoomBookingWorkflow`,顺序执行 submit→approve→update_use_status,业务唯一键 = `operator + roomId + 起止时间`。
- `resolver.py`:空。设计上负责身份补全(查 HR 主数据、重名反问)。

### 运行时(`runtime/`)

- `context.py`:`OperatorContext`(user_id/roles/tenant_id/name)+ `RequestContext`(operator + trace_id + process_id)+ `ContextVar`。深层组件(adapter/dispatcher)用 `get_operator()` 取当前请求操作人、`get_process_id()` 取当前流程实例 id(**无需层层透传**);`process_id` 由 dispatcher 在幂等创建 process 后 `set_process_id()` 回填,供 adapter 落 `adapter_call_logs` 关联。
- `runner.py`:`build_runtime()`(**启动装配工厂**,lifespan 调一次:组装 `WorkflowRegistry`+注册 workflow+`WorkflowDispatcher`+`IntentParser`)与 `Runtime.run()`(**每请求轻量执行**,见下「启动装配 vs 每请求执行」)。

### 适配器(`adapters/`)

- `base.py`:`BaseAdapter`(adapter_name/target_system/base_url/credential_provider)+ `AdapterRequest`(action/method/path/payload/params)+ `AdapterResponse`(结构化留痕,字段对齐 `AdapterCallLog` 表)。统一入口 `_call_action`:代签取头 → `infra.http.request` → `is_success()` 判定 → `extract_result()` 提取业务结果 → HTTP 状态码归一为业务异常 → **落 `AdapterCallLog`**(每次调用一条,带 operator/tenant/credential/trace/process 关联;落库失败 `except SQLAlchemyError` 只记日志、不阻断主流程)→ 构造留痕返回。
- `oa_adapter/`:`oa_client.py`(模块级单例 `client = OAClient()`,聚合会议室域)+ `oa_meeting_room.py`(会议室预订 adapter)。
- `crm_adapter/` / `ecs_adapter/` / `erp_adapter/`:各一个 `*_client.py` 骨架。

### 服务(`services/`)

- `credential.py`:代签凭证(见下「代签凭证」)。`email.py`/`memory.py` 为骨架。

### 基础设施(`infra/`)

`database.py`(异步引擎+会话)、`models.py`(5 张业务无关 ORM 模型)、`http.py`(httpx 封装+trace_id 注入)、`logger.py`(分级日志)、`exceptions.py`(`ApiException` + 11 个状态码子类,400/401/403/404/409/422/429/500/502/503/504)、`idempotency.py`(流程级幂等,见下)、`redis_client.py`(进程级单例)。全部已实现。

## 需要读多文件才能理解的设计

### 1. 提示词三级降级(`engine/prompts/system_prompt.py`)
`build_system_prompt(env, custom_prompt)` 优先级:**远程 git 仓库 > 自定义入参 > 内置默认 `_BASE_SYSTEM_PROMPT`**。远程拉取用 git 子进程:本地缓存目录 `.prompt/` 不存在则浅克隆,已存在则 `reset --hard` 强制以远程为准(直接覆盖不合并)。任一来源失败自动降级,不阻断启动。最终把 `WorkflowRegistry.to_api_schema()` 的工作流清单追加到末尾。

### 2. 全链路 Trace ID(`utils/trace_id_util.py` + `runtime/context.py`)
`trace_id_context: ContextVar` + `new_trace_id()`/`get_trace_id()`。同一 `trace_id` 贯穿:日志(`infra/logger.py` 的 `TraceIdFilter`)、数据库记录(`process.trace_id` 等)、对外 HTTP 请求头 `X-Trace-Id`(`infra/http.py` 自动注入)。`Runtime.run()` 每请求 `new_trace_id()` 写入 `RequestContext`。排查时凭一个 trace_id 串联整条链。

### 3. 数据库会话约定(`infra/database.py`)
用 `async with db_session() as session:` —— 正常退出自动 `commit`,异常 `rollback` 并重抛。引擎设了 `expire_on_commit=False`(异步下 commit 后对象不过期,可直接访问)和 `autoflush=False`(**写库必须手动 `await session.flush()`**)。关系属性惰性加载,异步会话访问前需 `selectinload`/`joinedload` 预加载,否则隐式 IO 报错。

### 4. 业务无关泛型 + 逻辑关联无物理外键(`infra/models.py`)
5 张表(`request_logs`/`process`/`process_step`/`adapter_call_logs`/`audit_logs`)全部业务无关:`process_key`/`business_key`/`adapter`/`action` 都是运行时赋值的泛型字符串。表间关联**故意不加物理外键**(日志表需独立于业务记录存活,满足审计不可篡改),ORM 用 `relationship(primaryjoin="foreign(...) == ...")` 在无 FK 前提下建立导航。`audit_logs` 用 `resource_type`+`resource_id` 多态指向任意资源。

### 5. 幂等:业务唯一键 + 状态机 + 重试计数(`infra/idempotency.py` + `orchestrator/dispatcher.py`)
幂等键不能用 `name`(会重名),必须用业务唯一键;但用户输入通常不含它,故执行前要先回查主数据补全(`resolver`,暂未实现),再以补全后的键做幂等。数据库用 `UNIQUE(process_key, business_key)`(对应 `process.idempotency_key`,键约定 `{process_key}_{business_key}`)兜底。

幂等判定是一个**状态机**:`IdempotencyChecker.check()` 先查 `process` 表(先查后插,并发命中 `IntegrityError` 则回查),按命中记录的 `status` 返回 `Action` 信号:

- `NEW`(未命中):插一条 `running` 占位,允许执行。
- `COMPLETED`:短路返回历史结果,跳过重复执行。
- `FAILED`:`context.attempt` 累加重试计数;超 `max_retry` 则 `_transition_status` 转 `reject` 永久拒绝,否则允许重跑。
- `REJECT` / `running` / 其它非终态:拒绝执行。

执行后由 dispatcher 按 `result.is_error` 调 `checker.completed()` / `failed()` 更新状态。状态转换受 `_FROM_*` 集合约束,避免跨态跳跃。

### 6. 代签凭证:服务账号 + operator HMAC(`services/credential.py` + `adapters/base.py`)
平台对下游业务系统的调用用**服务账号**做技术认证(`X-API-Key`),同时把**真实操作人**经 HMAC 签名「代签」给下游(`_build_agent_headers`):头含 `X-Operator-Userid` + `tenant-id` + `X-Agent-Signature`(HMAC-SHA256 over `userId|tenant|nonce|timestamp`)+ `X-Agent-Timestamp`/`X-Agent-Nonce`。下游 `AgentDelegationFilter`(**yudao 已实现**,注册在全局链、**先于** `TokenAuthenticationFilter`)按 `X-API-Key` 识别,校验 **api-key SHA-256 哈希比对(防配置泄露 + 恒定时间)、时间戳 ±5min 窗口、nonce Redis 去重(防重放)、HMAC 恒定比对**;通过后把当前用户改写为真实操作人,使 `@PreAuthorize` 按真实权限判定、审计归属真实用户。**密钥两把、分离**:`api-key`(服务账号身份,yudao 存哈希)与 `delegation-secret`(HMAC 密钥,yudao 明文存用于重算)是**不同值**,分别生成/管理/轮换。`utils/api_key_util.py` 生成 api-key + 哈希(`PYTHONPATH=src python -m utils.api_key_util`)。`CredentialProvider` Protocol 保证「同一 provider、不同 operator → 不同凭证」;`default_credential_provider(target_system)` 按 target_system 取 `settings.oa_api_key`/`oa_delegation_secret` 装配 `DefaultCredentialProvider`。

### 7. 启动装配 vs 每请求执行(`runtime/runner.py` + `main.py` + `api/router.py`)
**启动装配一次**(app `lifespan`):`build_runtime()` 组装 `WorkflowRegistry`(注册 MeetingRoomBookingWorkflow)+ `WorkflowDispatcher`(持 registry)+ `IntentParser`,存入 `app.state.runtime`;停机释放 DB 引擎与 redis。

**每请求轻量执行**:`POST /chat` → `Depends(get_current_operator)` 认证 → 取 `app.state.runtime` → `Runtime.run(operator, user_input)`:`new_trace_id()` + 建 `RequestContext` + 构造 user 消息 → `IntentParser.run()` 产出 `StreamEvent` → 序列化为 SSE `data:` JSON 行(`text`/`tool_started`/`tool_completed`/`unknown`)→ `StreamingResponse` 直出。**SSE 序列化归 runtime,router 不在请求内重新装配。**

### 8. 调度链与角色权限(`orchestrator/dispatcher.py`)
`WorkflowDispatcher.dispatch()` 顺序:① 认证(operator 缺失即拒)→ ② 查 workflow → ③ **权限网关**(`await workflow.is_allowed(operator)`:查 `workflow_role` 表允许角色,空集=全员可用,非空比对 `operator.roles`)→ ④ Pydantic 参数校验 → ⑤ 幂等校验(见上,命中后 `set_process_id(process.id)` 回填 `RequestContext` 供 adapter 落库关联)→ ⑥ `workflow.execute()` → ⑦ 按结果更新 process 状态。任一步失败都返回带 `is_error=True` 的 `WorkflowResult` 而非抛异常(除非执行体本身抛异常),便于上层流式反馈。

### 9. 适配器错误码归一(`adapters/base.py`)
`BaseAdapter._call_action()` 把下游真实 HTTP 状态码经 `_STATUS_EXCEPTIONS` 映射为对应 `ApiException` 子类(400→`BadRequestException` … 504→`GatewayTimeoutException`,未列出的统一 500),但**不向上抛**——捕获后转为 `AdapterResponse(is_error=True)` 返回,保证一次调用必有一条结构化留痕(`AdapterResponse` 字段与 `adapter_call_logs` 表对齐,可直接落库)。网络层异常(httpx)统一记为 503。`is_success()`/`extract_result()` 是子类必须实现的两个抽象方法(各业务系统成功判定与结果结构不同)。

## 当前进度与缺口(改动前必读)

- **层 A RBAC 已配置化**:`workflow_role` 表 + `permission.WorkflowRoleChecker`(DB + redis 缓存),`is_allowed` 异步查表,角色策略运维可改、无需重启(`invalidate` 立即生效或等 TTL)。
- **审计落库已闭环**:adapter 每次 HTTP 调用落 `AdapterCallLog`(operator/tenant/credential/trace/process 关联),可「凭一个 operator_id 串联该用户所有下游调用」。
- **代签安全已增强**:yudao `AgentDelegationFilter` 用 api-key **哈希比对**(防配置泄露)+ **nonce Redis 去重**(防重放)+ **恒定时间比对**(防时序)。**不走 OAuth2/token 刷新**(实测 yudao `client_credentials` 的 token 绑 `userId=0` 无业务权限)。
- **端到端尚未真正跑通**:`/chat` → 认证 → `Runtime.run` 链路已搭好,但 `IntentParser.run()` **只有签名、不产出任何 `StreamEvent`**——`runtime.run` 迭代它实际拿不到事件。这是当前主线缺口。
- **`WorkflowDispatcher` 完整但未接入主链路**:`dispatcher` 已实现完整调度+幂等+状态机,但 `IntentParser` 还没产出 workflow 调用决策交给它(`runner.run` 的 docstring 标注「后续」才接 dispatcher)。给 dispatcher 补单测是安全的(已有 `tests/test_idempotency.py`)。
- **会议室预订是唯一真实流程**:`MeetingRoomBookingWorkflow.execute()` 调 `adapters.oa_adapter.oa_client.client`,依赖下游 OA(yudao 风格)真实可达 + `.env` 配好 OA 凭证。
- **`resolver.py`(身份补全)、`services/email.py`/`memory.py`、crm/ecs/erp adapter 均为空骨架。**

## 测试(`tests/`)

- 用标准库 **`unittest`**(非 pytest),异步用例继承 `unittest.IsolatedAsyncioTestCase`。
- 从项目根运行:`PYTHONPATH=src python -m unittest discover -s tests`。
- ⚠️ `tests/test_llm_client.py` 是**真实 LLM 冒烟测试**——会真正发起 OpenAI/Anthropic API 调用,消耗 token、依赖 `.env` 配好的可用 key 与网络。
- 纯本地测试:`test_config`/`test_database`/`test_http`/`test_logger`/`test_system_prompt`/`test_idempotency`(幂等状态机)/`test_deps`(认证依赖 + `is_allowed` 异步 mock)/`test_sso`(JWKS 验签)/`test_audit_logging`(adapter 落 `AdapterCallLog`)。

## 数据库

`docker-compose.yml` 起 **MySQL 8 + Redis 7** 两个服务,把 `db/` 只读挂载到 `/docker-entrypoint-initdb.d/`。**MySQL 官方镜像仅在数据卷为空时执行该目录脚本**,所以改了 `db/smart_talkflow_init.sql` 后必须 `docker compose down -v` 清卷再 `up`,否则建表不重新执行(本轮新增 `workflow_role` 表,务必清卷重建)。`db/schema_diagram.md` 有 ER 图与落库流说明。容器强制 `utf8mb4_unicode_ci`,与建表脚本对齐,避免 JOIN 时 `Illegal mix of collations`(错误码 1267)。`adapter_call_logs` 含 `operator_id`/`tenant_id`/`credential_source` 列,对应代签留痕;`workflow_role`(`workflow_name`/`role`,UNIQUE)承载层 A RBAC 配置,`process_step` 记录步骤(断点恢复尚未接入,见下)。

## 与 README 的差异(务必留意)

`README.md` 描述的是**计划结构/早期结构**,与当前代码多处不符,改代码以实际目录为准:

- README 写 `orchestrator/actions/`,实际已重构为 `orchestrator/workflow/`(仅 `meeting_room.py`)+ `orchestrator/base.py`(基类与注册器在 `orchestrator/` 下)。
- README 入口写根 `main.py`,实际入口是 `src/main.py`(根 `main.py` 已删);运行需 `PYTHONPATH=src uv run uvicorn main:app`。
- README 仍以「员工入职」为主线示例,实际注册的是「会议室预订」。
- README 的 `cd src` 运行方式 import 可用,但跑测试仍需从项目根(见「运行环境」)。
- README 提到的 `works/` 目录、`services/stream.py`、`runtime/RequestRunner.py`、`engine/messages.py` 已不存在/已迁移(分别并入 `workflow/`、删除、改名 `runtime/runner.py`、迁到 `engine/client/messages.py`)。
- README 未提及 `security/` 认证层、redis、OA 代签凭证、SSE 流式等已落地内容。
