"""ORM 模型定义。

严格对应 ``db/smart_talkflow_init.sql`` 的 5 张表。遵循该脚本的设计:
所有表 / 字段均为「业务无关」的泛型标识。表间关联为「逻辑关联」而非
物理外键(日志类表需独立于业务记录存活),故 ID 列一律为普通 ``BigInteger``,
```

对象导航通过 ``relationship`` + ``primaryjoin``(配合 ``foreign()``)在
「无物理外键」的前提下建立,仅用于 ORM 层导航,不影响数据库约束。

.. note::

    关系属性默认惰性加载;异步会话中访问前需用 ``selectinload`` /
    ``joinedload`` 显式预加载,否则会触发隐式 IO 而报错。

用法::

    from infra.database import db_session
    from repository.models import Process

    async with db_session() as session:
        session.add(Process(process_key="onboarding", ...))
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class UpdatedAt:
    """为模型追加 ``updated_at`` 字段。"""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class RequestLog(Base):
    """用户请求 / 意图解析日志(对应 request_logs)。"""

    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_input: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_intent: Mapped[str | None] = mapped_column(String(64))
    parsed_params: Mapped[dict | None] = mapped_column(JSON)
    parse_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending"
    )
    clarification_question: Mapped[str | None] = mapped_column(Text)
    llm_model: Mapped[str | None] = mapped_column(String(64))
    llm_latency_ms: Mapped[int | None] = mapped_column(Integer)
    trace_ms: Mapped[int | None] = mapped_column(Integer)
    operator: Mapped[str | None] = mapped_column(String(64))

    # 一条请求最多产生一个流程实例(反问请求不产生);request_log_id 可空 → 0..1
    process: Mapped["Process | None"] = relationship(
        primaryjoin="foreign(Process.request_log_id) == RequestLog.id",
        back_populates="request_log",
    )


class Process(UpdatedAt, Base):
    """全流程执行记录(对应 process,核心表,承载幂等校验)。"""

    __tablename__ = "process"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    process_key: Mapped[str] = mapped_column(String(64), nullable=False)
    business_key: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending"
    )
    input_params: Mapped[dict | None] = mapped_column(JSON)
    context: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))
    request_log_id: Mapped[int | None] = mapped_column(BigInteger)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    request_log: Mapped["RequestLog | None"] = relationship(
        primaryjoin="foreign(Process.request_log_id) == RequestLog.id",
        back_populates="process",
    )
    steps: Mapped[list["ProcessStep"]] = relationship(
        primaryjoin="foreign(ProcessStep.process_id) == Process.id",
        back_populates="process",
    )
    adapter_call_logs: Mapped[list["AdapterCallLog"]] = relationship(
        primaryjoin="foreign(AdapterCallLog.process_id) == Process.id",
        back_populates="process",
    )


class ProcessStep(UpdatedAt, Base):
    """流程内单步执行记录(对应 process_step)。"""

    __tablename__ = "process_step"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    process_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    step_name: Mapped[str | None] = mapped_column(String(128))
    adapter: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending"
    )
    input_params: Mapped[dict | None] = mapped_column(JSON)
    output_result: Mapped[dict | None] = mapped_column(JSON)
    external_ref: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    compensation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="none"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    process: Mapped["Process"] = relationship(
        primaryjoin="foreign(ProcessStep.process_id) == Process.id",
        back_populates="steps",
    )
    adapter_call_logs: Mapped[list["AdapterCallLog"]] = relationship(
        primaryjoin="foreign(AdapterCallLog.step_execution_id) == ProcessStep.id",
        back_populates="step",
    )


class AdapterCallLog(Base):
    """对外部业务系统的 HTTP 调用留痕(对应 adapter_call_logs)。"""

    __tablename__ = "adapter_call_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    process_id: Mapped[int | None] = mapped_column(BigInteger)
    step_execution_id: Mapped[int | None] = mapped_column(BigInteger)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    target_system: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str | None] = mapped_column(String(128))
    method: Mapped[str | None] = mapped_column(String(8))
    http_status: Mapped[int | None] = mapped_column(Integer)
    request_payload: Mapped[dict | None] = mapped_column(JSON)
    response_payload: Mapped[dict | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    operator_id: Mapped[str | None] = mapped_column(String(64))  # 真实操作人
    tenant_id: Mapped[str | None] = mapped_column(String(64))  # 所属租户
    credential_source: Mapped[str | None] = mapped_column(String(64))  # 凭证来源

    # process_id / step_execution_id 均可空:部分调用不在流程上下文内(如健康检查)
    process: Mapped["Process | None"] = relationship(
        primaryjoin="foreign(AdapterCallLog.process_id) == Process.id",
        back_populates="adapter_call_logs",
    )
    step: Mapped["ProcessStep | None"] = relationship(
        primaryjoin="foreign(AdapterCallLog.step_execution_id) == ProcessStep.id",
        back_populates="adapter_call_logs",
    )


class AuditLog(Base):
    """通用操作审计(对应 audit_logs)。

    通过 ``resource_type`` + ``resource_id`` 多态指向任意资源,不与具体表绑定
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operator: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    business_key: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict | None] = mapped_column(JSON)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))


class WorkflowRole(Base):
    """工作流角色准入(层 A RBAC 配置,对应 ``workflow_role``)。

    一个 workflow 多个 role(多行);**无记录 = 全员可用**。运行时增删即生效
    (配合 :class:`~infra.permission.WorkflowRoleChecker` 的 invalidate / 缓存 TTL)。
    """

    __tablename__ = "workflow_role"
    __table_args__ = (
        UniqueConstraint("workflow_name", "role", name="uk_workflow_role"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workflow_name: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
