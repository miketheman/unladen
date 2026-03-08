# ruff: noqa
# Fake requests package for testing
__version__ = "2.31.0"

from requests.api import get, post, put, delete, head, options, patch


def session():
    """Create a new session."""
    return Session()


class Session:
    """A requests session."""

    def __init__(self):
        self.headers = {}

    def get(self, url, **kwargs):
        return _send("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return _send("POST", url, **kwargs)


def _send(method, url, **kwargs):
    """Internal send method."""
    return {"method": method, "url": url}
