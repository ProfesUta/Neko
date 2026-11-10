import logging

from main import configure_logging


def test_successful_http_polls_are_silent_but_warnings_and_app_messages_remain(caplog, monkeypatch):
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setattr("main.configure_vision_logging", lambda *args: None)
    configure_logging()
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info('HTTP Request: GET https://127.0.0.1:2999/liveclientdata/eventdata "HTTP/1.1 200 OK"')
        logging.getLogger("httpcore").info("Successful HTTP connection")
        logging.getLogger("httpx").warning("HTTP connection warning")
        logging.getLogger("capri").info("Screen analysis completed")
    assert "200 OK" not in caplog.text and "Successful HTTP connection" not in caplog.text
    assert "HTTP connection warning" in caplog.text and "Screen analysis completed" in caplog.text
