"""OpenAI Responses API agent and application-side function calling."""

import json
from typing import Callable, Dict, List, Optional, Tuple

from calendar_service import create_calendar_event
from config import Settings, get_connection_status, get_settings
from gmail_service import create_gmail_draft
from google_sheets import read_sales_rows
from sales_analysis import analyse_sales

INSTRUCTIONS = """You are a careful sales operations assistant. Use only information supplied
by the user or returned by application tools. Use analyse_sales for calculations
and treat its results and limitations as authoritative. Never invent sales data,
attendees, or completed actions. Do not modify spreadsheet data.
Only call create_gmail_draft or create_calendar_event when the user explicitly asks
for that action. Gmail drafts are never sent. Calendar events are created only
with a configured or user-provided attendee email; never guess an address. If the
user does not specify an event time, the calendar tool applies next weekday at
11:00 Asia/Kolkata for 30 minutes. If they provide a date/time, pass the date as
YYYY-MM-DD and time as 24-hour HH:MM in Asia/Kolkata. Calendar event descriptions
are built from actual sheet analysis. Report success only if the Calendar API
confirms the event or an exact duplicate already exists.
"""

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "name": "get_integration_status",
        "description": "Check which application integrations are configured and ready.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "read_sales_rows",
        "description": "Read all data rows from the configured Google Sheets sales worksheet. This is read-only.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "analyse_sales",
        "description": "Calculate September 2026 sales totals, rankings, target comparisons, data limitations, and recommendations from the connected sheet.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "create_gmail_draft",
        "description": "Create one unsent Gmail sales report draft from actual connected sheet analysis. This function never sends email.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "create_calendar_event",
        "description": "Create and verify one 30-minute sales performance review in the user's primary Google Calendar. Use null date/time for the default next weekday at 11:00 Asia/Kolkata. attendee_email may be null only when GOOGLE_ATTENDEE_EMAIL is configured; never guess an attendee.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_date": {"type": ["string", "null"], "description": "Explicit meeting date as YYYY-MM-DD, or null for the default next weekday."},
                "event_time": {"type": ["string", "null"], "description": "Explicit local meeting time as 24-hour HH:MM, or null for 11:00."},
                "attendee_email": {"type": ["string", "null"], "description": "Email the user explicitly provided, or null to use GOOGLE_ATTENDEE_EMAIL. Never invent."},
            },
            "required": ["event_date", "event_time", "attendee_email"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def get_integration_status() -> dict:
    return {name: ("configured" if ready else "not configured")
            for name, ready in get_connection_status().items()}


def _read_sales_rows_tool() -> dict:
    return read_sales_rows()


ToolHandler = Callable[..., object]
TOOL_HANDLERS: Dict[str, ToolHandler] = {
    "get_integration_status": get_integration_status,
    "read_sales_rows": _read_sales_rows_tool,
    "analyse_sales": analyse_sales,
    "create_gmail_draft": create_gmail_draft,
    "create_calendar_event": create_calendar_event,
}


def _dispatch_tool(name: str, arguments_json: str) -> str:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": "This operation is not implemented or available."})
    try:
        arguments = json.loads(arguments_json or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("Function arguments must be a JSON object.")
        result = handler(**arguments)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        return json.dumps({"error": "Tool execution failed.", "detail": str(exc)[:300]})


def _function_calls(response) -> list:
    return [item for item in response.output if getattr(item, "type", None) == "function_call"]


def run_agent(
    user_input: str,
    history: Optional[List[dict]] = None,
    settings: Optional[Settings] = None,
    client=None,
    max_tool_rounds: int = 8,
    tool_event_callback: Optional[Callable[[dict], None]] = None,
) -> Tuple[str, List[str]]:
    """Run one turn and execute requested local functions through Python."""
    settings = settings or get_settings()
    if client is None:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is missing. Add it to your local .env file.")
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key)

    input_items = list(history or [])
    input_items.append({"role": "user", "content": user_input})
    response = client.responses.create(
        model=settings.openai_model, instructions=INSTRUCTIONS,
        input=input_items, tools=TOOL_DEFINITIONS,
    )

    called_tools: List[str] = []
    draft_created_this_turn = False
    calendar_created_this_turn = False
    for _ in range(max_tool_rounds):
        calls = _function_calls(response)
        if not calls:
            answer = getattr(response, "output_text", "")
            return (answer or "The model returned no text. Please try again."), called_tools

        tool_outputs = []
        for call in calls:
            name = call.name
            called_tools.append(name)
            if name == "create_gmail_draft" and draft_created_this_turn:
                output = json.dumps({"error": "Only one Gmail draft may be created per request."})
            elif name == "create_calendar_event" and calendar_created_this_turn:
                output = json.dumps({"error": "Only one Calendar event may be created per request."})
            else:
                output = _dispatch_tool(name, call.arguments)
            try:
                result = json.loads(output)
            except (TypeError, ValueError):
                result = {"error": "Tool returned an invalid result."}
            if name == "create_gmail_draft" and result.get("success") is True:
                draft_created_this_turn = True
            if name == "create_calendar_event" and result.get("success") is True:
                calendar_created_this_turn = True
            if tool_event_callback:
                tool_event_callback({"name": name, "result": result})
            tool_outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
        response = client.responses.create(
            model=settings.openai_model, instructions=INSTRUCTIONS,
            previous_response_id=response.id, input=tool_outputs,
            tools=TOOL_DEFINITIONS,
        )

    raise RuntimeError("The agent reached its function-call limit before completing the reply.")
