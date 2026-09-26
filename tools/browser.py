import webbrowser
from urllib.parse import quote


def open_url(url: str) -> str:
    """
    Open a URL in the user's default web browser.
    """

    url = url.strip()

    if not url:
        return "You didn't give me a URL."

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        webbrowser.open(url)

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

        webbrowser.open(url)

        return f"Performed web search for: {query}"

    except Exception as e:
        return f"Failed to perform the web search: {e}"
