"""
email_classifier.py
===================
Thin wrapper — delegates to claude_parser.is_invoice_email.
Kept as a separate module so the classifier can be swapped out
independently of the parser.
"""

from parsing.claude_parser import is_invoice_email

__all__ = ["is_invoice_email"]
