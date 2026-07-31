"""Email sending with a console fallback.

Two transports, selected by EMAIL_PROVIDER:

- "smtp" (default) — any SMTP host via the SMTP_* settings (Gmail, Zoho,
  Mailgun…). smtplib is blocking, so the send runs in a thread via
  asyncio.to_thread to keep the event loop free.
- "brevo" / "resend" / "sendgrid" — the provider's HTTPS API via httpx. Use
  these where outbound SMTP is firewalled: Render's free tier and most PaaS
  free tiers block ports 25/465/587, so smtplib fails with "[Errno 101]
  Network is unreachable" regardless of credentials. Port 443 is always open.

If neither is configured, emails are logged to the console instead of sent, so
development needs zero setup and nothing silently fails.

Templates are plain functions returning (subject, html, text). Keep them simple
and inline — no template engine dependency.
"""
import asyncio
import logging
import smtplib
import socket
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from app.core.config import settings

logger = logging.getLogger("karibu.email")


def _connect(host: str, port: int, timeout: int, source_address=None) -> socket.socket:
    """Open a TCP socket, trying IPv4 before IPv6 and logging every attempt.

    socket.create_connection tries the addresses getaddrinfo returns, but on
    failure re-raises only exceptions[0] — so when a host has both A and AAAA
    records you see one error and the others vanish. That matters here: many
    container platforms have no IPv6 route, which surfaces as ENETUNREACH
    ("Network is unreachable") from the IPv6 attempt and can mask what actually
    happened on IPv4. Ordering IPv4 first avoids that dead end entirely, and
    logging each address makes a real egress block distinguishable from a
    routing quirk.
    """
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    # IPv4 first; getaddrinfo's RFC 6724 ordering often puts IPv6 ahead.
    infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)

    errors: list[str] = []
    for family, socktype, proto, _canon, sa in infos:
        label = f"{'IPv4' if family == socket.AF_INET else 'IPv6'} {sa[0]}:{sa[1]}"
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            logger.debug("SMTP connected via %s", label)
            return sock
        except OSError as exc:
            if sock is not None:
                sock.close()
            errors.append(f"{label} -> {exc.__class__.__name__}: {exc}")
            logger.warning("SMTP connect failed: %s", errors[-1])

    raise OSError(
        f"could not reach {host}:{port} on any address. Tried: "
        + "; ".join(errors)
        + ". If every attempt says 'Network is unreachable' or times out, "
        "outbound SMTP is blocked by the host (common on PaaS free tiers) — "
        "no SMTP setting will fix it; send over an HTTPS API instead "
        "(EMAIL_PROVIDER=brevo/resend/sendgrid)."
    )


class _IPv4FirstSMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return _connect(host, port, timeout, self.source_address)


class _IPv4FirstSMTP_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        sock = _connect(host, port, timeout, self.source_address)
        # self._host stays the hostname, so cert validation still checks the
        # name rather than the resolved IP.
        return self.context.wrap_socket(sock, server_hostname=self._host)


def diagnose_smtp() -> None:
    """Log a verdict on whether this host can reach the configured SMTP server.

    Runs at boot when EMAIL_DIAGNOSE_ON_BOOT=true, so the answer shows up in
    the platform's log viewer — the only diagnostic channel available when
    there's no shell access (Render's shell is paid-only).
    """
    host, port = settings.SMTP_HOST, settings.SMTP_PORT
    if not host:
        logger.info("[smtp-diagnose] SMTP_HOST is empty — nothing to test")
        return
    logger.info("[smtp-diagnose] testing TCP reachability of %s:%s", host, port)
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError as exc:
        logger.error("[smtp-diagnose] DNS FAILED for %s: %s", host, exc)
        return
    logger.info(
        "[smtp-diagnose] resolves to: %s",
        ", ".join(
            f"{'IPv4' if f == socket.AF_INET else 'IPv6'} {sa[0]}"
            for f, _, _, _, sa in infos
        ),
    )
    try:
        sock = _connect(host, port, min(settings.SMTP_TIMEOUT_SECONDS, 10))
        sock.close()
        logger.info(
            "[smtp-diagnose] SUCCESS — outbound SMTP to %s:%s works from this "
            "host. If sends still fail it's credentials, not the network.",
            host, port,
        )
    except OSError as exc:
        logger.error(
            "[smtp-diagnose] BLOCKED — cannot open a TCP connection to %s:%s. "
            "%s", host, port, exc,
        )


def _send_sync(to: str, subject: str, html: str, text: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    timeout = settings.SMTP_TIMEOUT_SECONDS
    if settings.SMTP_USE_TLS:
        with _IPv4FirstSMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout) as server:
            server.starttls()
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
    else:
        with _IPv4FirstSMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout) as server:
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)


# --- HTTPS API transports ---------------------------------------------------
# Used where outbound SMTP is firewalled (Render's free tier and most PaaS free
# tiers block 25/465/587). Port 443 is always open, so these work when smtplib
# cannot connect at all. Each returns (url, headers, json_body) for httpx.
def _brevo_request(to: str, subject: str, html: str, text: str) -> tuple:
    return (
        "https://api.brevo.com/v3/smtp/email",
        {"api-key": settings.EMAIL_API_KEY, "accept": "application/json"},
        {
            "sender": {
                "name": settings.email_from_name,
                "email": settings.email_from_address,
            },
            "to": [{"email": to}],
            "subject": subject,
            "htmlContent": html,
            "textContent": text,
        },
    )


def _resend_request(to: str, subject: str, html: str, text: str) -> tuple:
    return (
        "https://api.resend.com/emails",
        {"Authorization": f"Bearer {settings.EMAIL_API_KEY}"},
        {
            "from": settings.EMAIL_FROM,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        },
    )


def _sendgrid_request(to: str, subject: str, html: str, text: str) -> tuple:
    return (
        "https://api.sendgrid.com/v3/mail/send",
        {"Authorization": f"Bearer {settings.EMAIL_API_KEY}"},
        {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {
                "email": settings.email_from_address,
                "name": settings.email_from_name,
            },
            "subject": subject,
            "content": [
                {"type": "text/plain", "value": text},
                {"type": "text/html", "value": html},
            ],
        },
    )


_API_BUILDERS = {
    "brevo": _brevo_request,
    "resend": _resend_request,
    "sendgrid": _sendgrid_request,
}


class PermanentEmailError(Exception):
    """A provider rejection that retrying cannot fix (bad key, bad sender)."""


async def _send_via_api(to: str, subject: str, html: str, text: str) -> None:
    provider = settings.EMAIL_PROVIDER.strip().lower()
    url, headers, payload = _API_BUILDERS[provider](to, subject, html, text)

    async with httpx.AsyncClient(timeout=settings.SMTP_TIMEOUT_SECONDS) as client:
        response = await client.post(url, headers=headers, json=payload)

    if response.status_code < 300:
        return
    detail = response.text[:400]
    # 4xx (other than rate limiting) means the request itself is wrong — a bad
    # API key, or a sender address the account hasn't verified. Retrying an
    # identical request just delays the caller for nothing.
    if 400 <= response.status_code < 500 and response.status_code != 429:
        raise PermanentEmailError(
            f"{provider} rejected the send: HTTP {response.status_code} {detail}"
        )
    raise RuntimeError(f"{provider} send failed: HTTP {response.status_code} {detail}")


def _is_permanent(exc: Exception) -> bool:
    """True for failures that retrying cannot fix (bad credentials, rejected
    sender/recipient). Retrying those just delays the caller for nothing."""
    return isinstance(
        exc,
        (
            PermanentEmailError,
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
        # No transport: log the message instead so development needs zero
        # setup. This path is unreachable in production — assert_production_
        # ready() refuses to boot without one — but if it is ever reached
        # there, report failure rather than claiming a delivery that never
        # happened.
        if settings.ENV == "production":
            logger.error(
                "No email transport configured in production — refusing to "
                "pretend the email to %s was delivered (subject=%s)", to, subject,
            )
            return False
        logger.info("[email:console] To=%s | Subject=%s\n%s", to, subject, text)
        return True

    via_api = settings.uses_email_api
    attempts = max(1, settings.EMAIL_SEND_RETRIES + 1)
    for attempt in range(1, attempts + 1):
        try:
            if via_api:
                await _send_via_api(to, subject, html, text)
            else:
                await asyncio.to_thread(_send_sync, to, subject, html, text)
            logger.info(
                "Email sent to %s (%s) via %s",
                to, subject, settings.EMAIL_PROVIDER if via_api else "smtp",
            )
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
def confirm_email_link(full_name: str, url: str) -> tuple[str, str, str]:
    """The signup confirmation email.

    A single obvious action. The raw URL is repeated underneath because some
    mail clients strip or rewrite buttons, and a user who can't click still
    needs a way in.
    """
    subject = "Confirm your email · Karibu POS"
    html = _wrap(
        "Confirm your email",
        f"""<p style="color:#3a3a3a;font-size:15px;line-height:1.6;">
        Hi {full_name}, welcome to Karibu POS! Click the button below to
        confirm your email address and activate your account.</p>
        <p style="margin:28px 0;">{_button(url, "Confirm my email")}</p>
        <p style="color:#8a8a8a;font-size:13px;line-height:1.6;">
        Or paste this into your browser:<br>
        <span style="word-break:break-all;color:#005C39;">{url}</span></p>
        <p style="color:#8a8a8a;font-size:13px;line-height:1.6;">
        This link expires in 24 hours. If you didn't create a Karibu POS
        account, you can ignore this email.</p>""",
    )
    text = (
        f"Hi {full_name}, welcome to Karibu POS!\n\n"
        f"Confirm your email address to activate your account:\n{url}\n\n"
        f"This link expires in 24 hours. If you didn't sign up, ignore this email."
    )
    return subject, html, text


def password_reset_link(full_name: str, url: str) -> tuple[str, str, str]:
    """The password reset email.

    Says plainly that ignoring it leaves the password unchanged. Someone who
    did not request this needs to know that doing nothing is safe — otherwise
    the email itself reads as the attack, and a worried owner starts clicking
    to "cancel" it.
    """
    subject = "Reset your password · Karibu POS"
    html = _wrap(
        "Reset your password",
        f"""<p style="color:#3a3a3a;font-size:15px;line-height:1.6;">
        Hi {full_name}, we received a request to reset your Karibu POS
        password. Click the button below to choose a new one.</p>
        <p style="margin:28px 0;">{_button(url, "Choose a new password")}</p>
        <p style="color:#8a8a8a;font-size:13px;line-height:1.6;">
        Or paste this into your browser:<br>
        <span style="word-break:break-all;color:#005C39;">{url}</span></p>
        <p style="color:#8a8a8a;font-size:13px;line-height:1.6;">
        This link expires in 1 hour and can only be used once. If you didn't
        ask for this, you can safely ignore this email — your password will not
        change.</p>""",
    )
    text = (
        f"Hi {full_name},\n\n"
        f"We received a request to reset your Karibu POS password.\n"
        f"Choose a new one here:\n{url}\n\n"
        f"This link expires in 1 hour and can only be used once.\n"
        f"If you didn't ask for this, ignore this email — your password will "
        f"not change."
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
