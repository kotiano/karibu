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

    if settings.SMTP_USE_TLS:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as server:
            server.starttls()
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
    else:
        with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as server:
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)


async def send_email(to: str, subject: str, html: str, text: str) -> bool:
    """Send an email. Returns True if sent (or logged in dev), False on error.

    Never raises — email failures shouldn't break the calling request.
    """
    if not settings.email_configured:
        logger.info(
            "[email:console] To=%s | Subject=%s\n%s", to, subject, text
        )
        return True
    try:
        await asyncio.to_thread(_send_sync, to, subject, html, text)
        logger.info("Email sent to %s (%s)", to, subject)
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to)
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
def confirm_email(full_name: str, confirm_url: str) -> tuple[str, str, str]:
    subject = "Confirm your email · Karibu POS"
    html = _wrap(
        "Confirm your email",
        f"""<p style="color:#3a3a3a;font-size:15px;line-height:1.6;">
        Hi {full_name}, welcome to Karibu POS! Please confirm this email address
        to activate your account.</p>
        <p style="margin:24px 0;">{_button(confirm_url, "Confirm email")}</p>
        <p style="color:#8a8a8a;font-size:13px;line-height:1.6;">
        This link expires in {settings.EMAIL_TOKEN_HOURS} hours. If you didn't
        create a Karibu POS account, you can ignore this email.</p>""",
    )
    text = (
        f"Hi {full_name}, welcome to Karibu POS!\n\n"
        f"Confirm your email to activate your account:\n{confirm_url}\n\n"
        f"This link expires in {settings.EMAIL_TOKEN_HOURS} hours. "
        f"If you didn't sign up, ignore this email."
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
