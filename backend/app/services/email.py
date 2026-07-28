"""Email sending (SMTP) with a console fallback.

- If SMTP isn't configured, emails are logged to the console instead of sent,
  so development needs zero setup and nothing silently fails.
- smtplib is blocking, so the actual send runs in a thread via asyncio.to_thread
  to keep the event loop free.
- Templates are plain functions returning (subject, html, text). Keep them
  simple and inline — no template engine dependency.

Provider-agnostic: works with Gmail, Zoho, Mailgun SMTP, etc. via the SMTP_*
settings.
"""
import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger("karibu.email")


def _send_sync(to: str, subject: str, html: str, text: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    timeout = settings.SMTP_TIMEOUT_SECONDS
    if settings.SMTP_USE_TLS:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout) as server:
            server.starttls()
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
    else:
        with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout) as server:
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)


def _is_permanent(exc: Exception) -> bool:
    """True for failures that retrying cannot fix (bad credentials, rejected
    sender/recipient). Retrying those just delays the caller for nothing."""
    return isinstance(
        exc,
        (
            smtplib.SMTPAuthenticationError,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPNotSupportedError,
        ),
    )


async def send_email(to: str, subject: str, html: str, text: str) -> bool:
    """Send an email. Returns True if it was sent, False if it was not.

    Never raises — email failures shouldn't break the calling request. Callers
    that depend on delivery (signup codes) must check the return value.

    Transient failures are retried EMAIL_SEND_RETRIES times with a short
    backoff; authentication and rejected-address errors are permanent and fail
    immediately.
    """
    if not settings.email_configured:
        # No SMTP: log the message instead so development needs zero setup.
        # This path is unreachable in production — assert_production_ready()
        # refuses to boot without SMTP_HOST — but if it is ever reached there,
        # report failure rather than claiming a delivery that never happened.
        if settings.ENV == "production":
            logger.error(
                "SMTP is not configured in production — refusing to pretend "
                "the email to %s was delivered (subject=%s)", to, subject,
            )
            return False
        logger.info("[email:console] To=%s | Subject=%s\n%s", to, subject, text)
        return True

    attempts = max(1, settings.EMAIL_SEND_RETRIES + 1)
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.to_thread(_send_sync, to, subject, html, text)
            logger.info("Email sent to %s (%s)", to, subject)
            return True
        except Exception as exc:
            permanent = _is_permanent(exc)
            last = attempt == attempts
            logger.warning(
                "Email send to %s failed (attempt %d/%d, %s): %s",
                to, attempt, attempts,
                "permanent" if permanent else "transient",
                exc,
            )
            if permanent or last:
                logger.error(
                    "Giving up sending email to %s (subject=%s)", to, subject,
                    exc_info=exc,
                )
                return False
            await asyncio.sleep(2 ** (attempt - 1))
    return False


# --- Shared HTML shell ------------------------------------------------------
def _wrap(title: str, body_html: str) -> str:
    return f"""\
<!doctype html><html><body style="margin:0;background:#FBFAF6;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:520px;margin:0 auto;padding:32px 24px;">
    <div style="font-size:22px;font-weight:bold;color:#005C39;margin-bottom:4px;">Karibu POS</div>
    <div style="height:3px;width:44px;background:#F97316;border-radius:2px;margin-bottom:24px;"></div>
    <h1 style="font-size:20px;color:#1a1a1a;margin:0 0 16px;">{title}</h1>
    {body_html}
    <div style="margin-top:32px;padding-top:16px;border-top:1px solid #E7E4DC;font-size:12px;color:#8a8a8a;">
      Karibu POS · Restaurant point of sale
    </div>
  </div>
</body></html>"""


def _button(url: str, label: str) -> str:
    return (
        f'<a href="{url}" style="display:inline-block;background:#005C39;color:#fff;'
        f'text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:bold;">'
        f"{label}</a>"
    )


# --- Templates --------------------------------------------------------------
def confirm_email_code(full_name: str, code: str) -> tuple[str, str, str]:
    subject = "Your Karibu POS verification code"
    html = _wrap(
        "Confirm your email",
        f"""<p style="color:#3a3a3a;font-size:15px;line-height:1.6;">
        Hi {full_name}, welcome to Karibu POS! Enter this code in the app to
        confirm your email address and activate your account.</p>
        <p style="margin:24px 0;font-size:32px;font-weight:bold;letter-spacing:8px;
        background:#F4F2EC;padding:16px 20px;border-radius:8px;text-align:center;">
        {code}</p>
        <p style="color:#8a8a8a;font-size:13px;line-height:1.6;">
        This code expires in {settings.EMAIL_OTP_MINUTES} minutes. If you didn't
        create a Karibu POS account, you can ignore this email.</p>""",
    )
    text = (
        f"Hi {full_name}, welcome to Karibu POS!\n\n"
        f"Your verification code is: {code}\n\n"
        f"Enter it in the app to activate your account. This code expires in "
        f"{settings.EMAIL_OTP_MINUTES} minutes. If you didn't sign up, ignore "
        f"this email."
    )
    return subject, html, text


def payment_failed(full_name: str, retry_when: str, pay_url: str) -> tuple[str, str, str]:
    subject = "Payment failed — action needed · Karibu POS"
    html = _wrap(
        "We couldn't process your payment",
        f"""<p style="color:#3a3a3a;font-size:15px;line-height:1.6;">
        Hi {full_name}, your Karibu POS subscription payment didn't go through.
        We'll automatically try again {retry_when}, but you can pay now to avoid
        any interruption.</p>
        <p style="margin:24px 0;">{_button(pay_url, "Pay now")}</p>
        <p style="color:#8a8a8a;font-size:13px;line-height:1.6;">
        If your subscription lapses, your team keeps access to billing so you can
        always restore service.</p>""",
    )
    text = (
        f"Hi {full_name}, your Karibu POS subscription payment failed.\n\n"
        f"We'll retry automatically {retry_when}. To avoid interruption, pay now:\n"
        f"{pay_url}\n"
    )
    return subject, html, text


def subscription_suspended(full_name: str, pay_url: str) -> tuple[str, str, str]:
    subject = "Your subscription is suspended · Karibu POS"
    html = _wrap(
        "Subscription suspended",
        f"""<p style="color:#3a3a3a;font-size:15px;line-height:1.6;">
        Hi {full_name}, after several failed payment attempts your Karibu POS
        subscription has been suspended, so ordering is paused. Pay now to
        restore access immediately.</p>
        <p style="margin:24px 0;">{_button(pay_url, "Reactivate")}</p>""",
    )
    text = (
        f"Hi {full_name}, your Karibu POS subscription has been suspended after "
        f"failed payments. Reactivate:\n{pay_url}\n"
    )
    return subject, html, text


def staff_welcome(full_name: str, inviter: str, restaurant: str, email: str,
                  temp_password: str, login_url: str) -> tuple[str, str, str]:
    subject = f"You've been added to {restaurant} · Karibu POS"
    html = _wrap(
        f"Welcome to {restaurant}",
        f"""<p style="color:#3a3a3a;font-size:15px;line-height:1.6;">
        Hi {full_name}, {inviter} has added you to <b>{restaurant}</b> on Karibu
        POS.</p>
        <p style="color:#3a3a3a;font-size:15px;line-height:1.6;">
        Sign in with your email (<b>{email}</b>) and this temporary password:</p>
        <p style="font-size:18px;font-weight:bold;background:#F4F2EC;padding:12px 16px;
        border-radius:8px;letter-spacing:1px;">{temp_password}</p>
        <p style="margin:24px 0;">{_button(login_url, "Open Karibu POS")}</p>
        <p style="color:#8a8a8a;font-size:13px;line-height:1.6;">
        Please change your password after your first sign-in.</p>""",
    )
    text = (
        f"Hi {full_name}, {inviter} added you to {restaurant} on Karibu POS.\n\n"
        f"Email: {email}\nTemporary password: {temp_password}\n\n"
        f"Sign in: {login_url}\nPlease change your password after first sign-in."
    )
    return subject, html, text
