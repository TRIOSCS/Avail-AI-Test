"""Shared APIRouter for the vendors package.

W4.8 split of the 1,475-line app/routers/htmx/vendors.py — pure structural move: URLs
and behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

from fastapi import APIRouter

router = APIRouter(tags=["htmx-views"])
