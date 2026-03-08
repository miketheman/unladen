"""Public API functions."""


def get(url, **kwargs):
    """Send a GET request."""
    return request("GET", url, **kwargs)


def post(url, **kwargs):
    """Send a POST request."""
    return request("POST", url, **kwargs)


def put(url, **kwargs):
    """Send a PUT request."""
    return request("PUT", url, **kwargs)


def delete(url, **kwargs):
    """Send a DELETE request."""
    return request("DELETE", url, **kwargs)


def head(url, **kwargs):
    """Send a HEAD request."""
    return request("HEAD", url, **kwargs)


def options(url, **kwargs):
    """Send an OPTIONS request."""
    return request("OPTIONS", url, **kwargs)


def patch(url, **kwargs):
    """Send a PATCH request."""
    return request("PATCH", url, **kwargs)


def request(method, url, **kwargs):
    """Core request function."""
    return {"method": method, "url": url}
