import datetime
import logging
import os
import re
import socket
import warnings
from http.client import HTTPConnection as _HTTPConnection
from http.client import HTTPException as HTTPException  # noqa: F401
from socket import timeout as SocketTimeout
from typing import (
    TYPE_CHECKING,
    Callable,
    Mapping,
    NamedTuple,
    Optional,
    Tuple,
    Type,
    Union,
    cast,
)

if TYPE_CHECKING:
    from typing_extensions import Literal

    from .connectionpool import HTTPConnectionPool
    from .response import HTTPResponse
    from .util.ssl_ import _TYPE_PEER_CERT_RET_DICT
    from .util.ssltransport import SSLTransport
    from .util.retry import Retry

from ._collections import HTTPHeaderDict
from .util.response import assert_header_parsing
from .util.timeout import _DEFAULT_TIMEOUT, _TYPE_TIMEOUT, Timeout
from .util.util import to_str

# Needed to move this far below to avoid circular import issues
# from .response import HTTPResponse

try:  # Compiled with SSL?
    import ssl

    BaseSSLError = ssl.SSLError
except (ImportError, AttributeError):
    ssl = None  # type: ignore[assignment]

    class BaseSSLError(BaseException):  # type: ignore[no-redef]
        pass


from ._version import __version__
from .exceptions import (
    ConnectTimeoutError,
    HeaderParsingError,
    NameResolutionError,
    NewConnectionError,
    ProxyError,
    SystemTimeWarning,
)
from .util import SKIP_HEADER, SKIPPABLE_HEADERS, connection, ssl_
from .util.request import body_to_chunks
from .util.ssl_ import assert_fingerprint as _assert_fingerprint
from .util.ssl_ import (
    create_urllib3_context,
    is_ipaddress,
    resolve_cert_reqs,
    resolve_ssl_version,
    ssl_wrap_socket,
)
from .util.ssl_match_hostname import CertificateError, match_hostname
from .util.typing import _TYPE_BODY
from .util.url import Url

# Not a no-op, we're adding this to the namespace so it can be imported.
ConnectionError = ConnectionError
BrokenPipeError = BrokenPipeError


log = logging.getLogger(__name__)

port_by_scheme = {"http": 80, "https": 443}

# When it comes time to update this value as a part of regular maintenance
# (ie test_recent_date is failing) update it to ~6 months before the current date.
RECENT_DATE = datetime.date(2022, 1, 1)

_CONTAINS_CONTROL_CHAR_RE = re.compile(r"[^-!#$%&'*+.^_`|~0-9a-zA-Z]")


class ProxyConfig(NamedTuple):
    ssl_context: Optional["ssl.SSLContext"]
    use_forwarding_for_https: bool


class HTTPConnection(_HTTPConnection):
    """
    Based on :class:`http.client.HTTPConnection` but provides an extra constructor
    backwards-compatibility layer between older and newer Pythons.

    Additional keyword parameters are used to configure attributes of the connection.
    Accepted parameters include:

    - ``source_address``: Set the source address for the current connection.
    - ``socket_options``: Set specific options on the underlying socket. If not specified, then
      defaults are loaded from ``HTTPConnection.default_socket_options`` which includes disabling
      Nagle's algorithm (sets TCP_NODELAY to 1) unless the connection is behind a proxy.

      For example, if you wish to enable TCP Keep Alive in addition to the defaults,
      you might pass:

      .. code-block:: python

         HTTPConnection.default_socket_options + [
             (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
         ]

      Or you may want to disable the defaults by passing an empty list (e.g., ``[]``).
    """

    default_port: int = port_by_scheme["http"]

    #: Disable Nagle's algorithm by default.
    #: ``[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]``
    default_socket_options: connection._TYPE_SOCKET_OPTIONS = [
        (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    ]

    #: Whether this connection verifies the host's certificate.
    is_verified: bool = False

    #: Whether this proxy connection (if used) verifies the proxy host's
    #: certificate. If no HTTPS proxy is being used will be ``None``.
    proxy_is_verified: Optional[bool] = None

    blocksize: int
    source_address: Optional[Tuple[str, int]]
    socket_options: Optional[connection._TYPE_SOCKET_OPTIONS]
    _tunnel_host: Optional[str]
    _tunnel: Callable[["HTTPConnection"], None]
    _connecting_to_proxy: bool

    def __init__(
        self,
        host: str,
        port: Optional[int] = None,
        timeout: _TYPE_TIMEOUT = _DEFAULT_TIMEOUT,
        source_address: Optional[Tuple[str, int]] = None,
        blocksize: int = 8192,
        socket_options: Optional[
            connection._TYPE_SOCKET_OPTIONS
        ] = default_socket_options,
        proxy: Optional[Url] = None,
        proxy_config: Optional[ProxyConfig] = None,
    ) -> None:
        # Pre-set source_address.
        self.source_address = source_address

        self.socket_options = socket_options

        # Proxy options provided by the user.
        self.proxy = proxy
        self.proxy_config = proxy_config

        super().__init__(
            host=host,
            port=port,
            timeout=Timeout.resolve_default_timeout(timeout),
            source_address=source_address,
            blocksize=blocksize,
        )

        self._connecting_to_proxy = False
        self.ResponseClass_overide: Optional[Type["HTTPResponse"]] = None

    # https://github.com/python/mypy/issues/4125
    # Mypy treats this as LSP violation, which is considered a bug.
    # If `host` is made a property it violates LSP, because a writeable attribute is overridden with a read-only one.
    # However, there is also a `host` setter so LSP is not violated.
    # Potentially, a `@host.deleter` might be needed depending on how this issue will be fixed.
    @property  # type: ignore[override]
    def host(self) -> str:  # type: ignore[override]
        """
        Getter method to remove any trailing dots that indicate the hostname is an FQDN.

        In general, SSL certificates don't include the trailing dot indicating a
        fully-qualified domain name, and thus, they don't validate properly when
        checked against a domain name that includes the dot. In addition, some
        servers may not expect to receive the trailing dot when provided.

        However, the hostname with trailing dot is critical to DNS resolution; doing a
        lookup with the trailing dot will properly only resolve the appropriate FQDN,
        whereas a lookup without a trailing dot will search the system's search domain
        list. Thus, it's important to keep the original host around for use only in
        those cases where it's appropriate (i.e., when doing DNS lookup to establish the
        actual TCP connection across which we're going to send HTTP requests).
        """
        return self._dns_host.rstrip(".")

    @host.setter
    def host(self, value: str) -> None:
        """
        Setter for the `host` property.

        We assume that only urllib3 uses the _dns_host attribute; httplib itself
        only uses `host`, and it seems reasonable that other libraries follow suit.
        """
        self._dns_host = value

    def _new_conn(self) -> socket.socket:
        """Establish a socket connection and set nodelay settings on it.

        :return: New socket connection.
        """

        try:
            sock = connection.create_connection(
                (self._dns_host, self.port),
                self.timeout,
                source_address=self.source_address,
                socket_options=self.socket_options,
            )
        except socket.gaierror as e:
            raise NameResolutionError(self.host, self, e) from e
        except SocketTimeout as e:
            raise ConnectTimeoutError(
                self,
                f"Connection to {self.host} timed out. (connect timeout={self.timeout})",
            ) from e

        except OSError as e:
            raise NewConnectionError(
                self, f"Failed to establish a new connection: {e}"
            ) from e

        return sock

    def _is_using_tunnel(self) -> Optional[str]:
        return self._tunnel_host

    def _prepare_conn(self, conn: socket.socket) -> None:
        self.sock = conn
        if self._is_using_tunnel():
            # TODO: Fix tunnel so it doesn't depend on self.sock state.
            self._tunnel()
            self._connecting_to_proxy = False
            # Mark this connection as not reusable
            self.auto_open = 0

    def connect(self) -> None:
        self._connecting_to_proxy = bool(self.proxy)
        conn = self._new_conn()
        self._prepare_conn(conn)
        self._connecting_to_proxy = False

    def close(self) -> None:
        self._connecting_to_proxy = False
        super().close()

    def putrequest(
        self,
        method: str,
        url: str,
        skip_host: bool = False,
        skip_accept_encoding: bool = False,
    ) -> None:
        """"""
        # Empty docstring because the indentation of CPython's implementation
        # is broken but we don't want this method in our documentation.
        match = _CONTAINS_CONTROL_CHAR_RE.search(method)
        if match:
            raise ValueError(
                f"Method cannot contain non-token characters {method!r} (found at least {match.group()!r})"
            )

        return super().putrequest(
            method, url, skip_host=skip_host, skip_accept_encoding=skip_accept_encoding
        )

    def putheader(self, header: str, *values: str) -> None:
        """"""
        if not any(isinstance(v, str) and v == SKIP_HEADER for v in values):
            super().putheader(header, *values)
        elif to_str(header.lower()) not in SKIPPABLE_HEADERS:
            skippable_headers = "', '".join(
                [str.title(header) for header in sorted(SKIPPABLE_HEADERS)]
            )
            raise ValueError(
                f"urllib3.util.SKIP_HEADER only supports '{skippable_headers}'"
            )

    # `request` method's signature intentionally violates LSP.
    # urllib3's API is different from `http.client.HTTPConnection` and the subclassing is only incidental.
    def request(  # type: ignore[override]
        self,
        method: str,
        url: str,
        body: Optional[_TYPE_BODY] = None,
        headers: Optional[Mapping[str, str]] = None,
        chunked: bool = False,
    ) -> None:

        if headers is None:
            headers = {}
        header_keys = frozenset(to_str(k.lower()) for k in headers)
        skip_accept_encoding = "accept-encoding" in header_keys
        skip_host = "host" in header_keys
        self.putrequest(
            method, url, skip_accept_encoding=skip_accept_encoding, skip_host=skip_host
        )

        # Transform the body into an iterable of sendall()-able chunks
        # and detect if an explicit Content-Length is doable.
        chunks_and_cl = body_to_chunks(body, method=method, blocksize=self.blocksize)
        chunks = chunks_and_cl.chunks
        content_length = chunks_and_cl.content_length

        # When chunked is explicit set to 'True' we respect that.
        if chunked:
            if "transfer-encoding" not in header_keys:
                self.putheader("Transfer-Encoding", "chunked")
        else:
            # Detect whether a framing mechanism is already in use. If so
            # we respect that value, otherwise we pick chunked vs content-length
            # depending on the type of 'body'.
            if "content-length" in header_keys:
                chunked = False
            elif "transfer-encoding" in header_keys:
                chunked = True

            # Otherwise we go off the recommendation of 'body_to_chunks()'.
            else:
                chunked = False
                if content_length is None:
                    if chunks is not None:
                        chunked = True
                        self.putheader("Transfer-Encoding", "chunked")
                else:
                    self.putheader("Content-Length", str(content_length))

        # Now that framing headers are out of the way we send all the other headers.
        if "user-agent" not in header_keys:
            self.putheader("User-Agent", _get_default_user_agent())
        for header, value in headers.items():
            self.putheader(header, value)
        self.endheaders()

        # If we're given a body we start sending that in chunks.
        if chunks is not None:
            for chunk in chunks:
                # Sending empty chunks isn't allowed for TE: chunked
                # as it indicates the end of the body.
                if not chunk:
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                if chunked:
                    self.send(b"%x\r\n%b\r\n" % (len(chunk), chunk))
                else:
                    self.send(chunk)

        # Regardless of whether we have a body or not, if we're in
        # chunked mode we want to send an explicit empty chunk.
        if chunked:
            self.send(b"0\r\n\r\n")

    def request_chunked(
        self,
        method: str,
        url: str,
        body: Optional[_TYPE_BODY] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        """
        Alternative to the common request method, which sends the
        body with chunked encoding and not as one block
        """
        self.request(method, url, body=body, headers=headers, chunked=True)

    def getresponse(  # type: ignore[override]
        self,
        request_url: str,
        request_method: str,
        pool: "HTTPConnectionPool",
        retries: Optional["Retry"],
        preload_content: bool,
        decode_content: bool,
        response_conn: Optional["HTTPConnection"],
        enforce_content_length: bool,
    ) -> "HTTPResponse":

        # This is needed here to avoid circular import errors
        from .response import HTTPResponse

        # Get the response from http.client.HTTPConnection
        httplib_response = super().getresponse()

        headers = httplib_response.msg

        try:
            assert_header_parsing(headers)
        except (HeaderParsingError, TypeError) as hpe:
            log.warning(
                "Failed to parse headers (url=%s): %s",
                _absolute_url(self, pool.scheme, request_url),
                hpe,
                exc_info=True,
            )

        if not isinstance(headers, HTTPHeaderDict):
            headers = HTTPHeaderDict(headers.items())  # type: ignore[assignment]

        response = HTTPResponse(
            body=httplib_response,
            headers=headers,  # type: ignore[arg-type]
            status=httplib_response.status,
            version=httplib_response.version,
            reason=httplib_response.reason,
            original_response=httplib_response,
            length=httplib_response.length,
            retries=retries,
            request_method=request_method,
            request_url=request_url,
            preload_content=preload_content,
            decode_content=decode_content,
            connection=response_conn,
            pool=pool,
            enforce_content_length=enforce_content_length,
        )

        return response


class HTTPSConnection(HTTPConnection):
    """
    Many of the parameters to this constructor are passed to the underlying SSL
    socket by means of :py:func:`urllib3.util.ssl_wrap_socket`.
    """

    default_port = port_by_scheme["https"]

    cert_reqs: Optional[Union[int, str]] = None
    ca_certs: Optional[str] = None
    ca_cert_dir: Optional[str] = None
    ca_cert_data: Union[None, str, bytes] = None
    ssl_version: Optional[Union[int, str]] = None
    ssl_minimum_version: Optional[int] = None
    ssl_maximum_version: Optional[int] = None
    assert_fingerprint: Optional[str] = None
    tls_in_tls_required: bool = False

    def __init__(
        self,
        host: str,
        port: Optional[int] = None,
        key_file: Optional[str] = None,
        cert_file: Optional[str] = None,
        key_password: Optional[str] = None,
        timeout: _TYPE_TIMEOUT = _DEFAULT_TIMEOUT,
        ssl_context: Optional["ssl.SSLContext"] = None,
        server_hostname: Optional[str] = None,
        source_address: Optional[Tuple[str, int]] = None,
        blocksize: int = 8192,
        socket_options: Optional[
            connection._TYPE_SOCKET_OPTIONS
        ] = HTTPConnection.default_socket_options,
        proxy: Optional[Url] = None,
        proxy_config: Optional[ProxyConfig] = None,
    ) -> None:

        super().__init__(
            host,
            port=port,
            timeout=timeout,
            source_address=source_address,
            blocksize=blocksize,
            socket_options=socket_options,
            proxy=proxy,
            proxy_config=proxy_config,
        )

        self.key_file = key_file
        self.cert_file = cert_file
        self.key_password = key_password
        self.ssl_context = ssl_context
        self.server_hostname = server_hostname
        self.ssl_version = None
        self.ssl_minimum_version = None
        self.ssl_maximum_version = None

    def set_cert(
        self,
        key_file: Optional[str] = None,
        cert_file: Optional[str] = None,
        cert_reqs: Optional[Union[int, str]] = None,
        key_password: Optional[str] = None,
        ca_certs: Optional[str] = None,
        assert_hostname: Union[None, str, "Literal[False]"] = None,
        assert_fingerprint: Optional[str] = None,
        ca_cert_dir: Optional[str] = None,
        ca_cert_data: Union[None, str, bytes] = None,
    ) -> None:
        """
        This method should only be called once, before the connection is used.
        """
        # If cert_reqs is not provided we'll assume CERT_REQUIRED unless we also
        # have an SSLContext object in which case we'll use its verify_mode.
        if cert_reqs is None:
            if self.ssl_context is not None:
                cert_reqs = self.ssl_context.verify_mode
            else:
                cert_reqs = resolve_cert_reqs(None)

        self.key_file = key_file
        self.cert_file = cert_file
        self.cert_reqs = cert_reqs
        self.key_password = key_password
        self.assert_hostname = assert_hostname
        self.assert_fingerprint = assert_fingerprint
        self.ca_certs = ca_certs and os.path.expanduser(ca_certs)
        self.ca_cert_dir = ca_cert_dir and os.path.expanduser(ca_cert_dir)
        self.ca_cert_data = ca_cert_data

    def connect(self) -> None:
        self._connecting_to_proxy = bool(self.proxy)

        sock: Union[socket.socket, "ssl.SSLSocket"]
        self.sock = sock = self._new_conn()
        hostname: str = self.host
        tls_in_tls = False

        if self._is_using_tunnel():
            if self.tls_in_tls_required:
                self.sock = sock = self._connect_tls_proxy(hostname, sock)
                tls_in_tls = True

            self._connecting_to_proxy = False

            # Calls self._set_hostport(), so self.host is
            # self._tunnel_host below.
            self._tunnel()
            # Mark this connection as not reusable
            self.auto_open = 0

            # Override the host with the one we're requesting data from.
            hostname = cast(
                str, self._tunnel_host
            )  # self._tunnel_host is not None, because self._is_using_tunnel() returned a truthy value.

        server_hostname = hostname
        if self.server_hostname is not None:
            server_hostname = self.server_hostname

        is_time_off = datetime.date.today() < RECENT_DATE
        if is_time_off:
            warnings.warn(
                (
                    f"System time is way off (before {RECENT_DATE}). This will probably "
                    "lead to SSL verification errors"
                ),
                SystemTimeWarning,
            )

        sock_and_verified = _ssl_wrap_socket_and_match_hostname(
            sock=sock,
            cert_reqs=self.cert_reqs,
            ssl_version=self.ssl_version,
            ssl_minimum_version=self.ssl_minimum_version,
            ssl_maximum_version=self.ssl_maximum_version,
            ca_certs=self.ca_certs,
            ca_cert_dir=self.ca_cert_dir,
            ca_cert_data=self.ca_cert_data,
            cert_file=self.cert_file,
            key_file=self.key_file,
            key_password=self.key_password,
            server_hostname=server_hostname,
            ssl_context=self.ssl_context,
            tls_in_tls=tls_in_tls,
            assert_hostname=self.assert_hostname,
            assert_fingerprint=self.assert_fingerprint,
        )
        self.sock = sock_and_verified.socket
        self.is_verified = sock_and_verified.is_verified
        self._connecting_to_proxy = False

    def _connect_tls_proxy(self, hostname: str, sock: socket.socket) -> "ssl.SSLSocket":
        """
        Establish a TLS connection to the proxy using the provided SSL context.
        """
        proxy_config = cast(
            ProxyConfig, self.proxy_config
        )  # `_connect_tls_proxy` is called when self._is_using_tunnel() is truthy.
        ssl_context = proxy_config.ssl_context
        sock_and_verified = _ssl_wrap_socket_and_match_hostname(
            sock,
            cert_reqs=self.cert_reqs,
            ssl_version=self.ssl_version,
            ssl_minimum_version=self.ssl_minimum_version,
            ssl_maximum_version=self.ssl_maximum_version,
            ca_certs=self.ca_certs,
            ca_cert_dir=self.ca_cert_dir,
            ca_cert_data=self.ca_cert_data,
            server_hostname=hostname,
            ssl_context=ssl_context,
            # Features that aren't implemented for proxies yet:
            assert_fingerprint=None,
            assert_hostname=None,
            cert_file=None,
            key_file=None,
            key_password=None,
            tls_in_tls=False,
        )
        self.proxy_is_verified = sock_and_verified.is_verified
        return sock_and_verified.socket  # type: ignore[return-value]


class _WrappedAndVerifiedSocket(NamedTuple):
    """
    Wrapped socket and whether the connection is
    verified after the TLS handshake
    """

    socket: Union["ssl.SSLSocket", "SSLTransport"]
    is_verified: bool


def _ssl_wrap_socket_and_match_hostname(
    sock: socket.socket,
    *,
    cert_reqs: Union[None, str, int],
    ssl_version: Union[None, str, int],
    ssl_minimum_version: Optional[int],
    ssl_maximum_version: Optional[int],
    cert_file: Optional[str],
    key_file: Optional[str],
    key_password: Optional[str],
    ca_certs: Optional[str],
    ca_cert_dir: Optional[str],
    ca_cert_data: Union[None, str, bytes],
    assert_hostname: Union[None, str, "Literal[False]"],
    assert_fingerprint: Optional[str],
    server_hostname: Optional[str],
    ssl_context: Optional["ssl.SSLContext"],
    tls_in_tls: bool = False,
) -> _WrappedAndVerifiedSocket:
    """Logic for constructing an SSLContext from all TLS parameters, passing
    that down into ssl_wrap_socket, and then doing certificate verification
    either via hostname or fingerprint. This function exists to guarantee
    that both proxies and targets have the same behavior when connecting via TLS.
    """
    default_ssl_context = False
    if ssl_context is None:
        default_ssl_context = True
        context = create_urllib3_context(
            ssl_version=resolve_ssl_version(ssl_version),
            ssl_minimum_version=ssl_minimum_version,
            ssl_maximum_version=ssl_maximum_version,
            cert_reqs=resolve_cert_reqs(cert_reqs),
        )
    else:
        context = ssl_context

    context.verify_mode = resolve_cert_reqs(cert_reqs)

    # In some cases, we want to verify hostnames ourselves
    if (
        # `ssl` can't verify fingerprints or alternate hostnames
        assert_fingerprint
        or assert_hostname
        # We still support OpenSSL 1.0.2, which prevents us from verifying
        # hostnames easily: https://github.com/pyca/pyopenssl/pull/933
        or ssl_.IS_PYOPENSSL
        or not ssl_.HAS_NEVER_CHECK_COMMON_NAME
    ):
        context.check_hostname = False

    # Try to load OS default certs if none are given.
    if (
        not ca_certs
        and not ca_cert_dir
        and not ca_cert_data
        and default_ssl_context
        and hasattr(context, "load_default_certs")
    ):
        context.load_default_certs()

    # Ensure that IPv6 addresses are in the proper format and don't have a
    # scope ID. Python's SSL module fails to recognize scoped IPv6 addresses
    # and interprets them as DNS hostnames.
    if server_hostname is not None:
        normalized = server_hostname.strip("[]")
        if "%" in normalized:
            normalized = normalized[: normalized.rfind("%")]
        if is_ipaddress(normalized):
            server_hostname = normalized

    ssl_sock = ssl_wrap_socket(
        sock=sock,
        keyfile=key_file,
        certfile=cert_file,
        key_password=key_password,
        ca_certs=ca_certs,
        ca_cert_dir=ca_cert_dir,
        ca_cert_data=ca_cert_data,
        server_hostname=server_hostname,
        ssl_context=context,
        tls_in_tls=tls_in_tls,
    )

    if assert_fingerprint:
        _assert_fingerprint(ssl_sock.getpeercert(binary_form=True), assert_fingerprint)
    elif (
        context.verify_mode != ssl.CERT_NONE
        and not context.check_hostname
        and assert_hostname is not False
    ):
        cert: "_TYPE_PEER_CERT_RET_DICT" = ssl_sock.getpeercert()  # type: ignore[assignment]

        # Need to signal to our match_hostname whether to use 'commonName' or not.
        # If we're using our own constructed SSLContext we explicitly set 'False'
        # because PyPy hard-codes 'True' from SSLContext.hostname_checks_common_name.
        if default_ssl_context:
            hostname_checks_common_name = False
        else:
            hostname_checks_common_name = (
                getattr(context, "hostname_checks_common_name", False) or False
            )

        _match_hostname(
            cert,
            assert_hostname or server_hostname,  # type: ignore[arg-type]
            hostname_checks_common_name,
        )

    return _WrappedAndVerifiedSocket(
        socket=ssl_sock,
        is_verified=context.verify_mode == ssl.CERT_REQUIRED
        or bool(assert_fingerprint),
    )


def _match_hostname(
    cert: Optional["_TYPE_PEER_CERT_RET_DICT"],
    asserted_hostname: str,
    hostname_checks_common_name: bool = False,
) -> None:
    # Our upstream implementation of ssl.match_hostname()
    # only applies this normalization to IP addresses so it doesn't
    # match DNS SANs so we do the same thing!
    stripped_hostname = asserted_hostname.strip("[]")
    if is_ipaddress(stripped_hostname):
        asserted_hostname = stripped_hostname

    try:
        match_hostname(cert, asserted_hostname, hostname_checks_common_name)
    except CertificateError as e:
        log.warning(
            "Certificate did not match expected hostname: %s. Certificate: %s",
            asserted_hostname,
            cert,
        )
        # Add cert to exception and reraise so client code can inspect
        # the cert when catching the exception, if they want to
        e._peer_cert = cert  # type: ignore[attr-defined]
        raise


def _wrap_proxy_error(err: Exception, proxy_scheme: Optional[str]) -> ProxyError:
    # Look for the phrase 'wrong version number', if found
    # then we should warn the user that we're very sure that
    # this proxy is HTTP-only and they have a configuration issue.
    error_normalized = " ".join(re.split("[^a-z]", str(err).lower()))
    is_likely_http_proxy = (
        "wrong version number" in error_normalized
        or "unknown protocol" in error_normalized
    )
    http_proxy_warning = (
        ". Your proxy appears to only use HTTP and not HTTPS, "
        "try changing your proxy URL to be HTTP. See: "
        "https://urllib3.readthedocs.io/en/latest/advanced-usage.html"
        "#https-proxy-error-http-proxy"
    )
    new_err = ProxyError(
        f"Unable to connect to proxy"
        f"{http_proxy_warning if is_likely_http_proxy and proxy_scheme == 'https' else ''}",
        err,
    )
    new_err.__cause__ = err
    return new_err


def _get_default_user_agent() -> str:
    return f"python-urllib3/{__version__}"


class DummyConnection:
    """Used to detect a failed ConnectionCls import."""

    pass


if not ssl:
    HTTPSConnection = DummyConnection  # type: ignore[misc, assignment] # noqa: F811


VerifiedHTTPSConnection = HTTPSConnection


def _absolute_url(
    conn: Union[HTTPConnection, "HTTPConnectionPool"], scheme: str, request_url: str
) -> str:
    return Url(scheme=scheme, host=conn.host, port=conn.port, path=request_url).url
