from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    domain: str = "loopbridge.gr"
    backend_secret_key: str = "change-me-backend-secret-at-least-64-chars"
    encryption_key: str = ""  # Fernet key, generated once

    # MongoDB
    mongo_admin_user: str = "admin"
    mongo_admin_pass: str = ""
    mongo_host: str = "mongo"

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://loopbridge:change_me_pg@postgres:5432/loopbridge"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # GitHub OAuth
    github_client_id: str = ""
    github_client_secret: str = ""

    # Email
    postmark_server_token: str = ""
    email_from: str = "noreply@loopbridge.gr"

    # Docker
    docker_network: str = "traefik-public"
    nightscout_image: str = "nightscout/cgm-remote-monitor:15.0.3"

    # Qdrant / RAG
    qdrant_url: str = "http://qdrant:6333"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_chat_model: str = "deepseek-v4-pro"
    embedding_service: str = "local"  # "local" ή "deepseek" ή "openai"

    model_config = {"env_file": [".env", "/var/www/loopbridge/.env"], "env_file_encoding": "utf-8"}

settings = Settings()
