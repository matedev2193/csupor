"""SMTP transport for central account recovery messages."""

from email.message import EmailMessage
import smtplib
import ssl

from flask import current_app


def send_reset_email(email, token):
    from .mail_settings import effective_settings
    config = current_app.config
    settings = effective_settings()
    host = settings['host']
    sender = settings['sender']
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
    transport = smtplib.SMTP_SSL if settings['security'] == 'ssl' else smtplib.SMTP
    arguments = {'timeout': 15}
    if settings['security'] == 'ssl':
        arguments['context'] = ssl.create_default_context()
    with transport(host, int(settings['port']), **arguments) as smtp:
        smtp.ehlo()
        if settings['security'] == 'starttls':
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        username = settings['username']
        if username:
            smtp.login(username, settings['password'])
        smtp.send_message(message)
