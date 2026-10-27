"""Shared test setup."""

# Standard library imports
import os

# aeth_ext's import needs this; set before anything imports it.
os.environ.setdefault("ALERTS_EMAIL_PWD", "test")
