# Sales Operations AI Agent

A Streamlit application for sales analysis and assisted business operations. It uses a configured Google Sheet as its source of truth. Gmail drafts are never sent automatically; Calendar events are created only after an explicit user action.

## Setup

- Use Python 3.10.7 or newer and install dependencies with `pip install -r requirements.txt`.
- Keep `.env`, OAuth client credentials, and tokens local; they are excluded from source control.
- Configure Google Cloud with Sheets, Gmail, and Calendar APIs enabled and a Desktop OAuth client saved as `credentials/client_secret.json`.
- Set `GOOGLE_SPREADSHEET_ID` and the exact `GOOGLE_SHEET_NAME` in `.env`.
- Set `GOOGLE_ATTENDEE_EMAIL` if you want the dashboard to prefill the review attendee. The address can instead be entered explicitly when scheduling; the app never guesses one.
- Set `OPENAI_API_KEY` to enable chat; dashboard operations call Python functions directly.
- Start with `streamlit run app.py`.

## Google Sheets and sales analysis

The application reads actual worksheet rows using the `spreadsheets.readonly` scope. The analysis filters September 2026 when possible, calculates revenue and units, ranks products and regions, compares unambiguous regional targets, and reports limitations when source fields are missing.

## Gmail drafts

Enable the Gmail API in Google Cloud, then click **Connect/update Google access** and approve Gmail compose access. The application creates an unsent sales report draft from the live sheet analysis. It does not expose an email-sending function.

## Google Calendar

1. In the same Google Cloud project, enable the **Google Calendar API**.
2. Add your Google account as an OAuth test user if the consent screen is in testing mode.
3. Run the Streamlit app and click **Connect/update Google access**. Reauthorize so the token includes the Calendar events scope along with your existing Sheets and Gmail scopes.
4. In **Schedule a sales performance review**, confirm or edit the attendee email, date, and time, then click **Create and verify review meeting**. The default is the next Monday-Friday weekday at 11:00 AM Asia/Kolkata for 30 minutes. It does not account for public holidays.

The event description is generated from actual sheet analysis. An attendee email must be configured in `.env` or entered by the user. The API requests invitation notifications for that attendee, checks for an existing matching meeting before insertion, uses a deterministic event ID for retry safety, then retrieves the event from Calendar to verify the result. A matching meeting is reported as already existing rather than duplicated.

The agent can schedule an event when explicitly asked, including a user-provided date, time, and attendee. It never invents attendee addresses. Calendar status appears as ready only after the Calendar OAuth scope is granted. Do not paste OAuth credentials or tokens into chat.
