-- ============================================================================
-- smart_talkflow 第一阶段(MVP)数据库初始化脚本
-- 对应文档:传统业务系统接入 Agent 落地计划 v1.0.md —— 第一阶段:同步单流程跑通
-- ----------------------------------------------------------------------------
-- 【设计原则】
--   本平台是「对接各种传统业务系统的通用 Agent 编排平台」,业务无关。
--   因此:
--     1. 业务实体数据(员工、部门、身份证号、邮箱账号……)一律归各传统业务
--        系统所有,本平台「调用」而非「复制」,不建立任何业务主数据表。
--     2. 本平台只持久化「编排 / 执行 / 审计」三类平台数据。
--     3. 凡业务相关的标识(process_key / business_key / adapter / workflow)均为
--        泛型字符串,其「具体含义」由运行时的流程定义决定,而非由表结构绑定。
--     4. 所有数据可能流经 PII(如身份证号会作为 business_key 或参数出现),
--        生产环境需在应用层做脱敏 / 加密,表结构不针对单一敏感字段。
--
-- 【环境要求】 MySQL 5.7+(使用 JSON 类型);推荐 8.0+。
-- 【执行方式】 mysql -uroot -p < smart_talkflow_init.sql
--
-- 【表清单】(5 张,全部业务无关)
--   1. request_logs             用户请求 / 意图解析日志(含缺参反问记录)
--   2. process                  流程执行(核心,承载幂等校验)
--   3. process_step  流程内单步执行记录(为阶段4 补偿预留字段)
--   4. adapter_call_logs        对外部业务系统的每次 HTTP 调用留痕
--   5. audit_logs               通用操作审计(按 operator / business_key 追溯)
--
-- 【后续阶段演进提示】(本脚本不创建,仅标注,避免过度设计)
--   阶段2 新增:process_definitions(YAML 流程定义缓存)、adapter_registry(适配层注册表)
--   阶段3 新增:MCP 工具元数据缓存(可并入 adapter_registry)
--   阶段4 新增:approvals(人工审批)、event_history(外部事件)、compensation_logs(补偿日志)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 0. 库与基础设置
-- ----------------------------------------------------------------------------
-- 强制当前会话使用 utf8mb4:确保中文表/列注释在导入时不因客户端默认字符集
-- 差异而乱码。覆盖 docker 容器首次启动自动执行 / 手动 mysql < xxx.sql / GUI
-- 工具导入等所有场景(--character-set-server 只管 server 端,不管连接握手)。
SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS smart_talkflow
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE smart_talkflow;

-- ----------------------------------------------------------------------------
-- 1. request_logs —— 用户请求 / 意图解析日志
--    对应模块:engine/intent_parser.py (IntentParser.parse) + api/router.py (/chat)
--    用途:记录每条用户输入与其解析结果;度量 LLM 解析准确率(挑战点:>90%);
--         追溯缺参反问链路。一条请求不一定产生流程实例(可能仅反问)。
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS request_logs;
CREATE TABLE request_logs (
    id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    request_id           VARCHAR(64)  NOT NULL COMMENT '请求唯一标识(对外暴露,用于幂等去重 / 链路追溯)',
    user_input           TEXT         NOT NULL COMMENT '用户原始自然语言输入',
    parsed_intent        VARCHAR(64)  NULL COMMENT '解析出的意图 / 流程标识(如 onboarding);未识别时为空',
    parsed_params        JSON         NULL COMMENT '解析出的结构化参数(LLM 输出,可能不全,需经 Pydantic 校验)',
    parse_status         VARCHAR(32)  NOT NULL DEFAULT 'pending' COMMENT '解析状态:resolved=参数齐全可执行 / needs_clarification=缺参需反问 / unrecognized=未识别意图 / failed=解析异常',
    clarification_question TEXT       NULL COMMENT '反问内容(parse_status=needs_clarification 时填)',
    llm_model            VARCHAR(64)  NULL COMMENT '实际调用的 LLM 模型(可观测:效果回溯 / 成本归因)',
    llm_latency_ms       INT UNSIGNED NULL COMMENT 'LLM 解析耗时(毫秒)',
    trace_ms             INT UNSIGNED NULL COMMENT '全链路耗时(毫秒)',
    operator             VARCHAR(64)  NULL COMMENT '操作人标识(对接 RBAC,如 hr_admin)',
    created_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_request_id (request_id),
    KEY idx_parsed_intent (parsed_intent),
    KEY idx_operator (operator),
    KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户请求 / 意图解析日志:记录输入与解析结果,度量解析准确率';

-- ----------------------------------------------------------------------------
-- 2. process —— 全流程执行记录(核心表 + 幂等)
--    对应模块:infra/schema.py (ProcessInstance) + infra/idempotency.py
--    用途:一次意图触发的完整执行实例;承载流程级幂等校验。
--    幂等:UNIQUE(process_key, business_key) —— 同一流程对同一业务键不重复执行。
--         business_key 含义由业务决定:入职场景=身份证号,离职场景=工号,均为运行时值。
--         执行前需先通过适配器从外部业务系统补全 business_key,再做幂等校验。
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS process;
CREATE TABLE process (
    id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    process_key          VARCHAR(64)  NOT NULL COMMENT '流程标识(泛型:如 onboarding / offboarding / leave_request ……,值由流程定义决定)',
    business_key         VARCHAR(128) NOT NULL COMMENT '业务唯一键(泛型:值含义由流程决定,如身份证号 / 工单号;可能含敏感信息)',
    idempotency_key      VARCHAR(160) NOT NULL COMMENT '幂等键(约定 {process_key}_{business_key},便于直接查询)',
    status               VARCHAR(32)  NOT NULL DEFAULT 'pending' COMMENT '实例状态:pending / running / completed / failed',
    input_params         JSON         NULL COMMENT '执行入参(解析并补全后的最终参数,区别于 request_logs.parsed_params)',
    context              JSON         NULL COMMENT '执行上下文:各步骤中间产物(如 create_emp.emp_id 等),跨步骤传递',
    result               JSON         NULL COMMENT '最终执行结果(completed 时回填)',
    error_message        TEXT         NULL COMMENT '失败原因(failed 时填)',
    created_by           VARCHAR(64)  NULL COMMENT '触发人(RBAC 审计依据)',
    request_log_id       BIGINT UNSIGNED NULL COMMENT '关联的请求日志ID(一条请求最多产生一个实例,反问请求不产生)',
    trace_id             VARCHAR(64)  NULL COMMENT '全链路追踪ID',
    started_at           DATETIME     NULL COMMENT '执行开始时间',
    finished_at          DATETIME     NULL COMMENT '执行结束时间(completed / failed 时回填)',
    heartbeat_at         DATETIME     NULL COMMENT '心跳时间:执行中每步续期,长期无更新=协程失联',
    operator_context     JSON         NULL COMMENT '操作人身份(user_id/roles/tenant):失联重跑时重建身份',
    created_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_process_business (process_key, business_key) COMMENT '流程级幂等约束:同一流程对同一业务键禁止重复执行',
    UNIQUE KEY uk_idempotency_key (idempotency_key) COMMENT '幂等键唯一',
    KEY idx_status (status),
    KEY idx_created_by (created_by),
    KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='全流程执行:一次意图触发的完整执行记录,承载幂等校验';

-- ----------------------------------------------------------------------------
-- 3. process_step —— 流程内单步执行记录
--    对应模块:orchestrator/onboarding.py 各 Step + 埋坑点4(预留 # TODO: compensate)
--    用途:记录流程内每一步(如 建档/开户/授权/邮箱)的输入输出与状态;
--         为阶段4 Saga 补偿预留 compensation_status;失败时据此人工介入。
--    说明:adapter / workflow 均为泛型标识,不绑定具体业务系统或动作。
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS process_step;
CREATE TABLE process_step (
    id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    process_id  BIGINT UNSIGNED NOT NULL COMMENT '所属流程实例ID',
    step_no              INT          NOT NULL COMMENT '步骤序号(执行顺序,从1开始)',
    step_key             VARCHAR(64)  NOT NULL COMMENT '步骤标识(泛型:如 create_employee / create_account / grant_permissions / create_mailbox)',
    step_name            VARCHAR(128) NULL COMMENT '步骤展示名(便于人工查阅,如「开通邮箱」)',
    adapter              VARCHAR(64)  NULL COMMENT '调用的适配器标识(泛型:如 oa / email / crm)',
    action               VARCHAR(64)  NULL COMMENT '适配器动作标识(泛型:如 create / grant / disable)',
    status               VARCHAR(32)  NOT NULL DEFAULT 'pending' COMMENT '步骤状态:pending / running / completed / failed / skipped',
    input_params         JSON         NULL COMMENT '步骤输入参数',
    output_result        JSON         NULL COMMENT '步骤输出结果',
    result_data          JSON         NULL COMMENT '步产出/结果数据(extract 提取的业务结果,如 bookingId/emp 对象;只有 yields 的步写,供 recovery 重建 ref)',
    error_message        TEXT         NULL COMMENT '失败原因',
    duration_ms          INT UNSIGNED NULL COMMENT '执行耗时(毫秒,埋坑点3:同步阻塞监控依据)',
    compensation_status  VARCHAR(32)  NOT NULL DEFAULT 'none' COMMENT '补偿状态:none=未补偿 / done=已补偿 / failed=补偿失败',
    started_at           DATETIME     NULL COMMENT '步骤开始时间',
    finished_at          DATETIME     NULL COMMENT '步骤结束时间',
    created_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (id),
    KEY idx_step (process_id, step_no),
    KEY idx_status (status),
    KEY idx_compensation (compensation_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='流程单步执行记录:每步留痕,记录中间产物,为 Saga 补偿铺路';

-- ----------------------------------------------------------------------------
-- 4. adapter_call_logs —— 对外部业务系统的调用留痕
--    对应模块:adapters/* 适配层 + 埋坑点3(同步阻塞)/ 挑战点(传统系统接口文档缺失)
--    用途:记录对传统业务系统(OA / AD / 邮箱 / CRM ……)的每次 HTTP 调用,
--         支撑接口排查、耗时分析、故障定位。与步骤记录解耦:一次步骤可能含多次调用。
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS adapter_call_logs;
CREATE TABLE adapter_call_logs (
    id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    process_id           BIGINT UNSIGNED NULL COMMENT '关联流程(可空:部分调用可能不在流程上下文内,如健康检查)',
    step_execution_id    BIGINT UNSIGNED NULL COMMENT '关联步骤执行记录(可空)',
    adapter              VARCHAR(64)  NOT NULL COMMENT '适配器标识(泛型:oa / email / crm)',
    target_system        VARCHAR(64)  NOT NULL COMMENT '目标业务系统(泛型:如 oa / ad / email / crm)',
    action               VARCHAR(128) NULL COMMENT '动作 / 端点标识(泛型,如 create_account)',
    method               VARCHAR(8)   NULL COMMENT 'HTTP 方法(GET / POST ……)',
    http_status          INT          NULL COMMENT 'HTTP 状态码',
    request_payload      JSON         NULL COMMENT '请求载荷(注意脱敏:可能含敏感参数)',
    response_payload     JSON         NULL COMMENT '响应载荷(注意脱敏)',
    error_message        TEXT         NULL COMMENT '错误信息(超时 / 业务错误码等)',
    duration_ms          INT UNSIGNED NULL COMMENT '调用耗时(毫秒,埋坑点3:严格 10s 超时的监控依据)',
    trace_id             VARCHAR(64)  NULL COMMENT '全链路追踪ID',
    operator_id          VARCHAR(64)  NULL COMMENT '真实操作人(代签自 X-Operator-Userid)',
    tenant_id            VARCHAR(64)  NULL COMMENT '所属租户(代签自 operator.tenant_id)',
    credential_source    VARCHAR(64)  NULL COMMENT '凭证来源(如 service_account_delegated)',
    created_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    KEY idx_process (process_id),
    KEY idx_step (step_execution_id),
    KEY idx_adapter_target (adapter, target_system),
    KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='适配器对外部业务系统的调用留痕:接口排查 / 耗时分析 / 故障定位';

-- ----------------------------------------------------------------------------
-- 5. audit_logs —— 通用操作审计
--    对应模块:全局支撑体系「安全与权限 → 审计」
--    用途:记录平台级操作(谁在何时对什么资源做了什么),支持按 operator 与
--         business_key 追溯;建议保留 180 天。步骤级 input/output 见步骤执行表。
--    说明:resource_type / resource_id 为泛型,审计任意资源(实例 / 适配器 / 流程定义 ……),
--         不与具体业务绑定。审计表独立,不强加外键,保证日志不可因业务记录删除而丢失。
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS audit_logs;
CREATE TABLE audit_logs (
    id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    operator             VARCHAR(64)  NOT NULL COMMENT '操作人',
    action               VARCHAR(64)  NOT NULL COMMENT '操作类型(泛型:如 process_execute / adapter_call / config_change)',
    resource_type        VARCHAR(64)  NULL COMMENT '资源类型(泛型:如 process / adapter / process_definition)',
    resource_id          VARCHAR(128) NULL COMMENT '资源标识',
    business_key         VARCHAR(128) NULL COMMENT '业务唯一键(便于按业务追溯;值含义由业务决定)',
    detail               JSON         NULL COMMENT '操作详情摘要',
    ip_address           VARCHAR(64)  NULL COMMENT '操作来源IP',
    trace_id             VARCHAR(64)  NULL COMMENT '全链路追踪ID',
    created_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    KEY idx_operator (operator),
    KEY idx_business_key (business_key),
    KEY idx_resource (resource_type, resource_id),
    KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='通用操作审计:按 operator / business_key 追溯,建议保留 180 天';

-- ----------------------------------------------------------------------------
-- 工作流角色准入(层 A RBAC 配置,运行时可改):每个流程放行哪些角色。
-- 运维通过增删行调整权限,改后调 invalidate 或等缓存 TTL 过期。
-- 无记录 = 全员可用(不限制)。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workflow_role (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    workflow_name VARCHAR(64) NOT NULL COMMENT '工作流名称(对应 workflow.name)',
    `role` VARCHAR(64) NOT NULL COMMENT '允许触发的角色(平台 RBAC role,来自 SSO)',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_workflow_role (workflow_name, role),
    KEY idx_workflow (workflow_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='层 A RBAC:工作流角色准入(动态配置)';

-- ============================================================================
-- 结束:本脚本不含业务种子数据(平台业务无关,部门 / 员工等归各业务系统)。
-- 验证场景(如入职)所需的外部业务数据,请在对应传统业务系统中准备。
-- ============================================================================
