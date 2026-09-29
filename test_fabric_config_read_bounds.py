"""Configuration size limits apply before file contents enter memory."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

import operator_fabric_config as config
import operator_fabric_router as router
from operator_fabric_protocol import _MAX_BODY, FabricError


@pytest.mark.parametrize("loader", [config._read_closed_json, router.load_routing_policy])
def test_configuration_read_is_bounded_before_json_parsing(tmp_path, monkeypatch, loader):
    path = tmp_path / "routing.json"
    path.write_bytes(b" " * (_MAX_BODY + 2))
    original_open = Path.open
    requested_sizes = []

    class ObservedReader(io.BytesIO):
        def read(self, size=-1):
            requested_sizes.append(size)
            assert size == _MAX_BODY + 1
            return super().read(size)

    def observed_open(self, *args, **kwargs):
        if self == path:
            return ObservedReader(b" " * (_MAX_BODY + 2))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed_open)
    with pytest.raises(FabricError) as error:
        loader(path)
    assert error.value.code == "FABRIC_PAYLOAD_TOO_LARGE"
    assert requested_sizes == [_MAX_BODY + 1]
