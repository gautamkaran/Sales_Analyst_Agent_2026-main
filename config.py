"""Environment-backed application configuration."""

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")


def _project_path(value: str, default: str) -> Path:
    path = Path(value or default).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


@dataclass(frozen=True)
class Settings:
    """Application settings. Secret values are never rendered in the UI."""

    openai_api_key: str
    openai_model: str
    google_credentials_file: Path
    google_token_file: Path
    google_spreadsheet_id: str
    google_sheet_name: str
    google_attendee_email: str


def get_settings() -> Settings:
    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.5").strip() or "gpt-5.5",
        google_credentials_file=_project_path(
            os.getenv("GOOGLE_CREDENTIALS_FILE", ""), "credentials/client_secret.json"
        ),
        google_token_file=_project_path(
            os.getenv("GOOGLE_TOKEN_FILE", ""), "credentials/token.json"
        ),
        google_spreadsheet_id=os.getenv("GOOGLE_SPREADSHEET_ID", "").strip(),
        google_sheet_name=os.getenv("GOOGLE_SHEET_NAME", "").strip(),
        google_attendee_email=os.getenv("GOOGLE_ATTENDEE_EMAIL", "").strip(),
    )


def get_connection_status() -> dict:
    """Report each service's granted-scope readiness without exposing secrets."""
    settings = get_settings()
    sheets_ready = False
    gmail_ready = False
    calendar_ready = False
    try:
        from google_sheets import CALENDAR_SCOPES, GMAIL_SCOPES, SHEETS_SCOPES, has_saved_authorization
        sheets_ready = bool(
            settings.google_spreadsheet_id
            and settings.google_sheet_name
            and has_saved_authorization(settings=settings, required_scopes=SHEETS_SCOPES)
        )
        gmail_ready = has_saved_authorization(
            settings=settings, required_scopes=GMAIL_SCOPES
        )
        calendar_ready = has_saved_authorization(
            settings=settings, required_scopes=CALENDAR_SCOPES
        )
    except ImportError:
        pass
    return {
        "OpenAI": bool(settings.openai_api_key),
        "Google Sheets": sheets_ready,
        "Gmail": gmail_ready,
        "Google Calendar": calendar_ready,
    }
