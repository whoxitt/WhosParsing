from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.db.session import Session
from app.db.models import User, ReferralReward
from app.config import settings

async def grant_access(user_id: int, hours: int):
    async with Session() as db:
        user=await db.get(User,user_id)
        if not user: return
        now=datetime.now(timezone.utc)
        base=user.access_until if user.access_until and user.access_until>now else now
        user.access_until=base+timedelta(hours=hours)
        await db.commit()

async def grant_referral(referee_id: int):
    async with Session() as db:
        user=await db.get(User,referee_id)
        if not user or not user.referred_by: return None
        existing=await db.scalar(select(ReferralReward).where(ReferralReward.referred_id==referee_id))
        if existing: return None
        reward=ReferralReward(referrer_id=user.referred_by,referred_id=referee_id,hours=settings.referral_reward_hours)
        db.add(reward)
        ref=await db.get(User,user.referred_by)
        if ref:
            now=datetime.now(timezone.utc); base=ref.access_until if ref.access_until and ref.access_until>now else now
            ref.access_until=base+timedelta(hours=settings.referral_reward_hours)
        await db.commit()
        return user.referred_by
