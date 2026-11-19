"""Telegram collectible gift lookup and diagnostics.

Uses the official MTProto payments.getSavedStarGifts method. A collectible NFT
is identified by the StarGiftUnique constructor and its unique slug.
"""
from __future__ import annotations

import inspect
from collections import Counter
from typing import Any

from telethon import __version__ as telethon_version
from telethon.tl import functions


REQUEST_FLAGS = (
    "exclude_unsaved",
    "exclude_saved",
    "exclude_unlimited",
    "exclude_unique",
    "exclude_upgradable",
    "exclude_unupgradable",
    "exclude_hosted",
)


def _gift_object(saved_gift: Any) -> Any:
    """SavedStarGift wraps the actual StarGift object in `.gift`."""
    return getattr(saved_gift, "gift", None) or saved_gift


def _is_unique_gift(gift: Any) -> bool:
    # Avoid matching ordinary StarGift objects just because of unrelated fields.
    return type(gift).__name__ == "StarGiftUnique"


async def inspect_profile_gifts(client, entity) -> dict:
    """Fetch all visible saved/unsaved gifts and return detailed diagnostics."""
    req_cls = getattr(functions.payments, "GetSavedStarGiftsRequest", None)
    if req_cls is None:
        raise RuntimeError(
            f"Telethon {telethon_version} не содержит payments.getSavedStarGifts. "
            "Нужно обновить Telethon до версии с актуальным TL-слоем."
        )

    # Resolve explicitly to InputPeerUser; this avoids passing a full User object
    # where a layer expects an InputPeer.
    peer = await client.get_input_entity(entity)
    signature = inspect.signature(req_cls)
    supported_flags = {
        name: False for name in REQUEST_FLAGS if name in signature.parameters
    }

    all_saved_gifts = []
    page_count = 0
    offsets_seen = set()
    offset = ""
    response_count = None
    while True:
        request = req_cls(
            peer=peer,
            offset=offset,
            limit=100,
            **supported_flags,
        )
        response = await client(request)
        page_count += 1
        if response_count is None:
            response_count = getattr(response, "count", None)
        batch = list(getattr(response, "gifts", None) or [])
        all_saved_gifts.extend(batch)
        next_offset = getattr(response, "next_offset", None)
        if not next_offset or not batch or next_offset in offsets_seen:
            break
        offsets_seen.add(next_offset)
        offset = next_offset
        # Guard against a broken API response looping forever.
        if page_count >= 100:
            break

    wrapper_types = Counter(type(item).__name__ for item in all_saved_gifts)
    gift_types = Counter(type(_gift_object(item)).__name__ for item in all_saved_gifts)
    unique_gifts = [
        _gift_object(item)
        for item in all_saved_gifts
        if _is_unique_gift(_gift_object(item))
    ]
    links = []
    for gift in unique_gifts:
        slug = getattr(gift, "slug", None)
        if slug:
            links.append(f"https://t.me/nft/{slug}")

    return {
        "telethon_version": telethon_version,
        "request_class": req_cls.__name__,
        "response_class": type(response).__name__ if page_count else None,
        "reported_count": response_count,
        "returned_items": len(all_saved_gifts),
        "pages": page_count,
        "wrapper_types": dict(wrapper_types),
        "gift_types": dict(gift_types),
        "unique_count": len(unique_gifts),
        "links": links,
        "pagination_limit_reached": page_count >= 100,
    }


async def get_profile_gifts(client, entity):
    """Compatibility wrapper used by the parser: return (unique_count, links)."""
    result = await inspect_profile_gifts(client, entity)
    return result["unique_count"], result["links"]
