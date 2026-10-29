"""Tests may use local HTTP fixtures, but never the network or a live Sleeper Writer."""
import urllib.parse
import urllib.request

import pytest

from sleeper_gm import api


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    urlopen = urllib.request.urlopen
    send = api.Writer._send

    def local_urlopen(url, *args, **kwargs):
        address = url.full_url if isinstance(url, urllib.request.Request) else url
        assert urllib.parse.urlparse(address).hostname in ("127.0.0.1", "localhost", "::1"), "tests must stay offline"
        return urlopen(url, *args, **kwargs)

    def dry_send(self, *args, **kwargs):
        assert not self.live, "tests must never use a live Sleeper Writer"
        return send(self, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", local_urlopen)
    monkeypatch.setattr(api.Writer, "_send", dry_send)
