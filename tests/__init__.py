"""Test package.

Marked as a package so `from .conftest import ...` works — shared fixtures are
imported explicitly rather than only injected, which keeps helper factories
readable at the call site.
"""
