"""Gmail send integration for outbound emails (e.g. meeting-prep briefs)."""
from __future__ import annotations

import base64
import logging
from email.mime.text import MIMEText
from typing import Any

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.constants import Provider
from app.core.config import settings

logger = logging.getLogger(__name__)


def send_email(
    google_account: Any,
    subject: str,
    html_body: str,
) -> bool | str:
    """Send an HTML email via the Gmail API on behalf of *google_account*.

    Parameters
    ----------
    google_account:
        Object with ``.refresh_token`` and ``.email`` attributes.
    subject:
        Email subject line.
    html_body:
        HTML string used as the email body.

    Returns
    -------
    ``True`` on success, ``False`` on transient / unknown failure,
    or the string ``"auth_error"`` when the refresh token is invalid
    or permissions are insufficient.
    """
    try:
        credentials = Credentials(
            token=None,
            refresh_token=google_account.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
        )
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    except RefreshError:
        logger.warning(
            "Gmail send auth error: token refresh failed",
            extra={"provider": Provider.GMAIL, "email": google_account.email},
        )
        return "auth_error"

    try:
        message = MIMEText(html_body, "html")
        message["to"] = google_account.email
        message["from"] = google_account.email
        message["subject"] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body = {"raw": raw}

        service.users().messages().send(userId="me", body=body).execute()

        logger.info(
            "Gmail send succeeded",
            extra={"provider": Provider.GMAIL, "email": google_account.email, "subject": subject},
        )
        return True

    except HttpError as exc:
        status = exc.resp.status if exc.resp else None
        if status in (401, 403):
            logger.warning(
                "Gmail send auth error: HTTP %s",
                status,
                extra={"provider": Provider.GMAIL, "email": google_account.email},
            )
            return "auth_error"
        logger.exception(
            "Gmail send failed with HTTP error",
            extra={"provider": Provider.GMAIL, "email": google_account.email, "status": status},
        )
        return False

    except Exception:
        logger.exception(
            "Gmail send failed",
            extra={"provider": Provider.GMAIL, "email": google_account.email},
        )
        return False
