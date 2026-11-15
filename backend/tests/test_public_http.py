from types import SimpleNamespace

import pytest
import requests
from urllib3.exceptions import NewConnectionError

from parser import public_http


def test_public_transport_pins_checked_dns_addresses(monkeypatch):
    resolved = ["203.0.113.1", "127.0.0.1"]
    calls = []
    monkeypatch.setattr(public_http.socket, "getaddrinfo", lambda *a, **kw: [
        (None, None, None, None, (address, 443)) for address in resolved
    ])
    monkeypatch.setattr(public_http, "create_connection", lambda address, *a: calls.append(address))
    connection = public_http.PublicHTTPSConnection("university.example", port=443)
    with pytest.raises(NewConnectionError):
        connection._new_conn()
    assert calls == []
    resolved[:] = ["8.8.8.8"]
    connection._new_conn()
    assert calls == [("8.8.8.8", 443)]


def test_public_transport_rejects_private_redirect_before_connecting(monkeypatch):
    def first_response(self, request, **kwargs):
        assert request.url == "https://university.example/"
        response = requests.Response()
        response.status_code = 302
        response.headers["Location"] = "http://127.0.0.1/"
        response.url = request.url
        response.request = request
        response.raw = SimpleNamespace(close=lambda: None, release_conn=lambda: None)
        response._content = b""
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", first_response)
    with public_http.public_site_session() as session, pytest.raises(requests.exceptions.InvalidURL):
        session.get("https://university.example")
