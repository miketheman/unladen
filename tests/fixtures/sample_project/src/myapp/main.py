"""Main module that uses external dependencies."""

import requests
from click import echo as say

response = requests.get("https://example.com")
say(response.text)
