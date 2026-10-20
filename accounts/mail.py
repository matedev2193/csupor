"""SMTP transport configured only by the account service operator."""

from email.message import EmailMessage
import smtplib
import ssl

from flask import current_app


def send_reset_email(email, token):
    config = current_app.config
    host = config.get("ACCOUNTS_SMTP_HOST")
    sender = config.get("ACCOUNTS_SMTP_FROM")
    if not host or not sender:
        raise RuntimeError("Account email transport is not configured.")
    link = config["ACCOUNTS_ISSUER"].rstrip("/") + "/reset-password/" + token
    message = EmailMessage()
    message["Subject"] = "Micimackó-fiók – jelszó visszaállítása"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        "Új jelszót kértél a Micimackó-fiókodhoz.\n\n"
        "Az alábbi hivatkozás 30 percig, egyszer használható:\n" + link + "\n\n"
        "Ha nem te kérted a módosítást, nincs teendőd. A jelszavad változatlan marad.\n"
    )
    with smtplib.SMTP(host, int(config.get("ACCOUNTS_SMTP_PORT") or 587), timeout=15) as smtp:
        smtp.ehlo()
        if config.get("ACCOUNTS_SMTP_STARTTLS", True):
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        username = config.get("ACCOUNTS_SMTP_USERNAME")
        if username:
            smtp.login(username, config.get("ACCOUNTS_SMTP_PASSWORD") or "")
        smtp.send_message(message)
