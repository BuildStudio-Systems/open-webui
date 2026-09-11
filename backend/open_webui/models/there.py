"""Canonical bindings and a durable operation journal in THERE's main database."""

from open_webui.internal.db import Base
from sqlalchemy import BigInteger, Column, ForeignKey, JSON, String, Text, UniqueConstraint


class ThereKnowledge(Base):
    __tablename__ = 'there_knowledge'

    id = Column(Text, ForeignKey('knowledge.id', ondelete='RESTRICT'), primary_key=True)
    engine_id = Column(Text, unique=True, nullable=True)
    state = Column(String(32), nullable=False)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


class ThereOperation(Base):
    __tablename__ = 'there_operation'
    __table_args__ = (UniqueConstraint('user_id', 'request_id', name='uq_there_operation_request'),)

    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False, index=True)
    request_id = Column(Text, nullable=False)
    resource_id = Column(Text, nullable=True, index=True)
    action = Column(String(64), nullable=False)
    state = Column(String(32), nullable=False)
    error_code = Column(String(64), nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


class ThereSkillOrigin(Base):
    __tablename__ = 'there_skill_origin'

    skill_id = Column(String, ForeignKey('skill.id', ondelete='CASCADE'), primary_key=True)
    catalog_id = Column(Text, nullable=False)
    digest = Column(String(80), nullable=False)
    version = Column(String(128), nullable=False)
    reviewed_by = Column(Text, nullable=False)
    reviewed_at = Column(BigInteger, nullable=False)


class TherePaper(Base):
    __tablename__ = 'there_paper'
    __table_args__ = (UniqueConstraint('user_id', 'url', name='uq_there_paper_user_url'),)

    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False, index=True)
    title = Column(Text, nullable=False)
    url = Column(String(2048), nullable=False)
    data = Column(JSON, nullable=False)
    created_at = Column(BigInteger, nullable=False)
