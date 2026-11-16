"""
LoopBridge Production Config — προσαρμοσμένο για τον Hetzner server.
Χωρίς Docker για το backend, χωρίς PostgreSQL (SQLite), χωρίς Redis.
Μοιράζεται το Qdrant και MongoDB του server.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    domain: str = "myloop.top"
    backend_secret_key: str = "change-me"
    encryption_key: str = ""

    # MongoDB — shared with existing server (no auth on localhost)
    mongo_admin_user: str = ""
    mongo_admin_pass: str = ""
    mongo_host: str = "127.0.0.1"

    # PostgreSQL — replaced with SQLite for zero-dependency deployment
    database_url: str = "sqlite+aiosqlite:///loopbridge.db"

    # Redis — not available, use in-memory
    redis_url: str = ""

    # GitHub OAuth
    github_client_id: str = ""
    github_client_secret: str = ""

    # DeepSeek
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_chat_model: str = "deepseek-v4-pro"

    # Qdrant — shared with existing server
    qdrant_url: str = "http://127.0.0.1:6333"

    # Email
    postmark_server_token: str = "skip"
    email_from: str = "noreply@myloop.top"

    # Docker / Nightscout
    docker_network: str = "bridge"
    nightscout_image: str = "nightscout/cgm-remote-monitor:15.0.3"
    nightsout_port_range_start: int = 9000

    model_config = {"env_file": "/var/www/loopbridge/.env", "env_file_encoding": "utf-8"}


settings = Settings()
