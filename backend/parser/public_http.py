"""Public-site transport for collection initiated with a user-supplied URL."""

import ipaddress
import socket
from urllib.parse import urlsplit

import requests
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError
from urllib3.util.connection import create_connection


def website_key(url):
    parsed = urlsplit(url)
    return (parsed.hostname or "").lower().rstrip(".").removeprefix("www."), parsed.path.rstrip("/")


def public_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme not in {"http", "https"} or parsed.username is not None
            or parsed.port not in {None, 443 if parsed.scheme == "https" else 80}
            or "." not in host or host.lower().rstrip(".").endswith((".localhost", ".local", ".internal"))):
        raise ValueError("public_website_required")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global or address.is_multicast:
        raise ValueError("public_website_required")


class PublicConnection:
    def _new_conn(self):
        try:
            addresses = list(dict.fromkeys(item[4][0] for item in
                socket.getaddrinfo(self._dns_host, self.port, type=socket.SOCK_STREAM)))
            if not addresses or any(
                not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast
                for address in addresses
            ):
                raise OSError("Destination is outside the public network")
            last_error = None
            for address in addresses:
                try:
                    return create_connection((address, self.port), self.timeout,
                                             self.source_address, self.socket_options)
                except OSError as exc:
                    last_error = exc
            raise last_error
        except OSError as exc:
            raise NewConnectionError(self, f"Public site connection failed: {exc}") from exc


class PublicHTTPConnection(PublicConnection, HTTPConnection):
    pass


class PublicHTTPSConnection(PublicConnection, HTTPSConnection):
    pass


class PublicHTTPPool(HTTPConnectionPool):
    ConnectionCls = PublicHTTPConnection


class PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = PublicHTTPSConnection


class PublicSiteAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": PublicHTTPPool, "https": PublicHTTPSPool}

    def send(self, request, **kwargs):
        try:
            public_url(request.url)
        except ValueError:
            raise requests.exceptions.InvalidURL("Public website required", request=request) from None
        return super().send(request, **kwargs)


def public_site_session():
    session = requests.Session()
    session.trust_env = False
    adapter = PublicSiteAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
