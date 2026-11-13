"""
integrations/
=============
QuickBooks Online API integration package.

Public re-exports for convenient imports:
    from integrations import QBOAuth, QBOClient, QBOAuthError
"""
from .qbo_auth import QBOAuth, QBOAuthError
from .qbo_client import QBOClient

__all__ = ["QBOAuth", "QBOClient", "QBOAuthError"]
