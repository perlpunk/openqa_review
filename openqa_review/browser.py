import codecs
import json
import logging
import os.path
import sys
import errno
from urllib.parse import quote, unquote, urljoin, urlparse
import ssl

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from sortedcontainers import SortedDict

logging.basicConfig()
log = logging.getLogger(sys.argv[0] if __name__ == "__main__" else __name__)
logging.captureWarnings(True)  # see https://urllib3.readthedocs.org/en/latest/security.html#disabling-warnings


class DownloadError(Exception):
    """content could not be downloaded as requested."""

    pass


class CacheNotFoundError(DownloadError):
    """content could not retrieved from cache."""

    pass

class BugzillaError(Exception):
    """Bugzilla API returned error."""

    def __init__(self, url, code, msg):
        self.url = url
        self.code = code
        self.message = msg

    def __str__(self):
        """Return as markdown list item."""
        return "Error retrieving '%s': code=%s msg='%s'" % (self.url, self.code, self.message)

class BugNotFoundError(BugzillaError):
    """A bugref points to a non-existing bug URL."""

    pass


def url_to_filename(url):
    """
    Convert URL to a valid, unambigous filename.

    >>> url_to_filename('http://openqa.opensuse.org/tests/foo/3')
    'http%3A::openqa.opensuse.org:tests:foo:3'
    """
    return quote(url).replace("/", ":")


def filename_to_url(name):
    """
    Convert filename generated by 'url_to_filename' back to valid URL.

    >>> str(filename_to_url('http%3A::openqa.opensuse.org:tests:foo:3'))
    'http://openqa.opensuse.org/tests/foo/3'
    """
    return unquote(name.replace(":", "/"))


class Browser(object):

    """download relative or absolute url and return soup."""

    def __init__(self, args, root_url, auth=None, headers={}):
        """Construct a browser object with options."""
        self.save = args.save if hasattr(args, "save") else False
        self.load = args.load if hasattr(args, "load") else False
        self.load_dir = args.load_dir if hasattr(args, "load_dir") else "."
        self.save_dir = args.save_dir if hasattr(args, "save_dir") else "."
        self.dry_run = args.dry_run if hasattr(args, "dry_run") else False
        self.root_url = root_url
        self.auth = auth
        headers["User-Agent"] = "openqa-review (https://os-autoinst.github.io/openqa_review)"
        self.headers = headers
        self.cache = {}

    def get_soup(self, url):
        """Return content from URL as 'BeautifulSoup' output."""
        assert url, "url can not be None"
        return BeautifulSoup(self.get_page(url), "html.parser")

    def get_json(self, url, cache=True):
        """Call get_page retrieving json API output."""
        return self.get_page(url, as_json=True, cache=cache)

    def get_page(self, url, as_json=False, cache=True):
        """Return content from URL as string.

        If object parameter 'load' was specified, the URL content is loaded
        from a file.
        """
        if url in self.cache and cache:
            log.info("Loading content instead of URL %s from in-memory cache" % url)
            return json.loads(self.cache[url]) if as_json else self.cache[url]
        filename = url_to_filename(url)
        if self.load and cache:
            log.info("Loading content instead of URL %s from filename %s" % (url, filename))
            try:
                raw = codecs.open(os.path.join(self.load_dir, filename), "r", "utf8").read()
            except IOError as e:
                if e.errno == errno.ENOENT:
                    msg = "Request to %s was not successful, file %s not found" % (url, filename)
                    log.info(msg)
                    # as 'load' simulates downloading we also have to simulate an appropriate error
                    raise CacheNotFoundError(msg)
                else:  # pragma: no cover
                    raise
            content = json.loads(raw) if as_json else raw
        else:  # pragma: no cover
            absolute_url = url if not url.startswith("/") else urljoin(str(self.root_url), str(url))
            content = self._get(absolute_url, as_json=as_json)
        raw = json.dumps(content) if as_json else content
        if self.save:
            log.info("Saving content instead from URL %s from filename %s" % (url, filename))
            codecs.open(os.path.join(self.save_dir, filename), "w", "utf8").write(raw)
        self.cache[url] = raw
        return content

    def _get(self, url, as_json=False):  # pragma: no cover
        retries = Retry(total=7, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
        http = requests.Session()
        parsed_url = urlparse(url)
        http.mount("{}://".format(parsed_url.scheme), HTTPAdapter(max_retries=retries))

        try:
            r = http.get(url, auth=self.auth, timeout=30, headers=self.headers)
        except requests.exceptions.SSLError as e:
            try:
                import OpenSSL
            except ImportError:
                raise e
            # as we go one layer deeper from http now, we're just interested in the hostname
            server_name = parsed_url.netloc
            cert = ssl.get_server_certificate((server_name, 443))
            x509 = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, cert)
            issuer_components = x509.get_issuer().get_components()
            # we're only interested in the b'O'rganizational unit
            issuers = filter(lambda component: component[0] == b"O", issuer_components)
            issuer = next(issuers)[1].decode("utf-8", "ignore")
            sha1digest = x509.digest("sha1").decode("utf-8", "ignore")
            sha256digest = x509.digest("sha256").decode("utf-8", "ignore")
            msg = 'Certificate for "%s" from "%s" (sha1: %s, sha256 %s) is not trusted by the system' % (
                server_name,
                issuer,
                sha1digest,
                sha256digest,
            )
            log.error(msg)
            raise DownloadError(msg)
        except requests.exceptions.ConnectionError as e:
            msg = "Request to {} was not successful after {} retries: {}".format(url, retries.total, str(e))
            log.warn(msg)
            raise DownloadError(msg)
        except requests.exceptions.ChunkedEncodingError as e:
            msg = "Request to {} was not successful: {}".format(url, str(e))
            log.warn(msg)
            raise DownloadError(msg)
        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            msg = "Request to {} failed: {}".format(url, str(e))
            log.warn(msg)
            raise DownloadError(msg)
        return self._decode_content(url, r.content.decode("utf-8"), as_json)

    def _decode_content(self, url, raw, as_json=False):
        try:
            content = json.loads(raw) if as_json else raw
        except json.decoder.JSONDecodeError as e:
            msg = 'Unable to decode JSON for {}: {} (Content was: "{}")'.format(url, str(e), raw)
            log.warning(msg)
            raise DownloadError(msg)
        return content

    def json_rpc_get(self, url, method, params, cache=True):
        """Execute JSON RPC GET request."""
        absolute_url = url if not url.startswith("/") else urljoin("http://dummy/", str(url))
        get_params = SortedDict({"method": method, "params": json.dumps([params])})
        get_url = requests.Request("GET", absolute_url, params=get_params).prepare().url
        response = self.get_json(get_url.replace("http://dummy", ""), cache)
        if "error" in response and response["error"] is not None:
            error = response["error"]
            if error["code"] == 101:
                raise BugNotFoundError(get_url, error["code"], error["message"])
            else:
                raise BugzillaError(get_url, error["code"], error["message"])
        return response

    def json_rpc_post(self, url, method, params):
        """Execute JSON RPC POST request.

        Supports a 'dry-run' which is only simulating the request with a log message.
        """
        if self.dry_run:
            log.warning("NOT sending '%s' request to '%s' with params %r" % (method, url, params))
            return {}
        else:  # pragma: no cover
            absolute_url = url if not url.startswith("/") else urljoin(str(self.root_url), str(url))
            data = json.dumps({"method": method, "params": [params]})
            for i in range(1, 7):
                try:
                    headers = self.headers.copy()
                    headers["content-type"] = "application/json"
                    r = requests.post(absolute_url, data=data, auth=self.auth, headers=headers)
                    r.raise_for_status()
                except requests.exceptions.ConnectionError:
                    log.info("Connection error encountered accessing %s, retrying try %s" % (absolute_url, i))
                    continue
                break
            else:
                msg = "Request to %s was not successful after multiple retries, giving up" % absolute_url
                log.warn(msg)
                return None
            return r.json() if r.text else None

    def json_rest(self, url, method, data):
        """Execute JSON REST request.

        Supports a 'dry-run' which is only simulating the request with a log message.
        """
        if self.dry_run and method.upper() != "GET":
            log.warning("NOT sending '%s' request to '%s' with params %r" % (method, url, data))
            return {}
        else:  # pragma: no cover
            absolute_url = url if not url.startswith("/") else urljoin(str(self.root_url), str(url))
            data = json.dumps(data)
            headers = self.headers.copy()
            headers["content-type"] = "application/json"
            r = requests.request(
                method,
                absolute_url,
                data=data,
                headers=headers,
            )
            r.raise_for_status()
            return r.json() if r.text else None


def add_load_save_args(parser):
    load_save = parser.add_mutually_exclusive_group()
    load_save.add_argument(
        "--save",
        action="store_true",
        help="""Save downloaded webpages and test data to local
                           folder. Name is autogenerated. This could be useful
                           for test investigation, loading same results for
                           another run of report generation with "--load" or
                           debugging""",
    )
    load_save.add_argument(
        "--load",
        action="store_true",
        help="""Use previously downloaded webpages and data.
                           See '--save'.""",
    )
    parser.add_argument(
        "--load-dir",
        default=".",
        help="""The directory to read cache files from when
                        using '--load'.""",
    )
    parser.add_argument(
        "--save-dir",
        default=".",
        help="""The directory to write cache files to when
                        using '--save'.""",
    )
