import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "sk-placeholder")
    META_VERIFY_TOKEN: str = os.getenv("META_VERIFY_TOKEN", "my_secure_meta_verify_token_123")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./whatsapp_bot.db")
    META_GRAPH_API_VERSION: str = os.getenv("META_GRAPH_API_VERSION", "v20.0")
    # App secret from the Meta app dashboard. When set, every webhook call must carry a valid
    # X-Hub-Signature-256, so nobody but Meta can post fake customer messages.
    META_APP_SECRET: str = ""
    # WhatsApp access token and phone number ID for the tenant seeded on a brand-new database.
    # Later tenants get theirs from the admin dashboard.
    META_ACCESS_TOKEN: str = ""
    SEED_PHONE_NUMBER_ID: str = "1157440684128924"

    # Bootstrap credentials for the FIRST owner account, only used the very first time
    # the server starts with an empty admin_users table. Change the password afterward
    # from the admin dashboard's Staff Accounts panel — these env vars are not read again.
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "admin123")

    # Public base URL this server is reachable at (used to build invoice links and
    # Stripe success/cancel redirect URLs). Defaults to localhost for local dev.
    BASE_URL: str = os.getenv("BASE_URL", "http://localhost:8000")

    # Online payments (optional). If STRIPE_SECRET_KEY is left as a placeholder,
    # checkout falls back to the existing manual bank-transfer + receipt flow.
    # NOTE: Stripe does not settle in LKR — set PAYMENT_CURRENCY to a currency
    # your Stripe account supports (e.g. "usd").
    STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "sk_placeholder")
    STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    PAYMENT_CURRENCY: str = os.getenv("PAYMENT_CURRENCY", "usd")

    # Minutes of cart inactivity before a customer gets one abandoned-cart reminder.
    ABANDONED_CART_MINUTES: int = int(os.getenv("ABANDONED_CART_MINUTES", "30"))

    # AI providers. Any combination may be set; llm_router.py tries them in order and
    # falls back when one is rate-limited, overloaded or out of quota.
    GEMINI_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    MISTRAL_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_API_KEY_2: str = ""

    # Optional per-job model overrides, as "provider:model,provider:model".
    # Leave empty to use the defaults in llm_router.DEFAULT_ROLES.
    LLM_CHAT_MODELS: str = ""
    LLM_READER_MODELS: str = ""
    LLM_EXTRACT_MODELS: str = ""
    LLM_JUDGE_MODELS: str = ""
    LLM_FAST_MODELS: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
