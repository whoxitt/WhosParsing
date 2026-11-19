import aiohttp
import secrets
import string
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.config import settings
from app.db.models import Payment
from app.db.session import Session

class TonService:
    def new_memo(self):
        alphabet = string.ascii_uppercase + string.digits
        return settings.payment_memo_prefix + '-' + ''.join(secrets.choice(alphabet) for _ in range(16))

    async def create_invoice(self, user_id: int):
        memo = self.new_memo()
        nano = int(settings.ton_price * 1_000_000_000)
        async with Session() as db:
            p = Payment(user_id=user_id, memo=memo, amount_nano=nano,
                        expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.payment_ttl_minutes))
            db.add(p)
            await db.commit()
        return memo, settings.ton_price

    async def recent_transactions(self):
        if not settings.ton_wallet_address or not settings.ton_api_token:
            return []
        url = f"{settings.ton_api_base_url.rstrip('/')}/blockchain/accounts/{settings.ton_wallet_address}/transactions"
        headers = {'Authorization': f'Bearer {settings.ton_api_token}'}
        params = {'limit': 100}
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, params=params) as response:
                response.raise_for_status()
                data = await response.json()
        # TonAPI currently returns a transactions array. Fail closed on unexpected shape.
        rows = data.get('transactions') if isinstance(data, dict) else None
        return rows if isinstance(rows, list) else []

    @staticmethod
    def extract_comment(tx):
        msg = tx.get('in_msg') or {}
        # Accept only a decoded text comment that exactly equals our invoice memo.
        for container in (msg, msg.get('decoded_body') or {}, msg.get('message_content') or {}):
            if not isinstance(container, dict):
                continue
            for key in ('comment', 'text', 'message'):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def tx_hash(tx):
        return tx.get('hash') or (tx.get('transaction_id') or {}).get('hash')

    @staticmethod
    def tx_amount_nano(tx):
        msg = tx.get('in_msg') or {}
        value = msg.get('value')
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def tx_destination(tx):
        return ((tx.get('in_msg') or {}).get('destination'))

    async def poll(self, bot):
        """Fail-closed TonAPI verifier. Unexpected/ambiguous API records are never credited."""
        txs = await self.recent_transactions()
        for tx in txs:
            memo = self.extract_comment(tx)
            if not memo:
                continue
            tx_hash = self.tx_hash(tx)
            amount = self.tx_amount_nano(tx)
            destination = self.tx_destination(tx)
            if not tx_hash or amount is None:
                continue
            # Exact destination comparison is intentional. If TonAPI returns a different
            # address representation, this payment is not credited automatically.
            if not destination or destination != settings.ton_wallet_address:
                continue
            async with Session() as db:
                p = await db.scalar(select(Payment).where(Payment.memo == memo, Payment.status == 'pending').with_for_update())
                if not p or p.expires_at < datetime.now(timezone.utc):
                    continue
                if amount != p.amount_nano:
                    continue
                # The tx_hash unique constraint prevents one chain transaction being reused.
                already = await db.scalar(select(Payment.id).where(Payment.tx_hash == tx_hash))
                if already:
                    continue
                p.status = 'confirmed'
                p.tx_hash = tx_hash
                p.confirmed_at = datetime.now(timezone.utc)
                await db.commit()
                from app.services.access import grant_access, grant_referral
                await grant_access(p.user_id, settings.access_hours)
                await grant_referral(p.user_id)
                try:
                    await bot.send_message(p.user_id, 'Оплата подтверждена. Доступ активирован на 24 часа.')
                except Exception:
                    pass
