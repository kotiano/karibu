"""Send a test verification email to prove SMTP works in this environment.

Run it wherever the app runs — locally, in the container, or in a Render shell —
so it exercises the exact same settings, network path and firewall rules the
signup flow will use:

    python send_test_email.py you@example.com

Exits 0 on success, 1 on failure, so it can gate a deploy.
"""
import asyncio
import logging
import sys

from app.core.config import settings
from app.services import email as email_service

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    to = sys.argv[1]

    print(f"ENV            : {settings.ENV}")
    print(f"EMAIL_PROVIDER : {settings.EMAIL_PROVIDER}")
    if settings.uses_email_api:
        print(f"Transport      : HTTPS API (port 443 — works where SMTP is blocked)")
        print(f"EMAIL_API_KEY  : {'set' if settings.EMAIL_API_KEY else '(none)'}")
    else:
        print(f"Transport      : SMTP")
        print(f"SMTP_HOST      : {settings.SMTP_HOST or '(empty — console fallback)'}")
        print(f"SMTP_PORT      : {settings.SMTP_PORT}")
        print(f"SMTP_USE_TLS   : {settings.SMTP_USE_TLS} "
              f"({'STARTTLS' if settings.SMTP_USE_TLS else 'implicit SSL'})")
        print(f"SMTP_USER      : {settings.SMTP_USER or '(none)'}")
        print(f"SMTP_PASSWORD  : {'set' if settings.SMTP_PASSWORD else '(none)'}")
    print(f"EMAIL_FROM     : {settings.EMAIL_FROM}")
    print(f"Sending to     : {to}\n")

    if not settings.email_configured:
        print("No transport configured. The code below was only logged, not emailed.")

    # Reuse the real signup template so the test proves the whole path.
    subject, html, text = email_service.confirm_email_code("Test User", "123456")
    ok = await email_service.send_email(to, subject, html, text)

    if ok and settings.email_configured:
        print("\nSUCCESS — accepted by the provider. Check the inbox (and spam).")
        return 0
    if ok:
        print("\nConsole fallback only — configure a transport to send for real.")
        return 1

    if settings.uses_email_api:
        print(
            "\nFAILED — see the error above.\n"
            "  HTTP 401/403  : EMAIL_API_KEY is wrong or revoked\n"
            "  HTTP 400      : the sender address in EMAIL_FROM is not verified\n"
            "                  on the account — verify it in the provider's\n"
            "                  dashboard (Senders / Domains) and retry\n"
            "  network error : outbound HTTPS is blocked, which is unusual —\n"
            "                  check the host's egress rules"
        )
    else:
        print(
            "\nFAILED — see the error above.\n"
            "  Errno 101 / timeout : outbound SMTP is BLOCKED on this host.\n"
            "                  Render's free tier firewalls ports 25/465/587.\n"
            "                  No SMTP setting fixes this — switch to an HTTPS\n"
            "                  API: set EMAIL_PROVIDER=brevo and EMAIL_API_KEY.\n"
            "  535 auth error  : SMTP_PASSWORD must be a Gmail APP PASSWORD,\n"
            "                  not the account password. Turn on 2-Step\n"
            "                  Verification, then myaccount.google.com ->\n"
            "                  Security -> App passwords -> generate.\n"
            "  sender refused  : Gmail only sends as the authenticated mailbox\n"
            "                  — EMAIL_FROM must match SMTP_USER"
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
