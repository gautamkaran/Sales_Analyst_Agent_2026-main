import streamlit as st

from agent import run_agent
from calendar_service import (
    CalendarServiceError,
    create_calendar_event,
    get_default_review_datetime,
)
from config import get_connection_status, get_settings
from gmail_service import GmailServiceError, create_gmail_draft
from google_sheets import GoogleSheetsError, get_google_credentials
from sales_analysis import analyse_sales

st.set_page_config(
    page_title="Sales Operations AI Agent",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _format_number(value, decimals=2):
    if value is None:
        return "Unavailable"
    return ("{:,." + str(decimals) + "f}").format(value)


st.title("Sales Operations AI Agent")
st.caption("Analyze sales, prepare a Gmail draft, and schedule a performance review.")

with st.sidebar:
    st.header("Connections")
    status = get_connection_status()
    for service, ready in status.items():
        st.markdown("**{}** | {}".format(service, "Ready" if ready else "Not configured"))

    if st.button(
        "Connect/update Google access",
        disabled=status["Google Sheets"] and status["Gmail"] and status["Google Calendar"],
    ):
        with st.spinner("Opening Google authorization..."):
            try:
                get_google_credentials(interactive=True, force_reauthorize=True)
                st.success("Google permissions saved locally.")
                st.rerun()
            except GoogleSheetsError as exc:
                st.error(str(exc))

    st.caption("Sheets access is read-only. Gmail drafts are not sent. Calendar invitations are sent only when a meeting is explicitly created.")
    st.divider()
    st.subheader("Workflow")
    st.markdown("1. Analyze sales records\n2. Review findings and recommendations\n3. Create a Gmail draft\n4. Schedule a review meeting")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "sales_analysis" not in st.session_state:
    st.session_state.sales_analysis = None
if "gmail_draft_status" not in st.session_state:
    st.session_state.gmail_draft_status = None
if "calendar_event_status" not in st.session_state:
    st.session_state.calendar_event_status = None

analysis_col, action_col = st.columns([3, 2], gap="large")
with analysis_col:
    st.subheader("Sales analysis")
    if status["Google Sheets"]:
        if st.button("Analyze September 2026 sales", type="primary"):
            with st.spinner("Reading and analyzing sales records..."):
                try:
                    st.session_state.sales_analysis = analyse_sales()
                except GoogleSheetsError as exc:
                    st.error(str(exc))
                except Exception:
                    st.error("Sales analysis failed. Check the sheet configuration and application logs.")
    else:
        st.info("Configure the spreadsheet ID and tab name, then connect Google access in the sidebar.")

    analysis = st.session_state.sales_analysis
    if analysis:
        if analysis.get("period_limitation"):
            st.warning(analysis["period_limitation"])
        else:
            st.caption("Period: {}".format(analysis["period"]))
        metric_cols = st.columns(3)
        metric_cols[0].metric("Total revenue", _format_number(analysis.get("total_revenue")))
        metric_cols[1].metric("Units sold", _format_number(analysis.get("units_sold"), 0))
        metric_cols[2].metric("Rows analyzed", str(analysis.get("rows_in_scope", 0)))
        if analysis.get("revenue_column_or_formula"):
            st.caption("Revenue basis: {}".format(analysis["revenue_column_or_formula"]))

        product_col, region_col = st.columns(2)
        with product_col:
            st.markdown("**Revenue by product**")
            if analysis.get("revenue_by_product"):
                st.dataframe(analysis["revenue_by_product"], hide_index=True, use_container_width=True)
                top, low = analysis.get("top_products"), analysis.get("lowest_products")
                if top and low:
                    st.caption("Top: {} ({:,.2f}) | Lowest: {} ({:,.2f})".format(", ".join(top["names"]), top["revenue"], ", ".join(low["names"]), low["revenue"]))
            else:
                st.caption("No usable product revenue rows.")
        with region_col:
            st.markdown("**Revenue by region**")
            if analysis.get("revenue_by_region"):
                st.dataframe(analysis["revenue_by_region"], hide_index=True, use_container_width=True)
                top, low = analysis.get("top_regions"), analysis.get("lowest_regions")
                if top and low:
                    st.caption("Top: {} ({:,.2f}) | Lowest: {} ({:,.2f})".format(", ".join(top["names"]), top["revenue"], ", ".join(low["names"]), low["revenue"]))
            else:
                st.caption("No usable regional revenue rows.")

        if analysis.get("regional_target_comparisons"):
            st.markdown("**Regional target comparison**")
            st.dataframe(analysis["regional_target_comparisons"], hide_index=True, use_container_width=True)
        st.markdown("**Recommendations**")
        if analysis.get("recommendations"):
            for recommendation in analysis["recommendations"]:
                st.markdown("- " + recommendation)
        else:
            st.caption("The available fields do not support a data-grounded recommendation yet.")
        if analysis.get("data_quality_notes"):
            with st.expander("Data quality and calculation notes"):
                for note in analysis["data_quality_notes"]:
                    st.markdown("- " + note)
    else:
        st.info("Run the analysis after connecting your Google Sheet.")

with action_col:
    st.subheader("Business actions")
    draft_status = st.session_state.gmail_draft_status
    if status["Gmail"] and status["Google Sheets"]:
        if st.button("Create Gmail sales report draft"):
            with st.spinner("Preparing the report from the connected sheet and saving a Gmail draft..."):
                try:
                    st.session_state.gmail_draft_status = create_gmail_draft()
                    draft_status = st.session_state.gmail_draft_status
                except (GmailServiceError, GoogleSheetsError) as exc:
                    st.error(str(exc))
        if draft_status and draft_status.get("success"):
            st.success("Draft saved; it has not been sent.")
            st.caption("Gmail draft ID: {}".format(draft_status["draft_id"]))
        else:
            st.caption("No draft created yet.")
    else:
        st.caption("Connect Sheets and Gmail to enable report drafts.")

    event_status = st.session_state.calendar_event_status
    if event_status and event_status.get("success"):
        if event_status.get("status") == "already_exists":
            st.info("Matching review meeting already exists; Calendar verified it.")
        else:
            st.success("Review meeting created and verified.")
        st.caption("{} | {} | {}".format(
            event_status.get("start"), event_status.get("time_zone"),
            event_status.get("attendee_email"),
        ))
        if event_status.get("invitation_notifications_requested"):
            st.caption("Calendar invitation notifications were requested.")
        if event_status.get("html_link"):
            st.link_button("Open meeting in Google Calendar", event_status["html_link"])

    with st.expander("Schedule a sales performance review"):
        settings = get_settings()
        default_start = get_default_review_datetime()
        attendee = st.text_input(
            "Attendee email (required; no address is guessed)",
            value=settings.google_attendee_email,
            key="calendar_attendee_input",
        )
        review_date = st.date_input(
            "Meeting date (Asia/Kolkata)", value=default_start.date(), key="calendar_date_input"
        )
        review_time = st.time_input(
            "Meeting time (Asia/Kolkata)", value=default_start.time().replace(tzinfo=None),
            key="calendar_time_input",
        )
        if not status["Google Calendar"]:
            st.info("Connect/update Google access to grant Calendar event permission.")
        if not status["Google Sheets"]:
            st.info("The event description uses findings from your connected sales sheet.")
        if not attendee.strip():
            st.info("Enter an attendee email here or set GOOGLE_ATTENDEE_EMAIL in .env.")
        can_schedule = status["Google Calendar"] and status["Google Sheets"] and bool(attendee.strip())
        if st.button("Create and verify review meeting", disabled=not can_schedule):
            with st.spinner("Checking for duplicates and creating the Calendar meeting..."):
                try:
                    st.session_state.calendar_event_status = create_calendar_event(
                        event_date=review_date.isoformat(),
                        event_time=review_time.strftime("%H:%M"),
                        attendee_email=attendee,
                    )
                    st.rerun()
                except (CalendarServiceError, GoogleSheetsError) as exc:
                    st.error(str(exc))

    st.markdown("**Gmail**: drafts only; never sent automatically.")
    st.markdown("**Calendar**: creates a meeting only after you click the action or explicitly request it in chat.")

st.divider()
st.subheader("Chat")
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("Ask a question or request a sales operation..."):
    prior_history = list(st.session_state.messages)
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        settings = get_settings()
        if not settings.openai_api_key:
            error = "OpenAI is not configured. Add OPENAI_API_KEY to your local .env file, then restart the app."
            st.error(error)
            st.session_state.messages.append({"role": "assistant", "content": error})
        else:
            try:
                tool_events = []
                with st.spinner("Thinking..."):
                    answer, called_tools = run_agent(
                        prompt, prior_history, settings=settings,
                        tool_event_callback=tool_events.append,
                    )
                action_completed = False
                for event in tool_events:
                    if event["name"] == "create_gmail_draft" and event["result"].get("success"):
                        st.session_state.gmail_draft_status = event["result"]
                        action_completed = True
                    if event["name"] == "create_calendar_event" and event["result"].get("success"):
                        st.session_state.calendar_event_status = event["result"]
                        action_completed = True
                st.markdown(answer)
                if called_tools:
                    st.caption("Application functions used: " + ", ".join(called_tools))
                st.session_state.messages.append({"role": "assistant", "content": answer})
                if action_completed:
                    st.rerun()
            except Exception as exc:
                error = str(exc) if isinstance(exc, RuntimeError) else "The agent request failed. Check configuration and application logs."
                st.error(error)
                st.session_state.messages.append({"role": "assistant", "content": error})
