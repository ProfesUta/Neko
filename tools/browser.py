import webbrowser
from urllib.parse import quote, urlsplit


def open_url(url: str) -> str:
    """
    Open a URL in the user's default web browser.
    """

    url = url.strip()

    if not url:
        return "You didn't give me a URL."

    if "://" not in url:
        url = "https://" + url

    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return "Failed to open the website: provide a valid HTTP or HTTPS URL."
        if not webbrowser.open(url):
            return "Failed to open the website: no browser accepted the request."

        return f"Opened website: {url}"

    except Exception as e:
        return f"Failed to open the website: {e}"


def search_web(query: str) -> str:
    """
    Search the web using Google.
    """

    query = query.strip()

    if not query:
        return "You didn't give me anything to search for."

    try:
        encoded_query = quote(query)

        url = (
            "https://www.google.com/search?q="
            + encoded_query
        )

        if not webbrowser.open(url):
            return "Failed to perform the web search: no browser accepted the request."

        return f"Performed web search for: {query}"

    except Exception as e:
        return f"Failed to perform the web search: {e}"
