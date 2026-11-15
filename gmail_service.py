"""Create an unsent Gmail draft containing the actual sales analysis."""

import base64
from email.message import EmailMessage
from typing import Optional

from config import Settings, get_settings
from google_sheets import GMAIL_SCOPES, get_google_credentials
from sales_analysis import analyse_sales


class GmailServiceError(RuntimeError):
    """Readable error for Gmail draft creation failures."""


def get_gmail_service(settings: Optional[Settings] = None):
    settings = settings or get_settings()
    credentials = get_google_credentials(
        settings=settings, required_scopes=GMAIL_SCOPES
    )
    try:
        from googleapiclient.discovery import build
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)
    except Exception as exc:
        raise GmailServiceError("Could not initialize the Gmail API client.") from exc


def _format_amount(value) -> str:
    return "Unavailable" if value is None else "{:,.2f}".format(value)


def _report_body(analysis: dict) -> str:
    period = analysis.get("period") or "Period unavailable"
    lines = [
        "Sales Performance Report",
        "",
        "Analysis period: {}".format(period),
        "Rows analyzed: {}".format(analysis.get("rows_in_scope", 0)),
        "Total revenue: {}".format(_format_amount(analysis.get("total_revenue"))),
        "Units sold: {}".format(_format_amount(analysis.get("units_sold"))),
        "Revenue calculation basis: {}".format(
            analysis.get("revenue_column_or_formula") or "Unavailable from the source columns"
        ),
        "",
        "Revenue by product:",
    ]
    products = analysis.get("revenue_by_product", [])
    lines.extend(
        "- {}: {} revenue; {} units".format(
            item["name"], _format_amount(item.get("revenue")),
            _format_amount(item.get("units_sold")),
        )
        for item in products
    )
    if not products:
        lines.append("- Unavailable from the available data")

    lines.extend(["", "Revenue by region:"])
    regions = analysis.get("revenue_by_region", [])
    lines.extend(
        "- {}: {} revenue; {} units".format(
            item["name"], _format_amount(item.get("revenue")),
            _format_amount(item.get("units_sold")),
        )
        for item in regions
    )
    if not regions:
        lines.append("- Unavailable from the available data")

    lines.extend(["", "Regional target comparisons:"])
    comparisons = analysis.get("regional_target_comparisons", [])
    lines.extend(
        "- {0}: actual {1}; target {2}; gap {3}; attainment {4}".format(
            item["region"], _format_amount(item.get("actual_revenue")),
            _format_amount(item.get("target")), _format_amount(item.get("gap")),
            ("{:.1f}%".format(item["attainment_percent"])
             if item.get("attainment_percent") is not None else "Unavailable"),
        )
        for item in comparisons
    )
    if not comparisons:
        lines.append("- No unambiguous regional target comparison was available")

    lines.extend(["", "Recommendations:"])
    recommendations = analysis.get("recommendations", [])
    lines.extend("{}. {}".format(index, item) for index, item in enumerate(recommendations, start=1))
    if not recommendations:
        lines.append("- The available data did not support recommendations")

    notes = analysis.get("data_quality_notes", [])
    if analysis.get("period_limitation"):
        notes = [analysis["period_limitation"]] + list(notes)
    lines.extend(["", "Data and spreadsheet notes:"])
    lines.extend("- {}".format(note) for note in notes)
    if not notes:
        lines.append("- No additional data limitations were reported")
    lines.extend([
        "- Spreadsheet updates: none performed; this integration uses read-only Sheets access.",
        "",
        "This message is saved as a Gmail draft and has not been sent.",
    ])
    return "\n".join(lines)


def create_gmail_draft(service=None, settings: Optional[Settings] = None) -> dict:
    """Analyze connected sheet data and create exactly one unsent report draft."""
    settings = settings or get_settings()
    try:
        analysis = analyse_sales()
    except Exception as exc:
        raise GmailServiceError(
            "The sales report could not be prepared from Google Sheets; no Gmail draft was created."
        ) from exc

    message = EmailMessage()
    message["Subject"] = "{} Sales Performance Report".format(analysis.get("period", "Sales"))
    message.set_content(_report_body(analysis))
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")

    service = service or get_gmail_service(settings=settings)
    try:
        response = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw}},
        ).execute()
    except Exception as exc:
        raise GmailServiceError(
            "Gmail did not confirm creation of the report draft. Check Gmail API access and authorization."
        ) from exc

    draft_id = response.get("id") if isinstance(response, dict) else getattr(response, "id", None)
    if not draft_id:
        raise GmailServiceError("Gmail returned no draft ID, so success could not be confirmed.")
    message_info = response.get("message", {}) if isinstance(response, dict) else {}
    message_id = message_info.get("id") if isinstance(message_info, dict) else getattr(message_info, "id", None)
    return {
        "success": True,
        "status": "draft_created",
        "draft_id": draft_id,
        "message_id": message_id,
        "subject": message["Subject"],
        "recipient": None,
        "sent": False,
        "period": analysis.get("period"),
        "rows_analyzed": analysis.get("rows_in_scope", 0),
        "total_revenue": analysis.get("total_revenue"),
        "units_sold": analysis.get("units_sold"),
    }
