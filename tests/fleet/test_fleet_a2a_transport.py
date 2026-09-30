"""Response bounds for the official A2A HTTP transport."""

import io

import pytest

from hermes_gpt.fleet import a2a as transport


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("oversized", [False, True])
def test_http_response_read_is_bounded(monkeypatch, method, oversized):
    limit = transport._MAX_REMOTE_BYTES
    payload = b"x" * (limit + 20) if oversized else b'{"ok": true}'

    class Response(io.BytesIO):
        def read(self, size=-1):
            # A post-read size check cannot protect against unbounded allocation.
            assert size == limit + 1
            return super().read(size)

    monkeypatch.setattr(transport.urllib.request, "urlopen", lambda *a, **kw: Response(payload))

    def request():
        if method == "GET":
            return transport._http_get_json("http://127.0.0.1/card", {}, 1)
        return transport._http_post_json("http://127.0.0.1/rpc", {}, {}, 1)

    if oversized:
        with pytest.raises(ValueError, match="bounded response limit"):
            request()
    else:
        assert request() == {"ok": True}
