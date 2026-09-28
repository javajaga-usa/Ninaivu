"""Ninaivu API endpoints.

Exports:
    - bp: Main media API blueprint
    - admin_only: Admin-only endpoints
    - User accounts management
    - Admin console API
    - Media browsing and search
    - Face detection and library management
    - Photo sharing and galleries
    - Photo editing (straighten, rotate, etc.)
    - Cloud backup and sync
    - Archive operations
"""

from .api import bp, admin_only

__all__ = [
    "bp",
    "admin_only",
]
