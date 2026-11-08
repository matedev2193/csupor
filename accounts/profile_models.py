"""Personal profile owned by the account service, separate from app permissions."""
from secrets import token_hex

from .models import db


class AccountProfile(db.Model):
    __tablename__ = 'account_profiles'
    subject = db.Column(db.String(36), db.ForeignKey('accounts.subject'), primary_key=True)
    # Missing keys have never been set. Explicit nulls are intentional clears and
    # must not be overwritten by a later, one-time legacy import.
    fields = db.Column(db.JSON, nullable=False, default=dict)
    revision = db.Column(db.String(32), nullable=False, default=lambda: token_hex(16))
    legacy_imported = db.Column(db.Boolean, nullable=False, default=False)
