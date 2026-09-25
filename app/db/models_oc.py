from datetime import UTC, datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.compiles_types import SqliteCompatibleBigInteger
from app.db.models import CreatedAtUTCMixin, Group, ProxyInbound, User, fk_id_column


class OCIntegration(Base, CreatedAtUTCMixin):
    __tablename__ = "oc_integrations"
    id: Mapped[int] = mapped_column(SqliteCompatibleBigInteger, primary_key=True, init=False, autoincrement=True)
    base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    api_token_encrypted: Mapped[str] = mapped_column(String(2048), nullable=False)
    token_preview: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[dt] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: dt.now(UTC), onupdate=lambda: dt.now(UTC), init=False
    )

    panels: Mapped[list["OCPanel"]] = relationship(back_populates="integration", init=False, cascade="all, delete-orphan")


class OCPanel(Base, CreatedAtUTCMixin):
    __tablename__ = "oc_panels"
    
    id: Mapped[int] = mapped_column(SqliteCompatibleBigInteger, primary_key=True, init=False, autoincrement=True)
    integration_id: Mapped[int] = fk_id_column("oc_integrations.id", ondelete="CASCADE")
    source_panel_id: Mapped[str] = mapped_column(String(256), nullable=False)
    purchaser_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    default_multiplier: Mapped[float] = mapped_column(Numeric(6, 4), default=1.0000)
    test_user_id: Mapped[str | None] = mapped_column(String(256), default=None, nullable=True)
    sync_status: Mapped[str | None] = mapped_column(String(64), default=None, nullable=True)
    last_sync_at: Mapped[dt | None] = mapped_column(DateTime(timezone=True), default=None, nullable=True)
    updated_at: Mapped[dt] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: dt.now(UTC), onupdate=lambda: dt.now(UTC), init=False
    )

    integration: Mapped[OCIntegration] = relationship(back_populates="panels", init=False)
    groups: Mapped[list["OCPanelGroup"]] = relationship(back_populates="panel", init=False, cascade="all, delete-orphan")
    configs: Mapped[list["OCPanelConfig"]] = relationship(back_populates="panel", init=False, cascade="all, delete-orphan")
    user_mappings: Mapped[list["OCUserMapping"]] = relationship(back_populates="panel", init=False, cascade="all, delete-orphan")


class OCPanelGroup(Base, CreatedAtUTCMixin):
    __tablename__ = "oc_panel_groups"
    __table_args__ = (UniqueConstraint("panel_id", "source_group_id"),)
    
    id: Mapped[int] = mapped_column(SqliteCompatibleBigInteger, primary_key=True, init=False, autoincrement=True)
    panel_id: Mapped[int] = fk_id_column("oc_panels.id", ondelete="CASCADE")
    source_group_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_name: Mapped[str] = mapped_column(String(256), nullable=False)
    is_selected: Mapped[bool] = mapped_column(default=False)
    local_group_id: Mapped[int | None] = fk_id_column("groups.id", ondelete="SET NULL", nullable=True, default=None)
    updated_at: Mapped[dt] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: dt.now(UTC), onupdate=lambda: dt.now(UTC), init=False
    )

    panel: Mapped[OCPanel] = relationship(back_populates="groups", init=False)
    local_group: Mapped[Group | None] = relationship(init=False)
    configs: Mapped[list["OCPanelConfig"]] = relationship(back_populates="panel_group", init=False, cascade="all, delete-orphan")


class OCPanelConfig(Base, CreatedAtUTCMixin):
    __tablename__ = "oc_panel_configs"
    __table_args__ = (UniqueConstraint("panel_id", "source_config_id"),)
    
    id: Mapped[int] = mapped_column(SqliteCompatibleBigInteger, primary_key=True, init=False, autoincrement=True)
    panel_id: Mapped[int] = fk_id_column("oc_panels.id", ondelete="CASCADE")
    source_config_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_name: Mapped[str] = mapped_column(String(256), nullable=False)
    panel_group_id: Mapped[int | None] = fk_id_column("oc_panel_groups.id", ondelete="CASCADE", nullable=True, default=None)
    protocol: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    network: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    port: Mapped[int | None] = mapped_column(nullable=True, default=None)
    source_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    virtual_inbound_tag: Mapped[str | None] = mapped_column(
        String(256), ForeignKey("inbounds.tag", ondelete="SET NULL", onupdate="CASCADE"), nullable=True, default=None
    )
    source_missing: Mapped[bool] = mapped_column(default=False)
    locally_hidden: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[dt] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: dt.now(UTC), onupdate=lambda: dt.now(UTC), init=False
    )

    panel: Mapped[OCPanel] = relationship(back_populates="configs", init=False)
    panel_group: Mapped[OCPanelGroup | None] = relationship(back_populates="configs", init=False)
    virtual_inbound: Mapped[ProxyInbound | None] = relationship(init=False)


class OCUserMapping(Base, CreatedAtUTCMixin):
    __tablename__ = "oc_user_mappings"
    __table_args__ = (UniqueConstraint("user_id", "panel_id"),)
    
    id: Mapped[int] = mapped_column(SqliteCompatibleBigInteger, primary_key=True, init=False, autoincrement=True)
    user_id: Mapped[int] = fk_id_column("users.id", ondelete="CASCADE")
    panel_id: Mapped[int] = fk_id_column("oc_panels.id", ondelete="CASCADE")
    external_user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    last_cumulative_traffic: Mapped[int] = mapped_column(BigInteger, default=0)
    last_synced_at: Mapped[dt | None] = mapped_column(DateTime(timezone=True), default=None, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="active")
    
    user: Mapped[User] = relationship(init=False)
    panel: Mapped[OCPanel] = relationship(back_populates="user_mappings", init=False)


class OCSyncState(Base, CreatedAtUTCMixin):
    __tablename__ = "oc_sync_states"
    
    id: Mapped[int] = mapped_column(SqliteCompatibleBigInteger, primary_key=True, init=False, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(256), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    last_error: Mapped[str | None] = mapped_column(String(2048), nullable=True, default=None)
    next_retry_at: Mapped[dt | None] = mapped_column(DateTime(timezone=True), default=None, nullable=True)
    updated_at: Mapped[dt] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: dt.now(UTC), onupdate=lambda: dt.now(UTC), init=False
    )
