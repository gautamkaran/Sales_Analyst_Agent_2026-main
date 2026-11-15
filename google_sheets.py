"""Google OAuth and read-only Google Sheets access."""

import os
from pathlib import Path
from typing import Optional

from config import Settings, get_settings

SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events.owned"
SHEETS_SCOPES = [SHEETS_READONLY_SCOPE]
GMAIL_SCOPES = [GMAIL_COMPOSE_SCOPE]
CALENDAR_SCOPES = [CALENDAR_EVENTS_SCOPE]
SCOPES = SHEETS_SCOPES + GMAIL_SCOPES + CALENDAR_SCOPES


class GoogleSheetsError(RuntimeError):
    """Readable error for Google OAuth or Sheets API failures."""


def _has_required_scopes(credentials, required_scopes) -> bool:
    granted_scopes = getattr(credentials, "scopes", None)
    return bool(granted_scopes) and set(required_scopes).issubset(set(granted_scopes))


def has_saved_authorization(
    settings: Optional[Settings] = None,
    required_scopes=None,
) -> bool:
    """Check reusable local OAuth authorization without displaying its contents."""
    settings = settings or get_settings()
    required_scopes = required_scopes or SHEETS_SCOPES
    if not settings.google_token_file.is_file() or not settings.google_credentials_file.is_file():
        return False
    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(settings.google_token_file))
        return (
            _has_required_scopes(creds, required_scopes)
            and bool(creds.valid or (creds.expired and creds.refresh_token))
        )
    except Exception:
        return False


def _save_token(credentials, token_file: Path) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    try:
        os.chmod(str(token_file), 0o600)
    except OSError:
        # Windows may not support POSIX permission bits; the file remains ignored.
        pass


def get_google_credentials(
    interactive: bool = False,
    settings: Optional[Settings] = None,
    required_scopes=None,
    force_reauthorize: bool = False,
):
    """Load/refresh credentials or run local OAuth for the required scopes."""
    settings = settings or get_settings()
    required_scopes = required_scopes or SCOPES
    if not settings.google_credentials_file.is_file():
        raise GoogleSheetsError(
            "Google OAuth client file is missing. Put your downloaded Desktop app "
            "JSON at credentials/client_secret.json or set GOOGLE_CREDENTIALS_FILE."
        )

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        if not force_reauthorize and settings.google_token_file.is_file():
            try:
                # Do not pass requested scopes here: inspect the scopes actually
                # granted and persisted by the prior consent flow.
                creds = Credentials.from_authorized_user_file(str(settings.google_token_file))
            except (ValueError, OSError):
                creds = None

        has_scopes = bool(creds and _has_required_scopes(creds, required_scopes))
        if creds and creds.valid and has_scopes:
            return creds
        if creds and creds.expired and creds.refresh_token and has_scopes:
            creds.refresh(Request())
            _save_token(creds, settings.google_token_file)
            return creds
        if not interactive:
            raise GoogleSheetsError(
                "Google authorization is missing or needs additional permissions. "
                "Use Connect/update Google access in the sidebar to grant the required scopes."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(settings.google_credentials_file), required_scopes
        )
        creds = flow.run_local_server(
            port=0,
            open_browser=True,
            prompt="consent",
            access_type="offline",
        )
        if not _has_required_scopes(creds, required_scopes):
            raise GoogleSheetsError(
                "Google did not grant every requested permission. Reconnect and approve "
                "Sheets read access, Gmail draft access, and Calendar event access."
            )
        _save_token(creds, settings.google_token_file)
        return creds
    except GoogleSheetsError:
        raise
    except Exception as exc:
        raise GoogleSheetsError(
            "Google OAuth failed. Check the Desktop OAuth client, consent settings, "
            "enabled APIs, and try connecting again."
        ) from exc


def get_sheets_service(interactive: bool = False, settings: Optional[Settings] = None):
    """Build a Sheets v4 client with read-only Sheets authorization."""
    settings = settings or get_settings()
    credentials = get_google_credentials(
        interactive=interactive, settings=settings, required_scopes=SHEETS_SCOPES
    )
    try:
        from googleapiclient.discovery import build
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)
    except Exception as exc:
        raise GoogleSheetsError("Could not initialize the Google Sheets API client.") from exc


def _column_headers(raw_headers: list) -> list:
    """Keep sheet headers intact while making blank/duplicate keys unambiguous."""
    headers = []
    used = set()
    for index, raw in enumerate(raw_headers, start=1):
        base = str(raw).strip() or "Column_{}".format(index)
        name = base
        suffix = 2
        while name in used:
            name = "{}_{}".format(base, suffix)
            suffix += 1
        headers.append(name)
        used.add(name)
    return headers


def read_sales_rows(
    spreadsheet_id: Optional[str] = None,
    sheet_name: Optional[str] = None,
    service=None,
    interactive: bool = False,
    settings: Optional[Settings] = None,
) -> dict:
    """Read a worksheet with its first row as headers, preserving source values."""
    settings = settings or get_settings()
    spreadsheet_id = (spreadsheet_id or settings.google_spreadsheet_id).strip()
    sheet_name = (sheet_name or settings.google_sheet_name).strip()
    if not spreadsheet_id:
        raise GoogleSheetsError("Set GOOGLE_SPREADSHEET_ID in your local .env file.")
    if not sheet_name:
        raise GoogleSheetsError("Set GOOGLE_SHEET_NAME in your local .env file.")

    service = service or get_sheets_service(interactive=interactive, settings=settings)
    escaped_sheet_name = sheet_name.replace("'", "''")
    range_name = "'{}'!A:ZZ".format(escaped_sheet_name)
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise GoogleSheetsError(
            "Could not read the configured worksheet. Check the spreadsheet ID, "
            "tab name, and that the authorized Google account can access it. "
            "API error: {}".format(detail)
        ) from exc

    values = result.get("values", [])
    if not values:
        return {
            "spreadsheet_id": spreadsheet_id,
            "sheet_name": sheet_name,
            "headers": [],
            "rows": [],
            "row_count": 0,
        }

    headers = _column_headers(values[0])
    rows = []
    for row_number, raw_row in enumerate(values[1:], start=2):
        if not any(value not in (None, "") for value in raw_row):
            continue
        row = {header: raw_row[index] if index < len(raw_row) else ""
               for index, header in enumerate(headers)}
        row["_row_number"] = row_number
        rows.append(row)

    return {
        "spreadsheet_id": spreadsheet_id,
        "sheet_name": sheet_name,
        "headers": headers,
        "rows": rows,
        "row_count": len(rows),
    }
