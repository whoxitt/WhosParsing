from datetime import datetime, timezone
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase): pass

def now(): return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user id
    username: Mapped[str | None] = mapped_column(String(64))
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False)
    trial_profiles_sent: Mapped[int] = mapped_column(Integer, default=0)
    access_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referred_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_parser_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class TgAccount(Base):
    __tablename__ = 'tg_accounts'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True)
    session_name: Mapped[str] = mapped_column(String(128), unique=True)
    api_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    api_hash_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default='disconnected')
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Chat(Base):
    __tablename__ = 'chats'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey('tg_accounts.id', ondelete='CASCADE'))
    peer_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cursor_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint('account_id','peer_id', name='uq_chat_account_peer'),)

class OwnerProfile(Base):
    __tablename__ = 'owner_profiles'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(255))
    gift_count: Mapped[int] = mapped_column(Integer, default=0)
    gift_links: Mapped[str | None] = mapped_column(Text)
    source_chat_title: Mapped[str | None] = mapped_column(String(255))
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    discovered_by_account_id: Mapped[int | None] = mapped_column(ForeignKey('tg_accounts.id'))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    issued_to_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey('users.id'))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index('ix_owner_unissued','issued_to_user_id','discovered_at'),)

class Payment(Base):
    __tablename__ = 'payments'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.id'))
    memo: Mapped[str] = mapped_column(String(128), unique=True)
    amount_nano: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), default='pending')
    tx_hash: Mapped[str | None] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class ReferralReward(Base):
    __tablename__ = 'referral_rewards'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.id'))
    referred_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('users.id'), unique=True)
    hours: Mapped[int] = mapped_column(Integer, default=24)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BotSetting(Base):
    __tablename__ = 'bot_settings'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
