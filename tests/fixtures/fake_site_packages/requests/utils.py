"""Internal utility functions — never directly imported by users."""


def default_headers():
    """Return default headers."""
    return {
        "User-Agent": "python-requests/2.31.0",
        "Accept-Encoding": "gzip, deflate",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }


def parse_url(url):
    """Parse a URL into components."""
    parts = url.split("://", 1)
    if len(parts) == 2:
        scheme, rest = parts
    else:
        scheme = "http"
        rest = parts[0]
    return {"scheme": scheme, "rest": rest}


def check_header_validity(header):
    """Validate a header tuple."""
    name, value = header
    if not name:
        raise ValueError("Invalid header name")
    return True
