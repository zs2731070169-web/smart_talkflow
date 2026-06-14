> **目标**：让成熟的传统 OA/ERP/CRM 系统具备自然语言驱动能力，从"同步单点"逐步演进为"长程可靠执行"的 Agent 化平台。
>
> **核心原则**：先跑通，再解耦；先同步，再异步；先人工，再自动。
>
> **技术栈**：Python 全栈

------

## 文档导航

- [1：最小可行产品（MVP）——同步单流程硬跑通](https://www.notion.so/Agent-37c0bfc33f8380ebb8c6c8bedf175b0e?pvs=21)
- [2：配置化与解耦——YAML 驱动 + 适配层独立](https://www.notion.so/Agent-37c0bfc33f8380ebb8c6c8bedf175b0e?pvs=21)
- [3：MCP 标准化——适配层 MCP Server 化](https://www.notion.so/Agent-37c0bfc33f8380ebb8c6c8bedf175b0e?pvs=21)
- [4：长程任务可靠性——异步、事件驱动与 Saga 补偿](https://www.notion.so/Agent-37c0bfc33f8380ebb8c6c8bedf175b0e?pvs=21)
- [全局支撑体系（监控、安全、团队）](https://www.notion.so/Agent-37c0bfc33f8380ebb8c6c8bedf175b0e?pvs=21)

------

## 最小可行产品（MVP）——同步单流程可执行

### 架构图

```mermaid
graph TD
    A[用户/Chat] -->|自然语言| B[Agent+编排层]
    B -->|意图识别| C[LLM]
    C -->|结构化参数| B
    B -->|同步函数调用| D[适配层]
    D -->|HTTP/RPA| E[现有系统业务域<br/>emp/identity/auth]
    E -->|返回结果| D
    D -->|返回结果| B
    B -->|回复用户| A
```

**说明**：Agent、编排层、适配层在**同一进程**内；适配层以内联模块（非独立服务）形式存在，直接调用传统系统接口。本阶段单场景只接入入职流程涉及的 emp / identity /auth 邮箱三个业务。

### **技术选型**

| 层级       | 技术                   | 理由                                 |
| ---------- | ---------------------- | ------------------------------------ |
| API 框架   | FastAPI                | 异步原生、自动文档、Pydantic 集成    |
| 数据校验   | Pydantic v2            | LLM 输出强校验，防止幻觉参数污染下游 |
| ORM/数据库 | SQLAlchemy 2.0 + MySQL | 流程实例持久化、审计留痕             |
| 外部调用   | httpx (async)          | 异步 HTTP，替代 requests             |
| LLM 接入   | OpenAI / Anthropic     | 意图识别 + Function Calling          |

### **模块功能设计**

| 模块         | 职责                                            | 关键类/函数                                   | 代码位置                         |
| ------------ | ----------------------------------------------- | --------------------------------------------- | -------------------------------- |
| Agent 解析器 | 接收自然语言，调用 LLM 提取意图与参数，缺参反问 | IntentParser.parse(user_input) -> AgentIntent | agent/parser.py                  |
| 身份解析器   | 按姓名+部门查 HR 主数据补全身份证号，重名则反问 | EmployeeResolver.resolve(name, dept)          | orchestrator/resolver.py         |
| 硬编码编排器 | 针对入职单场景，写死「建档→开户→邮箱」执行顺序  | OnboardingOrchestrator.run(params)            | orchestrator/onboarding.py       |
| 适配器       | 直接封装 OA/CRM/邮箱 的 HTTP 调用               | OAAdapter / EmailAdapter                      | adapters/oa.py adapters/email.py |
| 幂等控制器   | 基于身份证号（业务唯一键）防重复执行            | IdempotencyChecker.check(business_key)        | core/idempotency.py              |

### **数据流时序图**

```mermaid
sequenceDiagram
    autonumber
    actor U as 用户
    participant A as Agent+编排层
    participant L as LLM
    participant OA as OA适配器
    participant EM as 邮箱适配器
    participant SYS as 传统系统<br/>OA/AD/邮箱
    participant DB as MySQL

    U->>A: "给市场部张三办入职"
    A->>L: 发送 Prompt（含可用流程描述）
    L-->>A: 返回 {intent:"onboarding", params:{name:"张三", dept:"市场部"}}

    Note over A,DB: 补全幂等键：按姓名+部门查 HR 主数据得身份证号；重名则反问用户
    A->>DB: 查询幂等键 onboarding_{身份证号}
    DB-->>A: 不存在，允许执行

    Note over A,SYS: Step 1 建档
    A->>OA: create_employee(身份证号, name, dept)
    OA->>SYS: 调用 OA REST API
    SYS-->>OA: {emp_id:"9527"}
    OA-->>A: {emp_id:"9527"}

    Note over A,SYS: Step 2 域账号
    A->>OA: create_account(emp_id, name)
    OA->>SYS: 调用 AD 域控 API
    SYS-->>OA: {account:"zhangsan"}
    OA-->>A: {account:"zhangsan"}

    Note over A,SYS: Step 3 权限授权（按部门/岗位分配 OA 角色、加入用户组、开通功能模块）
    A->>OA: grant_permissions(account, dept, position)
    OA->>SYS: 调用 OA 权限接口<br/>分配角色 + 加入用户组
    SYS-->>OA: {roles:["市场部-标准员工"], groups:["市场部"]}
    OA-->>A: {roles:["市场部-标准员工"], groups:["市场部"]}

    Note over A,SYS: Step 4 业务系统授权（开通 CRM 等关联系统账号与默认访问权限）
    A->>OA: grant_app_access(account, apps:["crm"])
    OA->>SYS: 调用统一权限中心 / CRM 接口
    SYS-->>OA: {crm_account:"zhangsan", enabled:true}
    OA-->>A: {crm_account:"zhangsan", enabled:true}

    Note over A,SYS: Step 5 邮箱（失败则无自动补偿，需人工介入）
    A->>EM: create_mailbox(account, name)
    EM->>SYS: 调用邮箱系统 API
    SYS-->>EM: {mailbox:"zhangsan@corp.com"}
    EM-->>A: {mailbox:"zhangsan@corp.com"}

    A->>DB: 写入流程实例记录（Completed）
    A-->>U: "入职完成：员工号9527，域账号zhangsan，OA权限已授权，CRM已开通，邮箱zhangsan@corp.com"
```

### **工程代码模块划分**

```
smart_talkflow/
├── prompts
├── main.py                  # FastAPI 应用装配与启动（生命周期、挂载路由/中间件、注册全局异常处理器）
├── config.py                # 环境变量与配置（Pydantic Settings：DB URL、LLM Key、各系统 BaseURL/超时）
├── requirements.txt
├── .env.example             # 环境变量样例（占位值，不含密钥真值）
├── api/
│   ├── __init__.py
│   ├── deps.py              # 依赖注入：get_db（DB Session）、当前操作人等
│   ├── router.py            # /chat 与 /execute 路由定义
│   └── schema.py            # HTTP 请求/响应 DTO（Pydantic）
├── runtime/                 # 请求级执行上下文：每请求构建一个，承载意图/参数/幂等键/步骤中间产物，用完即弃（不持久化）
│   ├── __init__.py       
│   ├── context.py           # RequestContext：聚合, deps.py 里加 get_request_context 注入外壳
│   ├── RequestRunner.py     # 每请求构建的执行对象，串联 parse → resolve → 幂等 → orchestrator
├── engine/
│   ├── __init__.py
│   ├── parser.py            # LLM 意图解析 + 参数提取 + 缺参反问
│   ├── llm_client.py        # LLM 客户端封装（OpenAI/Anthropic、超时、重试、Function Calling）
│   ├── prompts.py           # 意图识别 Prompt 管理（含可用流程描述与 few-shot）
│   └── models.py            # AgentIntent、LLMMessage 等 Pydantic 模型
├── orchestrator/
│   ├── __init__.py
│   ├── dispatcher.py        # intent → 对应编排器的路由分发
│   ├── resolver.py          # 数据补全和反问
│   └── onboarding.py        # 硬编码入职流程（建档→开户→邮箱）
├── adapters/
│   ├── __init__.py
│   ├── base.py              # 适配器基类：共享 httpx.AsyncClient、超时、错误码归一等
│   ├── oa_client.py     # OA REST
│   ├── oa_adapter/
│		│   ├── authorization.py # 权限域控 (按部门/岗位分配角色, 加入用户组, 开通业务系统)
│   │   ├── employee.py      #【员工域】建档 / 查员工 / 变更状态 (create_employee)
│   │   └── identity.py      #【身份/认证域】建域账号 / 改密 / 禁用（create_account）  
├── services/
│   └── email.py             # 邮箱系统 HTTP 封装（create_mailbox）
├── infra/
│   ├── __init__.py
│   ├── database.py          # SQLAlchemy engine + SessionLocal + Base（连接/会话管理）
│   ├── models.py            # SQLAlchemy 流程实例 ORM 模型 ProcessInstance（与 api/schema.py 区分）
│   ├── idempotency.py       # 幂等校验逻辑（依赖 UNIQUE(process_key, business_key)）
│   ├── http.py              # 封装统一的http请求
│   └── exceptions.py        # 业务/适配器异常定义（供 main.py 全局异常处理器捕获）
└── tests/                   # 冒烟测试：重复请求幂等、参数越界校验、邮箱失败无补偿留痕
```

### **埋坑点**

1. **LLM 参数幻觉**：LLM 可能编造不存在的部门名称。必须在编排层用 Pydantic `validator` 或枚举值强校验，不合法参数绝不透传给下游 OA。
2. **幂等键**：不能用 `name` 做幂等键（重名），必须用身份证号等业务唯一键；但用户说「给市场部张三办入职」通常**不含身份证号**。因此执行前需先按 `姓名+部门` 查 HR 主数据补全身份证号（命中多条则反问用户选择），再以补全后的键做幂等校验。数据库加唯一索引 `UNIQUE(process_key, business_key)`。
3. **同步阻塞风险**：传统系统接口可能长时间不返回（如 30 秒），会阻塞 FastAPI 处理。本阶段用 `httpx.AsyncClient(timeout=10)` 严格限制，超时即报错，绝不无限等待。
4. **无补偿的灾难**：三步顺序执行，若 Step 3（邮箱）失败，Step 1/2（建档/开户）已落地且**本阶段无自动回滚**，必须人工介入。每一步执行后在代码里预留 `# TODO: compensate` 钩子，为阶段4 的 Saga 补偿铺路。

### **挑战点**

- **Prompt 工程稳定性**：LLM 提取参数的准确率必须 > 90%，否则用户体验崩塌。需要设计多轮补全机制（缺参数时反问用户）。
- **传统系统接口文档缺失**：很多老 OA 没有标准 REST，需要抓包或读前端代码，时间不可控。

## 配置化与解耦——YAML 驱动 + 适配层独立

**目标**：新增业务不用改 Python 代码，改 YAML 配置即可；适配层独立部署，业务流程执行使用"通用引擎"。

### 架构图

```mermaid
graph TD
    A[用户/Chat] -->|自然语言| B[Agent+编排层<br/>FastAPI]
    B -->|意图识别| C[LLM]
    B -->|内存加载| D[YAML流程定义<br/>processes/*.yaml]
    B -->|执行| E[通用WorkflowEngine]
    E -->|HTTP调用| F[适配层服务<br/>oa-adapter:8001]
    E -->|HTTP调用| G[适配层服务<br/>crm-adapter:8002]
    E -->|HTTP调用| H[适配层服务<br/>email-adapter:8003]
    F -->|HTTP/RPA| I[OA域控]
    G -->|HTTP/RPA| J[CRM域控]
    H -->|HTTP/RPA| K[邮箱系统]
    
    style D fill:#f9f,stroke:#333,stroke-width:2px
```

**说明**：编排层热加载 YAML；适配层从"内联函数升级为独立 FastAPI 微服务，通过 HTTP 被编排层调用。

### **技术选型**

| 层级       | 技术                 | 理由                                        |
| ---------- | -------------------- | ------------------------------------------- |
| 流程定义   | YAML + Pydantic 校验 | 人工编写，结构化，可版本控制                |
| 热加载     | watchdog             | 文件系统监听，秒级热更新                    |
| 工作流引擎 | 自研轻量状态机       | 本阶段流程步骤固定，自研比引入 Camunda 更轻 |
| 适配层协议 | REST HTTP (FastAPI)  | 简单、可调试、团队熟悉                      |
| 服务发现   | 静态配置 / 环境变量  | 本阶段无服务网格，配置中心够用              |
| 异步任务   | Celery + Redis       | 适配层内部耗时操作（如 RPA）可异步化        |

### **模块功能设计**

| 模块              | 职责                              | 关键设计                                          |
| ----------------- | --------------------------------- | ------------------------------------------------- |
| YAML 流程定义中心 | 存储、校验、热加载流程定义        | ProcessStore类，监听 processes/*.yaml 变化        |
| 通用编排引擎      | 读取 YAML，按步骤驱动，管理上下文 | WorkflowEngine：支持串行、并行、参数映射          |
| 适配层注册表      | 维护 adapter_key -> endpoint_url  | 数据库表 adapter_registry，动态路由               |
| 规则引擎          | 权限、数据格式、前置条件校验      | 轻量自研，JSON 配置 + Python 函数                 |
| 上下文管理器      | 跨步骤数据传递                    | ContextResolver：支持 context.step_key.field 语法 |

### **数据流时序图**

```mermaid
sequenceDiagram
    autonumber
    actor Dev as 开发/业务
    participant File as processes/onboarding.yaml
    participant Engine as WorkflowEngine
    participant DB as MySQLSQL
    participant Reg as AdapterRegistry
    participant OA_Svc as OA适配服务

    Dev->>File: 新增/修改 YAML 流程定义
    File->>Engine: watchdog 触发热加载
    Engine->>Engine: Pydantic 校验 YAML 结构
    Engine->>DB: 更新 process_definitions 表（缓存）
    
    actor U as 用户
    U->>Engine: "给张三办入职"
    Engine->>Engine: 加载 onboarding.yaml 定义
    Engine->>DB: 幂等校验
    Engine->>Reg: 查询 oa-adapter 的 endpoint
    Reg-->>Engine: <http://ad-adapter:8002>
    
    Engine->>OA_Svc: POST /actions/create_account<br/>{emp_id, name}
    OA_Svc->>OA_Svc: 封装 OA 域控调用
    OA_Svc-->>Engine: {account: "zhangsan"}
    
    Engine->>Engine: 上下文写入 context.create_emp = result
    Engine->>Engine: 读取 YAML 的 next_steps，驱动下一步
    Engine->>DB: 更新流程状态
```

### **工程代码模块划分**

```
oa_agent/
├── main.py
├── api/   
│   ├── __init__.py
│   ├── deps.py                      # 依赖注入：get_db（DB Session）、当前操作人等
│	  ├── router.py                    # /chat 和 /execute 路由
│   └── schema.py                    # http请求和返回实例
├── processes/                       # ⭐ 新增：YAML 流程定义目录
│   ├── employee_onboarding.yaml
│   └── employee_offboarding.yaml
├── engine/
│   ├── parser.py                    # LLM 意图解析 + 参数提取 + 缺参反问
│   ├── llm_client.py                # LLM 客户端封装（OpenAI/Anthropic、超时、重试、Function Calling）
│   ├── prompts.py                   # 意图识别 Prompt 模板（含可用流程描述与 few-shot）
│   └── models.py                    # AgentIntent、LLMMessage 等 Pydantic 模型
├── orchestrator/
│   ├── __init__.py
│   ├── loader.py                    # ⭐ YAML 热加载器 (watchdog)
│   ├── models.py                    # MySQLSQL 数据模型
│   ├── context.py                   # ⭐ 上下文解析器
│   └── workflow_engine.py           # ⭐ 通用 WorkflowEngine
├── adapters/
│   ├── registry.py                  # ⭐ AdapterRegistry 服务发现                        # ⭐ 变为独立服务目录
│   ├── oa_adapter/
│   │   ├── main.py                  # FastAPI 独立服务
│   │   ├── client.py                # OA 系统原始 HTTP 封装
│   │   └── requirements.txt
│   ├── email_adapter/
│   │   ├── main.py
│   │   └── client.py
│   └── shared/                      # 适配层公共库（日志、错误码）
│       └── schemas.py
├── infra/
│   ├── models.py
│   ├── database.py                  # SQLAlchemy engine + SessionLocal + Base（连接/会话管理）
│   ├── models.py                    # SQLAlchemy 流程实例 ORM 模型 ProcessInstance（与 api/schema.py 区分）
│   ├── idempotency.py               # 幂等校验逻辑（依赖 UNIQUE(process_key, business_key)）
│   ├── exceptions.py                # 业务/适配器异常定义（供 main.py 全局异常处理器捕获）
│   └── rules.py                     # ⭐ 轻量规则引擎
└── requirements.txt
```

### **埋坑点**

1. **YAML 校验必须严格**：如果 YAML 里的  adapter: oa-adapter  写错，或  input_mapping  引用了不存在的上下文字段，必须在加载时就报错，而不是执行时才发现。用 Pydantic 做  ProcessDef  模型校验。
2. **热加载线程安全**： watchdog  触发重载时，可能正在执行旧流程。使用版本号机制：新 YAML 加载为  v2 ，旧实例继续用  v1 ，新实例用  v2 。
3. **适配层网络隔离**：适配层独立部署后，可能出现"编排层能启动，但连不上适配层"的情况。启动时必须做健康检查（ GET /health ），不健康则标记为不可用。
4. **上下文污染**：并行步骤（如同时开邮箱和门禁）如果同时写同一个  context  字段，会互相覆盖。并行步骤的输出必须隔离在  context.{step_key}  命名空间下。

### **挑战点**

- **YAML 编写门槛**：业务人员写 YAML 容易出错。需要提供JSON Schema 自动补全或一个极简的 Web 表单生成器。
- **调试链路变长**：问题可能在编排层、YAML 配置、适配层、传统系统四层中的任意一层。必须引入分布式 Trace ID，贯穿全链路日志。
- **并行步骤的聚合**：YAML 声明  parallel_next: true  后，引擎需要等待所有并行分支完成才能执行下一步。需要设计Join 节点或隐式聚合逻辑。

## MCP 标准化——适配层 MCP Server 化

**目标**：适配层从"私有 HTTP 接口"升级为"标准化 MCP Server"，可通过统一协议调用。

### 架构图

```mermaid
graph TD
    A[用户/Chat] -->|自然语言| B[Agent+编排层<br/>FastAPI]
    B -->|LLM| C[意图识别]
    B -->|内存加载| D[YAML流程定义中心<br/>processes/*.yaml]
    B -->|执行| E[通用WorkflowEngine]
    E -->|调用步骤| F[MCP Client]
    F -->|MCP 协议| G[OA适配服务<br/>MCP Server]
    F -->|MCP 协议| H[email适配服务<br/>MCP Server]
    G -->|HTTP/RPA| J[AD域控]
    H -->|HTTP/RPA| K[邮箱系统]
    
    style F fill:#f9f,stroke:#333,stroke-width:2px
    style G fill:#bbf,stroke:#333,stroke-width:2px
    style H fill:#bbf,stroke:#333,stroke-width:2px
```

**说明**：编排层作为 MCP Client，通过 stdio 或 sse 连接适配层 MCP Server。适配层本身也是独立进程，可被第三方 MCP Host 直接调用。

### **技术选型**

| 层级     | 技术                      | 理由                                               |
| -------- | ------------------------- | -------------------------------------------------- |
| MCP 框架 | FastMCP (官方 Python SDK) | 极简封装，@mcp.tool() 装饰器即可暴露能力           |
| 传输协议 | stdio（本阶段主推）       | 本地子进程，安全，适合服务器部署；sse 用于远程调试 |
| 进程管理 | supervisord / systemd     | 管理多个 MCP Server 子进程生命周期                 |
| 服务发现 | 静态配置（yaml）          | 编排层配置 adapter_key -> 启动命令                 |

### 模块功能设计

| 模块              | 职责                             | 关键设计                                                     |
| ----------------- | -------------------------------- | ------------------------------------------------------------ |
| MCP Server 基座   | 每个适配层暴露标准 Tool          | 统一错误码、统一参数命名、统一                               |
| MCP Client 连接池 | 编排层管理多个子进程连接         | AdapterClientPool：维护 adapter_key -> ClientSession 映射    |
| 工具自描述        | 适配层通过 list_tools() 暴露能力 | 编排层启动时扫描，用于 Agent 的 Function Calling 元数据      |
| 双向通信          | 支持适配层主动推送进度           | stdio 是单向请求-响应；如需推送，适配层应通过 HTTP 回调编排层 |

### **数据流时序图**

```mermaid
sequenceDiagram
    autonumber
    participant Engine as WorkflowEngine
    participant Pool as MCP Client Pool
    participant MCP as oa-adapter<br/>(MCP Server)
    participant OA as OA域控

    Note over Engine: 编排层启动阶段
    Engine->>Pool: 初始化连接 oa-adapter
    Pool->>MCP: 启动子进程 (python -m adapters.ad)
    MCP-->>Pool: MCP Initialize + 协议握手
    Pool->>MCP: list_tools()
    MCP-->>Pool: [{name:"create_account", description:"...", schema:{...}}]
    Pool-->>Engine: 连接就绪，工具清单已缓存

    Note over Engine: 用户请求阶段
    Engine->>Pool: call_tool("ad-adapter", "create_account", {emp_id, name})
    Pool->>MCP: JSON-RPC call_tool
    MCP->>OA: HTTP POST 创建账号
    OA-->>MCP: {account: "zhangsan"}
    MCP-->>Pool: ToolResult(content=[...])
    Pool-->>Engine: 解析结果，写入上下文
```

### 工程代码模块划分

```
oa_agent/
├── main.py
├── api/   
│   ├── __init__.py
│   ├── router.py                    # /chat 和 /execute 路由
│   └── schema.py                    # http请求和返回实例
├── processes/                       # YAML 流程定义目录
│   ├── employee_onboarding.yaml
│   └── employee_offboarding.yaml
├── agent/
│   ├── parser.py
│   └── models.py
├── orchestrator/
│   ├── __init__.py
│   ├── loader.py                    # YAML 热加载器 (watchdog)
│   ├── models.py                    # MySQLSQL 数据模型
│   ├── context.py                   # 上下文解析器
│   └── mcp_client.py                # ⭐ 新增：MCP Client 连接池
├── adapters/                        # ⭐ 每个适配层变为 MCP Server
│   ├── hr_adapter/
│   │   ├── mcp_server.py            # FastMCP 入口
│   │   ├── hr_client.py             # 原始 HR 系统调用
│   │   └── pyproject.toml
│   ├── ad_adapter/
│   │   ├── mcp_server.py
│   │   └── ad_client.py
│   └── shared/                      # 适配层公共库（日志、错误码）
│       └── schemas.py
├── core/
│   ├── models.py
│   ├── registry.py                  # AdapterRegistry 服务发现
│   ├── workflow_engine.py           # 通用 WorkflowEngine
│   └── rules.py                     # 轻量规则引擎
├── config.py
└── requirements.txt
```

### 埋坑点

1. **MCP Server 进程生命周期**：stdio 模式下，如果编排层重启，子进程会变成孤儿进程或僵尸进程。必须在  AdapterClientPool  里用  atexit  注册清理，或用进程组管 。
2. **Tool 描述决定 LLM 理解质量**： @mcp.tool()  的功能描述和参数类型注释必须写得极其清晰，否则 LLM 传错参数。建议每个 Tool 都配  examples 。
3. **错误信息传递链**：MCP Server 内部捕获的异常，必须包装成 MCP 标准的  ToolResult(isError=True) ，而不是直接抛异常导致连接断开。
4. **stdio 的并发限制**：stdio 传输是单通道，如果编排层同时发起 10 个 Tool Call，可能串行阻塞。高并发场景需切 SSE 或每个请求独立启动进程（但开销大）。

### 挑战点

- **调试复杂度陡增**：问题可能出在 MCP 协议层、JSON-RPC 层、适配层业务逻辑层、传统系统层。需要 MCP Inspector（官方工具）做协议抓包。
- **版本兼容性**：MCP 协议还在快速迭代。SDK 升级可能导致 Breaking Change，需要锁定版本。
- **第三方接入的安全边界**：如果允许直接连  oa-adapter ，等于给外部工具直接操作 OA 域控的能力。必须加独立的权限网关或只允许编排层白名单 IP 连接。

## 长程任务可靠性——异步、事件驱动与事物补偿

**目标**：支撑"提交即返回、数天后完成、失败可补偿、人工可介入"的企业级长程流程。

### 架构图

```mermaid
graph TB
    subgraph 交互层
        A1[Web/Chat Agent]
        A2[审批Portal]
        A3[运维告警]
    end
    
    subgraph 编排层
        B1[Agent+FastAPI<br/>主进程]
        B2[Sync执行器]
        B3[Async调度器]
        B4[事件接收器<br/>/api/v1/events]
    end
    
    subgraph 执行层
        C1[Celery/ARQ Worker]
        C2[超时扫描器<br/>APScheduler]
        C3[事件路由器<br/>Redis/RabbitMQ]
    end
    
    subgraph 适配层
        D1[hr-adapter<br/>MCP Server]
        D2[ad-adapter<br/>MCP Server<br/>+ Async Worker]
        D3[email-adapter<br/>MCP Server]
    end
    
    subgraph 资源层
        E1[OA域控]
        E2[CRM域控]
        E3[邮箱系统]
    end
    
    subgraph 基础设施
        F1[(MySQL<br/>状态持久化)]
        F2[(Redis<br/>消息队列/幂等)]
        F3[(对象存储<br/>审计日志)]
    end
    
    A1 --> B1
    A2 --> B4
    B1 --> B2
    B1 --> B3
    B2 -->|同步MCP| D1
    B3 -->|异步提交| D2
    B3 -->|写入| F1
    B3 -->|发布任务| C1
    C1 -->|后台执行| D2
    D2 -->|完成后| C3
    C3 -->|事件分发| B4
    B4 -->|恢复状态机| F1
    B4 -->|驱动下一步| B2
    C2 -->|扫描超时| F1
    C2 -->|触发告警| A3
    D1 --> E1
    D2 --> E2
    D3 --> E3
    B1 --> F2
    B1 --> F3
    
    style B4 fill:#f96,stroke:#333,stroke-width:2px
    style C2 fill:#f96,stroke:#333,stroke-width:2px
    style C3 fill:#f96,stroke:#333,stroke-width:2px
```

**说明**：新增"执行层"作为独立平面；编排层内部裂变为 Sync/Async/事件接收器三个子系统；状态机支持  WAITING_EVENT  和  HUMAN_APPROVAL  状态。

### 技术选型

| 层级     | 技术                      | 理由                                               |
| -------- | ------------------------- | -------------------------------------------------- |
| 任务队列 | Celery + Redis            | 成熟，Python 生态完善，支持延迟任务和重试          |
| 定时任务 | APScheduler               | 扫描超时实例、兜底轮询                             |
| 消息总线 | Redis Pub/Sub 或 RabbitMQ | 适配层完成事件后，通过消息队列通知编排层           |
| 状态存储 | MySQLSQL                  | 主状态；Redis 做分布式锁和幂等缓存                 |
| 补偿引擎 | 自研事物协调器            | 逆序调用补偿 Tool，记录补偿日志                    |
| 审批网关 | FastAPI 独立路由          | /api/v1/approvals/{instance_id} 供外部审批系统对接 |

### 模块功能设计

| 模块         | 职责                                         | 关键设计                                                     |
| ------------ | -------------------------------------------- | ------------------------------------------------------------ |
| Async 调度器 | 调用适配层异步 Tool，拿到 task_id 后立即挂起 | `AsyncScheduler.submit()`：返回后设置 `status=WAITING`       |
| 事件接收器   | 接收外部回调或消息队列事件，恢复流程         | `EventController.receive()`：通过 `task_id` 反查实例，注入上下文 |
| 人工审批节点 | 流程挂起，通知审批人，等待决策               | `HumanApprovalNode`：支持 `approved/rejected/timeout` 三种出口 |
| 事物补偿引擎 | 流程失败时，逆序执行已成功的补偿 Tool        | `CompensationEngine`：从数据库读取历史步骤，按完成时间倒序补偿 |
| 超时扫描器   | 定时扫描 `WAITING` 且超时的实例              | `TimeoutScanner`：APScheduler 每 10 分钟执行一次             |
| 幂等控制     | 防重复回调、防重复审批                       | Redis `SETNX` + 业务唯一键                                   |

### 数据流时序图

```mermaid
sequenceDiagram
    autonumber
    actor U as 用户
    participant Agent as Agent+编排层
    participant DB as PostgreSQL
    participant Celery as Celery Worker
    participant AD as ad-adapter<br/>MCP Server
    participant AD_Worker as AD后台Worker
    participant Queue as Redis队列
    participant Scanner as 超时扫描器

    U->>Agent: "给张三办入职"
    Agent->>Agent: 解析意图，加载 YAML
    Agent->>DB: 创建实例 status=RUNNING
    
    Note over Agent: Step 1: HR建档（同步，3秒完成）
    Agent->>Agent: 执行 create_hr（同步MCP）
    Agent->>DB: 更新上下文 context.create_hr={emp_id:9527}
    
    Note over Agent: Step 2: AD开户（长程，可能2天）
    Agent->>Agent: 执行 submit_ad_async
    Agent->>AD: MCP call_tool create_account_async
    AD-->>Agent: 返回 {task_id: "AD-9527", status: "accepted"}
    Agent->>DB: 更新实例<br/>status=WAITING_EVENT<br/>waiting_for=ad_account_created<br/>external_task_id=AD-9527<br/>timeout_at=48h后
    Agent-->>U: "AD账号已提交IT排期，预计2天完成，我会跟进"
    
    Note over Agent,Celery: 【编排层主进程可重启，不影响】
    
    Note over AD,AD_Worker: 【2天后，AD域控完成】
    AD_Worker->>AD: 内部完成账号创建
    AD->>Queue: 发布事件 {task_id:AD-9527, event:ad_account_created}
    Queue->>Celery: 消费者投递
    Celery->>Agent: POST /api/v1/events
    Agent->>DB: 根据 task_id 反查实例
    Agent->>DB: 校验 event_type 匹配
    Agent->>DB: 恢复上下文，status=RUNNING
    Agent->>Agent: 驱动下一步：manager_approval
    
    Note over Agent: Step 3: 经理审批（人工，可能1天）
    Agent->>Agent: 发送审批通知给经理王五
    Agent->>DB: status=WAITING_EVENT<br/>waiting_for=manager_approval
    Agent-->>U: "等待经理王五审批AD账号"
    
    Note over Agent: 【1天后，经理点击审批链接】
    actor M as 经理王五
    M->>Agent: POST /approvals/123 {decision:approved}
    Agent->>DB: 校验状态，记录审批历史
    Agent->>DB: status=RUNNING
    Agent->>Agent: 驱动下一步
    
    Note over Agent: Step 4: 开通门禁（同步，5秒）
    Agent->>Agent: 调用 access-adapter.grant()
    Agent->>DB: 更新上下文
    Agent->>DB: status=COMPLETED
    Agent-->>U: "张三入职全部完成！"
    
    Note over Scanner: 【兜底：如果48小时AD没回调】
    Scanner->>DB: 扫描 timeout_at < now()
    DB-->>Scanner: 实例123超时
    Scanner->>Agent: 触发超时处理
    Agent->>DB: status=TIMEOUT
    Agent->>Agent: 通知运维+用户
    Agent->>Agent: 可选：触发补偿或人工介入
```

### 工程代码模块划分

```
oa_agent/
├── main.py
├── api/   
│   │   ├── chat.py                  # 用户对话入口
│   │   ├── events.py                # ⭐ 外部事件回调入口
│   │   └── approvals.py             # ⭐ 人工审批提交入口
│   └── deps.py                      # 依赖注入（DB Session等）
├── processes/                       # YAML 流程定义目录
│   ├── employee_onboarding.yaml
│   └── employee_offboarding.yaml
├── agent/
│   ├── parser.py
│   └── models.py
├── orchestrator/
│   ├── __init__.py
│   ├── loader.py                    # YAML 热加载器 (watchdog)
│   ├── models.py                    # MySQLSQL 数据模型
│   ├── context.py                   # 上下文解析器
│   ├── mcp_client.py                # MCP Client 连接池
│   ├── async_scheduler.py           # ⭐ Async 调度器
│   └── event_receiver.py            # ⭐ 事件接收与恢复逻辑
├── adapters/                        # 每个适配层变为 MCP Server
│   ├── hr_adapter/
│   │   ├── mcp_server.py            # FastMCP 入口
│   │   ├── hr_client.py             # 原始 HR 系统调用
│   │   └── pyproject.toml
│   ├── ad_adapter/
│   │   ├── mcp_server.py
│   │   ├── ad_client.py
│   │   └── async_worker.py          # ⭐ 后台执行 + 完成后发事件
│   └── shared/                      # 适配层公共库（日志、错误码）
│       └── schemas.py
├── core/
│   ├── models.py
│   ├── registry.py                  # AdapterRegistry 服务发现
│   ├── workflow_engine.py           # 通用工作流引擎
│   ├── rules.py                     # 轻量规则引擎
│   ├── idempotency.py               # ⭐ 幂等控制（Redis SETNX）
│   ├── compensation.py              # ⭐ 事物补偿引擎
│   └── exceptions.py                # ⭐ 业务异常定义
├── workers/                         # ⭐ 新增：执行层 Worker
│   ├── celery_app.py                # Celery 应用实例
│   ├── event_consumer.py            # 消费 Redis 队列事件
│   ├── human_approval.py            # ⭐ 人工审批节点处理
│   └── timeout_scanner.py           # APScheduler 定时任务
├── config.py
└── requirements.txt
```

### 埋坑点

1. **回调幂等**：外部系统可能重复发送回调（网络重试）。事件接收器必须用  external_task_id  +  event_type  做幂等，处理过的回调直接返回  200 ，不重复驱动流程。
2. **脑裂恢复**：编排层多实例部署时，两个实例同时收到同一事件的回调，可能同时恢复流程导致并发执行。必须用数据库行锁（ SELECT FOR UPDATE ）或Redis 分布式锁保护恢复过程。
3. **补偿不是时光机**：有些操作无法撤销（如"已发送的邮件"、"已产生的审计日志"）。YAML 设计时必须区分可补偿步骤和不可补偿步骤，后者放在流程最后或加前置人工确认。
4. **超时与审批的竞态**：用户刚好在超时前 1 秒提交审批，扫描器和审批接口可能并发修改同一行。数据库更新必须用乐观锁（version 字段）或状态机校验（只允许  WAITING -> RUNNING ，不允许  TIMEOUT -> RUNNING ）。
5. **消息队列死信**：如果事件路由到 Celery 后，Worker 一直消费失败（如编排层接口 500），消息会进入死信队列。必须配置死信队列 + 告警，否则流程永远挂起。
6. **上下文膨胀**：长程任务执行数天，上下文（每一步的输入输出）可能变得很大。不要无限往  context  JSON 字段塞数据，大文件/日志应存对象存储，上下文只存引用 ID。

### 挑战点

- **分布式事务的最终一致性**：没有数据库 ACID 跨系统事务，全靠 Saga 补偿。补偿逻辑本身可能失败（如补偿时 AD 系统又挂了），需要补偿的补偿（人工兜底工单）。
- **长程调试的复杂性**：一个流程跑了 3 天，中间经历了重启、回调、审批，出问题时要像"查案"一样从  event_history  里还原现场。日志必须结构化 + Trace ID 全链路。
- **人工节点的 UX 设计**：审批人可能不用你的系统，而是在钉钉/企业微信/邮件里收到链接。审批网关必须支持免登/短链/移动端适配。
- **状态机的复杂度**：从 4 个状态（PENDING/RUNNING/COMPLETED/FAILED）扩展到 8+ 个状态，状态转换矩阵容易出错。建议用状态模式或状态机库（如  python-statemachine ）显式管理，不要写满地的  if-else 。
- **运维心智负担**：系统包含 FastAPI、Celery Worker、APScheduler、Redis、MySQL、多个 MCP Server 子进程。需要Docker Compose 或 K8s 统一编排，否则本地开发环境都起不来。

## 全局支撑体系

### 里程碑与验收标准

| 阶段  | 验收标准（必须全部通过）                                     |
| ----- | ------------------------------------------------------------ |
| 阶段1 | 1. 用户说一句"给张三办入职"，30 秒内得到成功回复；2. 数据库能查到完整的实例记录；3. 同一句话重复说，不会重复建账号。 |
| 阶段2 | 1. 新增一个"离职流程"只需新增 YAML 文件，不改 Python 代码；2. 适配层独立部署，编排层通过配置发现它；3. 热加载生效时间 < 5 秒。 |
| 阶段3 | 1. 适配层可用 `npx @anthropics/mcp-inspector` 直接调试；2. 编排层通过 MCP 调用适配层，而非私有 HTTP；3. Tool 的 docstring 能让 LLM 正确理解参数。 |
| 阶段4 | 1. 长程任务（模拟 10 分钟）提交后，用户立即收到"已受理"回复；2. 编排层重启后，流程能从 WAITING 状态正确恢复；3. 超时后自动触发补偿或告警；4. 人工审批拒绝后，已创建的账号被自动撤销。 |

### 监控告警设计

| 监控对象      | 指标                               | 告警阈值               |
| ------------- | ---------------------------------- | ---------------------- |
| 编排层 API    | 请求延迟 P99                       | 2s 告警                |
| 流程实例      | 处于 `WAITING` 超过 timeout 的数量 | 0 立即告警             |
| 适配层 MCP    | 进程存活状态                       | 进程消失立即告警       |
| Celery Worker | 任务积压数量                       | 100 告警               |
| 死信队列      | 未消费消息数                       | 0 告警                 |
| 补偿执行      | 补偿失败次数                       | 0 立即告警（人工介入） |

### 安全与权限

1. **Agent 层**：LLM 提取的参数必须经过 Pydantic Schema 校验，拒绝任何越界参数（如  dept="管理员"  但 Schema 里无此枚举值）。
2. **编排层**：操作前校验  created_by  的 RBAC 权限（如只有  hr_admin  能触发入职。
3. **适配层**：MCP Server 只暴露最小必要 Tool，高危操作（如  delete_account ）不暴露给外部 MCP Host，仅编排层白名单可调。
4. **审计**：所有  TaskInstance  的  input_params  和  output_result  必须落库，保存 180 天，支持按  operator  和  business_key  追溯。

### 回滚策略

| 场景                   | 回滚方案                                                     |
| ---------------------- | ------------------------------------------------------------ |
| YAML 配置错误          | 流程定义表支持 `version` 字段，发现错误后立即回滚到上一版本 YAML |
| 适配层发版失败         | 独立部署，蓝绿发布。编排层的 `AdapterRegistry` 标记不健康后自动流量切换 |
| 编排层发版失败         | 数据库状态兼容旧代码。回滚 Pod 后，WAITING 中的流程由新实例继续处理（无状态设计） |
| LLM 模型升级后效果变差 | Prompt 版本化存储，切换模型或 Prompt 版本只需改环境变量，重启生效 |