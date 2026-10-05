import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("headers", Path(__file__).parents[1] / "scripts/fix-awg-headers.py")
headers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(headers)


@pytest.fixture
def native(tmp_path, monkeypatch):
    p = tmp_path / "awg.conf"
    p.write_text("[Interface]\nH1 = 500-700\nH2 = 1000-2000\n")
    p.chmod(0o600)
    monkeypatch.setattr(headers, "Path", lambda _: p)
    return p


def test_already_applied_headers_are_not_changed(native, monkeypatch):
    calls = []
    def request(cmd):
        calls.append(cmd)
        return {"h1": "500-700", "h2": "1000-2000", "errno": "0"}
    monkeypatch.setattr(headers, "request", request)
    assert headers.fix()["updated"] is False
    assert calls == ["get=1\n\n", "get=1\n\n"]


def test_missing_headers_are_restored_and_verified(native, monkeypatch):
    replies = iter([{"h1": "1", "h2": "2"}, {"errno": "0"}, {"h1": "500-700", "h2": "1000-2000"}])
    monkeypatch.setattr(headers, "request", lambda _: next(replies))
    assert headers.fix() == {"h1_matches": True, "h2_matches": True, "updated": True}


def test_rejected_uapi_update_fails_closed(native, monkeypatch):
    replies = iter([{"h1": "1", "h2": "2"}, {"errno": "22"}])
    monkeypatch.setattr(headers, "request", lambda _: next(replies))
    with pytest.raises(ValueError):
        headers.fix()


def test_invalid_headers_never_reach_uapi(native, monkeypatch):
    native.write_text("[Interface]\nH1 = 900-1\nH2 = 1000-2000\n")
    monkeypatch.setattr(headers, "request", lambda _: pytest.fail("UAPI must not be called"))
    with pytest.raises(ValueError):
        headers.fix()
