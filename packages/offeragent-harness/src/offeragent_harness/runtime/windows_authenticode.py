"""Canonical Authenticode verifier used by release and process trust chains."""

from .windows_process import WindowsAuthenticodeVerifier

__all__ = ["WindowsAuthenticodeVerifier"]
