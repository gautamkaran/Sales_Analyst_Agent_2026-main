"""Create and verify sales performance review events in Google Calendar."""

import base64
import hashlib
import re
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from config import Settings, get_settings
from google_sheets import CALENDAR_SCOPES, get_google_credentials
from sales_analysis import analyse_sales

TIME_ZONE = "Asia/Kolkata"
LOCAL_TZ = ZoneInfo(TIME_ZONE)
EVENT_SUMMARY = "Sales performance review"
EVENT_DURATION = timedelta(minutes=30)


class CalendarServiceError(RuntimeError):
    """Readable error for Calendar authorization, scheduling, or API failures."""


def get_calendar_service(settings: Optional[Settings] = None):
    settings = settings or get_settings()
    credentials = get_google_credentials(
        settings=settings, required_scopes=CALENDAR_SCOPES
    )
    try:
        from googleapiclient.discovery import build
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)
    except Exception as exc:
        raise CalendarServiceError("Could not initialize the Google Calendar API client.") from exc


def get_default_review_datetime(now: Optional[datetime] = None) -> datetime:
    """Return next Monday-Friday at 11:00 in Asia/Kolkata."""
    now = now.astimezone(LOCAL_TZ) if now else datetime.now(LOCAL_TZ)
    day = now.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, time(11, 0), tzinfo=LOCAL_TZ)


def _parse_start(event_date: Optional[str], event_time: Optional[str]) -> datetime:
    if event_date:
        try:
            day = datetime.strptime(event_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise CalendarServiceError("Meeting date must use YYYY-MM-DD format.") from exc
        if event_time:
            try:
                start_time = datetime.strptime(event_time, "%H:%M").time()
            except ValueError as exc:
                raise CalendarServiceError("Meeting time must use 24-hour HH:MM format.") from exc
        else:
            start_time = time(11, 0)
        start = datetime.combine(day, start_time, tzinfo=LOCAL_TZ)
    else:
        start = get_default_review_datetime()
        if event_time:
            try:
                start_time = datetime.strptime(event_time, "%H:%M").time()
            except ValueError as exc:
                raise CalendarServiceError("Meeting time must use 24-hour HH:MM format.") from exc
            start = datetime.combine(start.date(), start_time, tzinfo=LOCAL_TZ)
    if start <= datetime.now(LOCAL_TZ):
        raise CalendarServiceError("Meeting time must be in the future.")
    return start


def _validated_attendee(configured: str, provided: Optional[str]) -> str:
    value = (provided or configured or "").strip()
    if not value:
        raise CalendarServiceError(
            "No meeting attendee is configured. Set GOOGLE_ATTENDEE_EMAIL in .env "
            "or provide an attendee email explicitly. The address will not be guessed."
        )
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise CalendarServiceError("The attendee email address is not valid.")
    return value


def _report_description(analysis: dict) -> str:
    lines = [
        "Review the connected sales performance analysis.",
        "Analysis period: {}".format(analysis.get("period") or "Unavailable"),
        "Rows analyzed: {}".format(analysis.get("rows_in_scope", 0)),
        "Total revenue: {}".format(
            "Unavailable" if analysis.get("total_revenue") is None
            else "{:,.2f}".format(analysis["total_revenue"])
        ),
        "Units sold: {}".format(
            "Unavailable" if analysis.get("units_sold") is None
            else "{:,.2f}".format(analysis["units_sold"])
        ),
        "",
        "Revenue by product:",
    ]
    products = analysis.get("revenue_by_product", [])
    lines.extend(
        "- {0}: {1:,.2f} revenue".format(item["name"], item["revenue"])
        for item in products
    )
    if not products:
        lines.append("- Not available from the source data")
    lines.append("Revenue by region:")
    regions = analysis.get("revenue_by_region", [])
    lines.extend(
        "- {0}: {1:,.2f} revenue".format(item["name"], item["revenue"])
        for item in regions
    )
    if not regions:
        lines.append("- Not available from the source data")
    lines.append("Regional target comparisons:")
    comparisons = analysis.get("regional_target_comparisons", [])
    lines.extend(
        "- {0}: actual {1:,.2f}; target {2:,.2f}; gap {3:,.2f}".format(
            item["region"], item["actual_revenue"], item["target"], item["gap"]
        )
        for item in comparisons
    )
    if not comparisons:
        lines.append("- No unambiguous regional target comparison was available")
    lines.append("Recommendations:")
    recommendations = analysis.get("recommendations", [])
    lines.extend("{}. {}".format(index, item) for index, item in enumerate(recommendations, 1))
    if not recommendations:
        lines.append("- The available data did not support recommendations")
    notes = list(analysis.get("data_quality_notes", []))
    if analysis.get("period_limitation"):
        notes.insert(0, analysis["period_limitation"])
    lines.append("Data limitations:")
    lines.extend("- {}".format(note) for note in notes)
    if not notes:
        lines.append("- No additional limitations were reported")
    lines.append("Spreadsheet updates: none were performed; the current Sheets access is read-only.")
    return "\n".join(lines)


def _event_id(start: datetime) -> str:
    # Calendar event IDs must use lowercase base32hex characters (a-v and 0-9).
    digest = hashlib.sha256(start.isoformat().encode("utf-8")).digest()
    encoded = base64.b32hexencode(digest).decode("ascii").lower().rstrip("=")
    return "salesops" + encoded


def _api_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(LOCAL_TZ)


def _attendee_is_present(event: dict, email: str) -> bool:
    return any(
        str(attendee.get("email", "")).casefold() == email.casefold()
        for attendee in event.get("attendees", [])
    )


def _matches_schedule(event: dict, start: datetime) -> bool:
    if event.get("status") == "cancelled" or event.get("summary") != EVENT_SUMMARY:
        return False
    event_start = event.get("start", {}).get("dateTime")
    return bool(event_start and _api_datetime(event_start) == start)


def _find_existing_event(service, start: datetime, attendee: str) -> Optional[dict]:
    start_utc = (start - timedelta(seconds=1)).astimezone(ZoneInfo("UTC")).isoformat()
    end_utc = (start + EVENT_DURATION + timedelta(seconds=1)).astimezone(ZoneInfo("UTC")).isoformat()
    page_token = None
    while True:
        params = {
            "calendarId": "primary",
            "timeMin": start_utc,
            "timeMax": end_utc,
            "q": EVENT_SUMMARY,
            "singleEvents": True,
            "maxResults": 2500,
            "pageToken": page_token,
        }
        result = service.events().list(**{key: value for key, value in params.items() if value is not None}).execute()
        for event in result.get("items", []):
            if _matches_schedule(event, start):
                if not _attendee_is_present(event, attendee):
                    raise CalendarServiceError(
                        "A sales performance review already exists at that time but does not "
                        "include the requested attendee. No duplicate event was created."
                    )
                return event
        page_token = result.get("nextPageToken")
        if not page_token:
            return None


def _verify_event(service, event_id: str, start: datetime, attendee: str) -> dict:
    event = service.events().get(calendarId="primary", eventId=event_id).execute()
    if not _matches_schedule(event, start):
        raise CalendarServiceError("Google Calendar returned an event that did not match the requested meeting time.")
    event_end = event.get("end", {}).get("dateTime")
    expected_end = start + EVENT_DURATION
    if not event_end or _api_datetime(event_end) != expected_end:
        raise CalendarServiceError("Google Calendar did not confirm the 30-minute meeting duration.")
    if not _attendee_is_present(event, attendee):
        raise CalendarServiceError("Google Calendar did not confirm the requested attendee on the event.")
    return event


def _result(event: dict, attendee: str, status: str) -> dict:
    return {
        "success": True,
        "status": status,
        "event_id": event["id"],
        "summary": event.get("summary", EVENT_SUMMARY),
        "start": event.get("start", {}).get("dateTime"),
        "end": event.get("end", {}).get("dateTime"),
        "time_zone": TIME_ZONE,
        "attendee_email": attendee,
        "invitation_notifications_requested": status == "created_and_verified",
        "html_link": event.get("htmlLink"),
    }


def create_calendar_event(
    event_date: Optional[str] = None,
    event_time: Optional[str] = None,
    attendee_email: Optional[str] = None,
    service=None,
    settings: Optional[Settings] = None,
) -> dict:
    """Create one 30-minute, attendee-invited review and verify it via Calendar."""
    settings = settings or get_settings()
    attendee = _validated_attendee(settings.google_attendee_email, attendee_email)
    start = _parse_start(event_date, event_time)
    end = start + EVENT_DURATION

    try:
        analysis = analyse_sales()
    except Exception as exc:
        raise CalendarServiceError(
            "The sales findings could not be loaded; no Calendar event was created."
        ) from exc

    service = service or get_calendar_service(settings=settings)
    try:
        existing = _find_existing_event(service, start, attendee)
        if existing:
            # Confirm the existing event and attendee directly before reporting success.
            event = service.events().get(
                calendarId="primary", eventId=existing["id"]
            ).execute()
            if not _matches_schedule(event, start) or not _attendee_is_present(event, attendee):
                raise CalendarServiceError("Existing event verification failed.")
            return _result(event, attendee, "already_exists")

        event_id = _event_id(start)
        body = {
            "id": event_id,
            "summary": EVENT_SUMMARY,
            "description": _report_description(analysis),
            "start": {"dateTime": start.isoformat(), "timeZone": TIME_ZONE},
            "end": {"dateTime": end.isoformat(), "timeZone": TIME_ZONE},
            "attendees": [{"email": attendee}],
            "extendedProperties": {"private": {"salesOperationsReview": "true"}},
        }
        try:
            service.events().insert(
                calendarId="primary", body=body, sendUpdates="all"
            ).execute()
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) != 409:
                raise
            # A concurrent run may have inserted the deterministic event ID.
        verified = _verify_event(service, event_id, start, attendee)
        return _result(verified, attendee, "created_and_verified")
    except CalendarServiceError:
        raise
    except Exception as exc:
        raise CalendarServiceError(
            "Google Calendar did not confirm the review event. Check Calendar API access, "
            "permissions, and the target calendar."
        ) from exc
