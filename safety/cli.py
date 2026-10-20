"""Operator commands for the independent safety database."""
import click
from flask import current_app
from sqlalchemy.exc import IntegrityError

from .models import LoginSession, Member, ReadingSession, db, now


def _text(value, label, limit, required=True):
    if not isinstance(value, str) or (required and not value.strip()) or len(value.strip()) > limit:
        raise click.ClickException(f'{label}: adj meg legfeljebb {limit} karaktert.')
    value = value.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise click.ClickException(f'{label}: érvénytelen vezérlőkarakter.')
    return value


def register_cli(app):
    @app.cli.command('init-db')
    def init_db():
        """Create only this application's independent tables."""
        db.create_all()
        click.echo('A tűz- és munkavédelmi adatbázis táblái elkészültek.')

    @app.cli.command('add-member')
    @click.argument('subject')
    @click.option('--name', required=True)
    @click.option('--email', required=True)
    def add_member(subject, name, email):
        """Add a colleague by their stable shared-account subject."""
        subject = _text(subject, 'Fiókazonosító', 255)
        name = _text(name, 'Név', 120)
        email = _text(email, 'E-mail', 254)
        issuer = current_app.config['SAFETY_OIDC_ISSUER']
        if Member.query.filter_by(issuer=issuer, subject=subject).first():
            raise click.ClickException('Ez a fiók már szerepel a munkatársak között.')
        db.session.add(Member(issuer=issuer, subject=subject, name=name, email=email,
                              role='employee', active=True))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise click.ClickException('Ez a fiók már szerepel a munkatársak között.') from None
        click.echo('Munkatárs hozzáadva. A központi alkalmazás-hozzáférést külön kell engedélyezni.')

    @app.cli.command('set-admin')
    @click.argument('subject')
    @click.option('--name')
    @click.option('--email')
    @click.option('--remove', is_flag=True, help='Adminisztrátori jogosultság visszavonása.')
    def set_admin(subject, name, email, remove):
        """Grant/revoke local admin rights for an exact stable account identity."""
        subject = _text(subject, 'Fiókazonosító', 255)
        issuer = current_app.config['SAFETY_OIDC_ISSUER']
        member = Member.query.filter_by(issuer=issuer, subject=subject).first()
        if member is None:
            if remove:
                raise click.ClickException('A fiók nem szerepel a munkatársak között.')
            name = _text(name, 'Új fiók neve (--name)', 120)
            email = _text(email, 'Új fiók e-mail-címe (--email)', 254)
            member = Member(issuer=issuer, subject=subject, name=name, email=email,
                            role='admin', active=True)
            db.session.add(member)
        else:
            if not remove and not member.active:
                raise click.ClickException('Előbb aktiváld a munkatársat az adminfelületen.')
            if name is not None:
                member.name = _text(name, 'Név', 120)
            if email is not None:
                member.email = _text(email, 'E-mail', 254)
            if remove and member.role == 'admin' and member.active:
                others = Member.query.filter(Member.id != member.id, Member.role == 'admin', Member.active.is_(True)).count()
                if not others:
                    raise click.ClickException('Az utolsó aktív adminisztrátor jogosultsága nem vonható vissza.')
            member.role = 'employee' if remove else 'admin'
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise click.ClickException('A fiókot közben módosították. Ellenőrizd és próbáld újra.') from None
        click.echo('A helyi adminisztrátori jogosultság frissült. A központi hozzáférés változatlan.')

    @app.cli.command('cleanup')
    def cleanup():
        """Remove expired sessions, retaining every training/completion record."""
        db.session.execute(db.delete(LoginSession).where(LoginSession.expires_at <= now()))
        db.session.execute(db.delete(ReadingSession).where(ReadingSession.expires_at <= now()))
        db.session.commit()
        click.echo('A lejárt munkamenetek törölve; a tanulási adatok megmaradtak.')
