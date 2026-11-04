from types import SimpleNamespace

from tools import applications, browser


def test_browser_failure_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(browser.webbrowser, "open", lambda url: False)
    assert browser.open_url("example.com").startswith("Failed")
    assert browser.search_web("a query").startswith("Failed")


def test_invalid_url_never_reaches_browser(monkeypatch):
    calls = []
    monkeypatch.setattr(browser.webbrowser, "open", lambda url: calls.append(url) or True)
    assert browser.open_url("file:///C:/test").startswith("Failed")
    assert calls == []
    assert browser.open_url("example.com").startswith("Opened")
    assert calls == ["https://example.com"]


def test_edge_close_uses_executable_name_and_timeout(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(applications.subprocess, "run", run)
    assert applications.close_application("edge") == "Closed application: edge"
    assert calls[0][0] == ["taskkill", "/IM", "msedge.exe", "/T"]
    assert calls[0][1]["timeout"] == 5


def test_calculator_close_does_not_invent_success(monkeypatch):
    monkeypatch.setattr(applications.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    assert not applications.close_application("calculator").startswith("Closed")
