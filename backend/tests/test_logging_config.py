import io
import logging

import logging_config


def test_server_errors_are_logged_but_access_requests_are_suppressed(monkeypatch):
    root = logging.getLogger()
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_access = logging.getLogger("uvicorn.access")
    previous_root_handlers = root.handlers[:]
    previous_root_level = root.level
    previous_error_state = (uvicorn_error.disabled, uvicorn_error.level, uvicorn_error.propagate)
    previous_access_disabled = uvicorn_access.disabled
    output = io.StringIO()
    monkeypatch.setattr(logging_config.sys, "stdout", output)

    try:
        logging_config.configure_logging()

        assert not uvicorn_error.disabled
        assert uvicorn_error.propagate
        assert uvicorn_access.disabled

        uvicorn_error.error("Ошибка привязки порта")
        assert "ОШИБКА" in output.getvalue()
        assert "Ошибка привязки порта" in output.getvalue()
    finally:
        root.handlers[:] = previous_root_handlers
        root.setLevel(previous_root_level)
        uvicorn_error.disabled, uvicorn_error.level, uvicorn_error.propagate = previous_error_state
        uvicorn_access.disabled = previous_access_disabled
