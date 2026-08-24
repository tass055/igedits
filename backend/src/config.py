from dotenv import load_dotenv
import os

load_dotenv()

_config_override = None


class Config:
    def __init__(self):
        self.openai_api_key = self._get_optional_env("OPENAI_API_KEY")
        self.anthropic_api_key = self._get_optional_env("ANTHROPIC_API_KEY")
        self.google_api_key = self._get_optional_env("GOOGLE_API_KEY")
        self.groq_api_key = self._get_optional_env("GROQ_API_KEY")
        self.youtube_data_api_key = self._get_optional_env("YOUTUBE_DATA_API_KEY")
        self.ollama_base_url = self._get_optional_env("OLLAMA_BASE_URL")
        self.ollama_api_key = self._get_optional_env("OLLAMA_API_KEY")

        # Support both WHISPER_MODEL and WHISPER_MODEL_SIZE for compatibility
        self.whisper_model = os.getenv("WHISPER_MODEL") or os.getenv("WHISPER_MODEL_SIZE", "base")
        self.llm = self._get_optional_env("LLM") or self._infer_default_llm()
        self.assembly_ai_api_key = os.getenv("ASSEMBLY_AI_API_KEY")
        self.pexels_api_key = os.getenv("PEXELS_API_KEY")
        # B-roll overlays: fetch stock footage from Pexels and cut it over clips at
        # AI-identified moments. Active only when both this flag and a Pexels key
        # are set, and the task itself requested B-roll.
        self.enable_broll = self._get_bool_env("ENABLE_BROLL", True)
        self.apify_api_token = self._get_optional_env("APIFY_API_TOKEN")
        self.youtube_metadata_provider = self._normalize_youtube_metadata_provider(
            os.getenv("YOUTUBE_METADATA_PROVIDER", "yt_dlp")
        )
        self.apify_youtube_default_quality = self._normalize_apify_quality(
            os.getenv("APIFY_YOUTUBE_DEFAULT_QUALITY", "1080")
        )

        self.max_video_duration = int(os.getenv("MAX_VIDEO_DURATION", "5400"))
        self.output_dir = os.getenv("OUTPUT_DIR", "outputs")

        self.max_clips = int(os.getenv("MAX_CLIPS", "10"))
        self.clip_duration = int(os.getenv("CLIP_DURATION", "30"))  # seconds

        self.temp_dir = os.getenv("TEMP_DIR", "temp")
        self.uploads_dir = os.getenv(
            "UPLOADS_DIR", os.path.join(self.temp_dir, "uploads")
        )

        # Redis configuration
        self.redis_host = os.getenv("REDIS_HOST", "localhost")
        self.redis_port = int(os.getenv("REDIS_PORT", "6379"))
        self.redis_password = self._get_optional_env("REDIS_PASSWORD")

        # Fail-safe: queued tasks should not stay queued forever
        self.queued_task_timeout_seconds = int(
            os.getenv("QUEUED_TASK_TIMEOUT_SECONDS", "180")
        )
        self.processing_task_timeout_seconds = int(
            os.getenv("PROCESSING_TASK_TIMEOUT_SECONDS", "1800")
        )

        self.self_host = self._get_bool_env("SELF_HOST", True)
        self.monetization_enabled = not self.self_host
        self.backend_auth_secret = self._get_optional_env("BACKEND_AUTH_SECRET")
        self.auth_signature_ttl_seconds = int(
            os.getenv("AUTH_SIGNATURE_TTL_SECONDS", "300")
        )
        self.free_plan_task_limit = int(os.getenv("FREE_PLAN_TASK_LIMIT", "10"))
        self.pro_plan_task_limit = int(os.getenv("PRO_PLAN_TASK_LIMIT", "0"))
        self.cors_origins = self._get_csv_env(
            "CORS_ORIGINS",
            [
                "http://localhost:3000",
                "http://sp.localhost:3000",
            ],
        )
        self.resend_api_key = self._get_optional_env("RESEND_API_KEY")
        self.resend_from_email = os.getenv(
            "RESEND_FROM_EMAIL", "igedits <onboarding@resend.dev>"
        )
        self.app_base_url = (
            self._get_optional_env("NEXT_PUBLIC_APP_URL") or "http://localhost:3000"
        ).rstrip("/")
        self.discord_feedback_webhook_url = self._get_optional_env("DISCORD_FEEDBACK_WEBHOOK_URL")
        self.discord_sales_webhook_url = self._get_optional_env("DISCORD_SALES_WEBHOOK_URL")
        self.transcript_language = self._get_optional_env("TRANSCRIPT_LANGUAGE")
        self.romanize_transcript = self._get_bool_env("ROMANIZE_TRANSCRIPT", False)
        self.video_encoder = (os.getenv("VIDEO_ENCODER", "cpu") or "cpu").strip().lower()
        if self.video_encoder not in {"cpu", "nvenc"}:
            self.video_encoder = "cpu"
        self.public_base_url = (
            self._get_optional_env("PUBLIC_BASE_URL") or self.app_base_url
        ).rstrip("/")
        self.make_instagram_webhook_url = self._get_optional_env("MAKE_INSTAGRAM_WEBHOOK_URL")
        self.default_processing_mode = os.getenv("DEFAULT_PROCESSING_MODE", "fast")
        self.fast_mode_max_clips = int(os.getenv("FAST_MODE_MAX_CLIPS", "4"))
        self.fast_mode_transcript_model = os.getenv(
            "FAST_MODE_TRANSCRIPT_MODEL", "nano"
        )

        # Proactive LLM rate limiting (see rate_limit.py). Enforced across the
        # backend + worker processes via a Redis-backed token bucket so provider
        # per-minute / per-second thresholds are never exceeded. 0 disables a check.
        # Defaults sit under Groq's free-tier ceiling for llama-3.3-70b (~30 RPM,
        # ~12k TPM); override to match your account's actual limits.
        self.llm_max_rpm = int(os.getenv("LLM_MAX_RPM", "25"))
        self.llm_max_rps = int(os.getenv("LLM_MAX_RPS", "1"))
        self.llm_max_tpm = int(os.getenv("LLM_MAX_TPM", "10000"))

        # Transcript analysis is split into chunks so a single request never
        # exceeds the provider's per-request / TPM ceiling (Groq free tier rejects
        # requests over ~12k tokens). LLM_MAX_REQUEST_TOKENS is the target size of
        # one request; LLM_REQUEST_TOKEN_OVERHEAD reserves budget for the fixed
        # system prompt + output schema, leaving the remainder for transcript text.
        self.llm_max_request_tokens = int(os.getenv("LLM_MAX_REQUEST_TOKENS", "8000"))
        self.llm_request_token_overhead = int(
            os.getenv("LLM_REQUEST_TOKEN_OVERHEAD", "4000")
        )

    @staticmethod
    def _get_optional_env(name: str):
        value = os.getenv(name)
        if value is None:
            return None

        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _get_bool_env(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default

    @staticmethod
    def _get_csv_env(name: str, default: list[str]) -> list[str]:
        value = os.getenv(name)
        if not value:
            return default
        return [item.strip() for item in value.split(",") if item.strip()]

    @staticmethod
    def _normalize_apify_quality(value: str | None) -> str:
        normalized = (value or "").strip()
        if normalized in {"360", "480", "720", "1080"}:
            return normalized
        return "1080"

    @staticmethod
    def _normalize_youtube_metadata_provider(value: str | None) -> str:
        normalized = (value or "").strip().lower()
        if normalized == "youtube_data_api":
            return "youtube_data_api"
        return "yt_dlp"

    def resolve_youtube_data_api_key(self) -> str | None:
        return self.youtube_data_api_key or self.google_api_key

    def _infer_default_llm(self) -> str:
        """
        Infer a usable default model based on whichever API key is present.
        Falls back to Google for backward compatibility.
        """
        if self.groq_api_key:
            return "groq:llama-3.3-70b-versatile"
        if self.google_api_key:
            return "google-gla:gemini-3-flash-preview"
        if self.openai_api_key:
            return "openai:gpt-5.2"
        if self.anthropic_api_key:
            return "anthropic:claude-4-sonnet"
        return "google-gla:gemini-3-flash-preview"


def get_config() -> Config:
    override = _config_override
    if override is not None:
        return override
    return Config()


def set_config_override(config: Config | None) -> None:
    global _config_override
    _config_override = config
