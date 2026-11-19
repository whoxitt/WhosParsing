from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    bot_token: str = Field(default='', alias='BOT_TOKEN')
    bot_username: str = Field(default='', alias='BOT_USERNAME')
    admin_ids: str = Field(default='', alias='ADMIN_IDS')
    required_channel_id: str = Field(default='', alias='REQUIRED_CHANNEL_ID')
    required_channel_url: str = Field(default='', alias='REQUIRED_CHANNEL_URL')
    database_url: str = Field(default='', alias='DATABASE_URL')

    telegram_api_id: int = Field(default=0, alias='TELEGRAM_API_ID')
    telegram_api_hash: str = Field(default='', alias='TELEGRAM_API_HASH')
    telethon_session_dir: str = Field(default='/app/data/sessions', alias='TELETHON_SESSION_DIR')

    ton_network: str = Field(default='mainnet', alias='TON_NETWORK')
    ton_wallet_address: str = Field(default='', alias='TON_WALLET_ADDRESS')
    ton_api_base_url: str = Field(default='https://tonapi.io/v2', alias='TON_API_BASE_URL')
    ton_api_token: str = Field(default='', alias='TON_API_TOKEN')
    ton_price: float = Field(default=5, alias='ACCESS_PRICE_TON')
    payment_ttl_minutes: int = Field(default=30, alias='PAYMENT_EXPIRATION_MINUTES')
    payment_poll_seconds: int = Field(default=15, alias='PAYMENT_CHECK_INTERVAL_SECONDS')
    payment_memo_prefix: str = Field(default='NFTPARSER', alias='PAYMENT_MEMO_PREFIX')

    trial_max_profiles: int = Field(default=3, alias='TRIAL_MAX_PROFILES')
    access_hours: int = Field(default=24, alias='ACCESS_DURATION_HOURS')
    referral_reward_hours: int = Field(default=24, alias='REFERRAL_REWARD_HOURS')
    parser_max_results: int = Field(default=10, alias='PARSER_MAX_RESULTS')
    parser_cooldown_seconds: int = Field(default=300, alias='PARSER_COOLDOWN_SECONDS')
    parser_job_timeout_seconds: int = Field(default=900, alias='PARSER_MAX_SCAN_DURATION_SECONDS')
    parser_max_profile_checks_per_account: int = Field(default=2, alias='PARSER_MAX_CONCURRENT_PROFILE_CHECKS')
    parser_scan_messages_limit: int = Field(default=500, alias='PARSER_MAX_SCAN_MESSAGES_PER_CHAT')
    telethon_flood_wait_max_seconds: int = Field(default=3600, alias='TELETHON_FLOOD_WAIT_MAX_SECONDS')

    backup_enabled: bool = Field(default=True, alias='BACKUP_ENABLED')
    backup_dir: str = Field(default='/app/backups', alias='BACKUP_DIR')
    backup_interval_hours: int = Field(default=24, alias='BACKUP_INTERVAL_HOURS')
    backup_retention_days: int = Field(default=14, alias='BACKUP_RETENTION_DAYS')
    log_level: str = Field(default='INFO', alias='LOG_LEVEL')
    app_env: str = Field(default='production', alias='APP_ENV')

    @property
    def admins(self) -> set[int]:
        return {int(x.strip()) for x in self.admin_ids.split(',') if x.strip().isdigit()}

settings = Settings()
Path(settings.telethon_session_dir).mkdir(parents=True, exist_ok=True)
Path(settings.backup_dir).mkdir(parents=True, exist_ok=True)
