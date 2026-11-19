"""Telegram collectible gift lookup.

Uses the official MTProto payments.getSavedStarGifts method through Telethon raw API.
Visibility/access is controlled by Telegram; failures are treated as inaccessible, not as zero gifts.
"""
from telethon.tl import functions

async def get_profile_gifts(client, entity):
    # Recent Telethon layers expose this request in payments namespace.
    req_cls = getattr(functions.payments, 'GetSavedStarGiftsRequest', None)
    if req_cls is None:
        raise RuntimeError('Installed Telethon layer does not expose payments.getSavedStarGifts; update Telethon layer.')
    gifts, offset = [], ''
    while True:
        result = await client(req_cls(peer=entity, offset=offset, limit=100))
        batch = getattr(result, 'gifts', []) or []
        gifts.extend(batch)
        offset = getattr(result, 'next_offset', None)
        if not offset or not batch: break
    collectible = []
    for gift in gifts:
        # StarGiftUnique is the collectible/unique gift constructor.
        g = getattr(gift, 'gift', None)
        if g and ('Unique' in type(g).__name__ or getattr(g, 'slug', None)):
            collectible.append(g)
    links = []
    for gift in collectible:
        slug = getattr(gift, 'slug', None)
        if slug: links.append('https://t.me/nft/' + slug)
    return len(collectible), links
