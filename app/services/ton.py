import aiohttp
import base64
import binascii
import logging
import secrets
import string
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.config import settings
from app.db.models import Payment, User, ReferralReward
from app.db.session import Session

logger = logging.getLogger(__name__)


class TonService:
    def new_memo(self):
        alphabet = string.ascii_uppercase + string.digits
        return settings.payment_memo_prefix + '-' + ''.join(secrets.choice(alphabet) for _ in range(16))

    async def create_invoice(self, user_id: int):
        from app.services.bot_settings import get_value
        price = await get_value('ton_price')
        nano = int(price) * 1_000_000_000
        memo = self.new_memo()
        async with Session() as db:
            p = Payment(
                user_id=user_id,
                memo=memo,
                amount_nano=nano,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.payment_ttl_minutes),
            )
            db.add(p)
            await db.commit()
        return memo, price

    async def recent_transactions(self):
        if not settings.ton_wallet_address or not settings.ton_api_token:
            raise RuntimeError('TON_WALLET_ADDRESS или TON_API_TOKEN не заданы')
        url = f"{settings.ton_api_base_url.rstrip('/')}/blockchain/accounts/{settings.ton_wallet_address}/transactions"
        headers = {'Authorization': f'Bearer {settings.ton_api_token}'}
        params = {'limit': 100, 'sort_order': 'desc'}
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, params=params) as response:
                body = await response.text()
                if response.status >= 400:
                    logger.error('TonAPI HTTP %s: %s', response.status, body[:1000])
                    response.raise_for_status()
                try:
                    data = await response.json()
                except Exception as exc:
                    raise RuntimeError(f'TonAPI вернул не JSON: {body[:300]}') from exc
        rows = data.get('transactions') if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError(f'Неожиданный формат ответа TonAPI: {str(data)[:500]}')
        return rows

    @staticmethod
    def _walk_text_values(value):
        """Read decoded text comments across TonAPI decoded-body variants."""
        preferred = {'text', 'comment', 'message'}
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in preferred and isinstance(item, str) and item.strip():
                    yield item.strip()
                elif isinstance(item, (dict, list)):
                    yield from TonService._walk_text_values(item)
        elif isinstance(value, list):
            for item in value:
                yield from TonService._walk_text_values(item)

    @staticmethod
    def extract_comment(tx):
        msg = tx.get('in_msg') or {}
        if not isinstance(msg, dict):
            return None
        for container in (msg.get('decoded_body'), msg.get('message_content'), msg):
            if container is not None:
                for value in TonService._walk_text_values(container):
                    return value
        return None

    @staticmethod
    def tx_hash(tx):
        transaction_id = tx.get('transaction_id') or {}
        return tx.get('hash') or (transaction_id.get('hash') if isinstance(transaction_id, dict) else None)

    @staticmethod
    def tx_amount_nano(tx):
        msg = tx.get('in_msg') or {}
        try:
            return int(msg.get('value'))
        except (TypeError, ValueError, AttributeError):
            return None

    @staticmethod
    def tx_destination(tx):
        msg = tx.get('in_msg') or {}
        return msg.get('destination') if isinstance(msg, dict) else None

    @staticmethod
    def normalize_address(address):
        """Normalize raw workchain:hex and user-friendly TON addresses to the same key."""
        if not isinstance(address, str) or not address.strip():
            return None
        address = address.strip()
        if ':' in address:
            try:
                workchain, account = address.split(':', 1)
                account = account.strip()
                if len(account) != 64:
                    return None
                return int(workchain), bytes.fromhex(account)
            except (ValueError, TypeError):
                return None
        try:
            padded = address.replace('-', '+').replace('_', '/')
            padded += '=' * ((4 - len(padded) % 4) % 4)
            raw = base64.b64decode(padded, validate=True)
            if len(raw) != 36:
                return None
            payload, checksum = raw[:34], raw[34:]
            expected = binascii.crc_hqx(payload, 0).to_bytes(2, 'big')
            if checksum != expected:
                return None
            tag = payload[0] & 0x7F
            if tag not in (0x11, 0x51):
                return None
            workchain = int.from_bytes(payload[1:2], 'big', signed=True)
            return workchain, payload[2:34]
        except (ValueError, binascii.Error):
            return None

    @staticmethod
    def same_address(left, right):
        a = TonService.normalize_address(left)
        b = TonService.normalize_address(right)
        return a is not None and b is not None and a == b

    async def poll(self, bot):
        """Verify incoming TON transfers and atomically activate the purchased access."""
        txs = await self.recent_transactions()
        if TonService.normalize_address(settings.ton_wallet_address) is None:
            raise RuntimeError('TON_WALLET_ADDRESS имеет неверный формат')

        confirmed_count = 0
        for tx in txs:
            if not isinstance(tx, dict):
                continue
            # TonAPI marks failed/aborted transactions explicitly. Never credit them.
            if tx.get('success') is False or tx.get('aborted') is True:
                continue
            memo = self.extract_comment(tx)
            tx_hash = self.tx_hash(tx)
            amount = self.tx_amount_nano(tx)
            destination = self.tx_destination(tx)
            if not memo or not tx_hash or amount is None:
                continue
            if not self.same_address(destination, settings.ton_wallet_address):
                continue

            from app.services.bot_settings import get_value
            access_hours = await get_value('access_hours')
            now = datetime.now(timezone.utc)
            user_id = None
            username = None
            referral_user_id = None
            referral_username = None
            async with Session() as db:
                payment = await db.scalar(
                    select(Payment)
                    .where(Payment.memo == memo, Payment.status == 'pending')
                    .with_for_update()
                )
                if not payment or payment.expires_at < now:
                    continue
                if amount != payment.amount_nano:
                    logger.info('TON amount mismatch for invoice %s: got=%s expected=%s', payment.id, amount, payment.amount_nano)
                    continue
                if await db.scalar(select(Payment.id).where(Payment.tx_hash == tx_hash)):
                    continue
                user = await db.get(User, payment.user_id)
                if not user:
                    logger.error('TON payment %s belongs to missing user %s', payment.id, payment.user_id)
                    continue

                # Payment confirmation and access grant are committed together.
                payment.status = 'confirmed'
                payment.tx_hash = tx_hash
                payment.confirmed_at = now
                base = user.access_until if user.access_until and user.access_until > now else now
                user.access_until = base + timedelta(hours=access_hours)
                user_id = user.id
                username = user.username

                # Referral reward is also idempotent and shares this transaction.
                if user.referred_by:
                    existing_reward = await db.scalar(
                        select(ReferralReward.id).where(ReferralReward.referred_id == user.id)
                    )
                    if not existing_reward:
                        from app.config import settings as app_settings
                        reward_hours = app_settings.referral_reward_hours
                        db.add(ReferralReward(
                            referrer_id=user.referred_by,
                            referred_id=user.id,
                            hours=reward_hours,
                        ))
                        referrer = await db.get(User, user.referred_by)
                        if referrer:
                            ref_base = referrer.access_until if referrer.access_until and referrer.access_until > now else now
                            referrer.access_until = ref_base + timedelta(hours=reward_hours)
                            referral_user_id = referrer.id
                            referral_username = referrer.username
                payment_id = payment.id
                await db.commit()

            confirmed_count += 1
            from app.services.formatting import format_user
            from app.services.notifications import notify_admins
            try:
                await bot.send_message(user_id, f'Оплата подтверждена. Доступ активирован на {access_hours} ч.')
            except Exception:
                logger.warning('Could not send payment confirmation to user %s', user_id, exc_info=True)
            await notify_admins(
                'Покупка доступа подтверждена\n'
                f'Юз: {format_user(username, user_id)}\n'
                f'Сумма: {amount / 1_000_000_000:g} TON\n'
                f'Платёж: #{payment_id}\n'
                f'TX: {tx_hash}'
            )
            if referral_user_id:
                await notify_admins(f'Реферальная награда начислена\nЮз: {format_user(referral_username, referral_user_id)}\nНачислено: {settings.referral_reward_hours} ч.')
        return confirmed_count
