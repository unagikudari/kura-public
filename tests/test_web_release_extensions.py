import json
import runpy
from pathlib import Path

import pytest


def test_web_release_cannot_remove_installed_extension_routes(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "web/scripts/web-release.py"
    verify = runpy.run_path(str(script))["verify_extension_pages"]
    monkeypatch.setitem(verify.__globals__, "EXTENSION_PAGES", {"example": "/example", "lora": "/lora"})
    installed = {"example": {}, "lora": {}}
    routes = {"example": 404, "lora": 303}

    def response(url):
        if url.endswith("/api/info"):
            return 200, json.dumps({"extensions": installed}).encode()
        return routes[url.rsplit("/", 1)[-1]], b""

    monkeypatch.setitem(verify.__globals__, "response", response)
    with pytest.raises(ValueError, match=r"missing installed extension routes: /example"):
        verify("http://127.0.0.1:3002")

    routes["example"] = 303
    verify("http://127.0.0.1:3002")

    installed.clear()
    routes["example"] = routes["lora"] = 404
    verify("http://127.0.0.1:3002")
