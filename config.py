import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "sk-placeholder")
    META_VERIFY_TOKEN: str = os.getenv("META_VERIFY_TOKEN", "my_secure_meta_verify_token_123")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./whatsapp_bot.db")
    META_GRAPH_API_VERSION: str = os.getenv("META_GRAPH_API_VERSION", "v20.0")
    # Meta Developer Dashboard -> App settings -> Basic -> App secret. When set, every webhook
    # POST must carry Meta's X-Hub-Signature-256, so nobody who finds the URL can post fake
    # customer messages or orders. Set it on any server reachable from the internet.
    META_APP_SECRET: str = os.getenv("META_APP_SECRET", "")
    # WhatsApp access token and phone number ID for the tenant seeded on a brand-new database.
    # Later tenants get theirs from the admin dashboard.
    META_ACCESS_TOKEN: str = ""
    SEED_PHONE_NUMBER_ID: str = "1157440684128924"

    # Bootstrap credentials for the FIRST owner account, only used the very first time
    # the server starts with an empty admin_users table. Change the password afterward
    # from the admin dashboard's Staff Accounts panel — these env vars are not read again.
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "admin123")

    # Other websites allowed to call this server from a browser, comma separated, e.g. the
    # Business Sender site: "https://business-sender.vercel.app,http://localhost:3000".
    # Empty = only this server's own pages. Never "*": the API carries business data.
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "")

    # The platform's name, shown on the home page. Change it to your brand.
    PLATFORM_NAME: str = os.getenv("PLATFORM_NAME", "WhatsApp AI Bot")

    # Self-service sign-up: anyone can create an account and their own business bot from the
    # portal's login page. Set ALLOW_SIGNUP=false to close sign-ups (existing accounts still work).
    ALLOW_SIGNUP: bool = os.getenv("ALLOW_SIGNUP", "true").strip().lower() not in ("0", "false", "no")

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

    # Time zone the bot lives in: it is told today's date and the time of day from this, so it
    # can answer "is it open now?" and plan dates correctly.
    # Default timezone for the business. Set this per deployment to the tenant's local timezone.
    # Examples: Asia/Colombo, America/New_York, Europe/London, Asia/Dubai, Asia/Kuala_Lumpur
    BUSINESS_TIMEZONE: str = os.getenv("BUSINESS_TIMEZONE", "Asia/Colombo")

    # A customer who sends a voice note gets a voice note back (then the same reply in text).
    VOICE_REPLIES: bool = os.getenv("VOICE_REPLIES", "true").strip().lower() not in ("0", "false", "no")

    # WhatsApp voice calls (call_service.py): the AI answers calls and can call customers.
    # Calling needs a newer Graph API than messaging, so it has its own version.
    CALLS_ENABLED: bool = os.getenv("CALLS_ENABLED", "true").strip().lower() not in ("0", "false", "no")
    CALLS_GRAPH_API_VERSION: str = os.getenv("CALLS_GRAPH_API_VERSION", "v23.0")
    CALL_STUN_SERVER: str = os.getenv("CALL_STUN_SERVER", "stun:stun.l.google.com:19302")
    CALL_MAX_MINUTES: int = int(os.getenv("CALL_MAX_MINUTES", "15"))
    # Incoming calls ring the team's website first for this long; then the AI answers (0 = AI at once).
    CALL_TEAM_RING_SECONDS: int = int(os.getenv("CALL_TEAM_RING_SECONDS", "15"))
    # Keep a recording of each call (the greeting tells callers). false = no recordings.
    CALL_RECORDING: bool = os.getenv("CALL_RECORDING", "true").strip().lower() not in ("0", "false", "no")
    # After a call a person handled: transcribe it, keep it in the chat history, and suggest the
    # facts they gave as answers for the owner to approve (call_service.learn_from_call).
    CALL_LEARNING: bool = os.getenv("CALL_LEARNING", "true").strip().lower() not in ("0", "false", "no")

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
