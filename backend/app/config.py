from dataclasses import dataclass
import os


def _csv_values(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, default).split(",") if value.strip())


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str | None
    database_name: str
    gridfs_bucket: str
    public_api_base_url: str | None
    n8n_webhook_url: str | None
    n8n_webhook_token: str | None
    n8n_callback_secret: str | None
    asset_signing_secret: str | None
    cors_origins: tuple[str, ...]
    landing_page_allowed_hosts: tuple[str, ...]
    max_image_bytes: int
    max_total_upload_bytes: int

    @classmethod
    def from_env(cls) -> "Settings":
        public_api_base_url = os.getenv("PUBLIC_API_BASE_URL", "").strip().rstrip("/")
        return cls(
            mongodb_uri=os.getenv("MONGODB_URI") or None,
            database_name=os.getenv("MONGODB_DATABASE", "launchforge").strip(),
            gridfs_bucket=os.getenv("GRIDFS_BUCKET", "launch_assets").strip(),
            public_api_base_url=public_api_base_url or None,
            n8n_webhook_url=os.getenv("N8N_WEBHOOK_URL") or None,
            n8n_webhook_token=os.getenv("N8N_WEBHOOK_TOKEN") or None,
            n8n_callback_secret=os.getenv("N8N_CALLBACK_SECRET") or None,
            asset_signing_secret=os.getenv("ASSET_SIGNING_SECRET") or None,
            cors_origins=_csv_values(
                "CORS_ORIGINS",
                "http://localhost:3000,http://localhost:5173",
            ),
            landing_page_allowed_hosts=tuple(
                host.lower() for host in _csv_values("LANDING_PAGE_ALLOWED_HOSTS")
            ),
            max_image_bytes=int(os.getenv("MAX_IMAGE_BYTES", str(10 * 1024 * 1024))),
            max_total_upload_bytes=int(
                os.getenv("MAX_TOTAL_UPLOAD_BYTES", str(25 * 1024 * 1024))
            ),
        )
