def format_user(username: str | None, user_id: int | None = None) -> str:
    """Consistent compact Telegram username label for bot/admin messages."""
    clean = (username or '').strip().lstrip('@')
    if clean:
        return f'@{clean}'
    return f'ID {user_id}' if user_id is not None else 'не указан'
