from __future__ import annotations

from typing import Any


async def delete_message_safely(message: Any | None) -> bool:
    if message is None:
        return False
    try:
        await message.delete()
        return True
    except Exception:
        return False
