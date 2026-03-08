"""Authentication handlers."""


class HTTPBasicAuth:
    """HTTP Basic Authentication."""

    def __init__(self, username, password):
        self.username = username
        self.password = password

    def __call__(self, request):
        request.headers["Authorization"] = "Basic ..."
        return request


class HTTPDigestAuth:
    """HTTP Digest Authentication."""

    def __init__(self, username, password):
        self.username = username
        self.password = password

    def __call__(self, request):
        request.headers["Authorization"] = "Digest ..."
        return request
