#!/usr/bin/env python3
"""kura API を叩く caller に `X-Api-Key` を持たせる (kura#43 item 1)。

`kura_task_client.py` / `kura_state_client.py` は自前で header を足すが、shell script の
埋め込み python や extension の worker 群は `urllib.request.urlopen` や `requests` を直に
呼ぶ。呼び出し点ごとに header を書き足すと書き漏らしが生まれるので、**transport を
1 回差し替える**形にした。

■ host を絞る (この module の要点)

最初の実装は `build_opener().addheaders` に足しただけで、**その process の全 urlopen**
に header が乗った。同じ process は外部の第三者サイトも叩くので、
kura の machine key が第三者サイトへ飛ぶ形になっていた (2026-09-05、deploy 後に自分で
気づいて撤回)。だから header は **kura と分かった host にだけ**載せる:

  * 環境変数 (`KURA_URL` / `KURA_API` / `KURA_API_BASE` / `KURA_API_LOCAL`) の host
  * loopback (127.0.0.1 / localhost / ::1)

それ以外の host には何も足さない。鍵が無い環境では何もしない (observe 期間に鍵を
持たない caller を落とさないため)。
"""
from __future__ import annotations

import os
import urllib.parse
import urllib.request

HEADER = "X-Api-Key"
_URL_ENVS = ("KURA_URL", "KURA_API", "KURA_API_BASE", "KURA_API_LOCAL", "KURA_API_URL")
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def api_key() -> str:
    return (os.environ.get("KURA_API_KEY") or "").strip()


def kura_hosts() -> set[str]:
    """鍵を載せてよい host。env に書かれた kura の base URL と loopback。"""
    hosts = set(_LOOPBACK)
    for env in _URL_ENVS:
        value = (os.environ.get(env) or "").strip()
        if not value:
            continue
        host = urllib.parse.urlsplit(value if "//" in value else f"//{value}").hostname
        if host:
            hosts.add(host.lower())
    return hosts


def is_kura_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return bool(host) and host in kura_hosts()


class _KuraKeyHandler(urllib.request.BaseHandler):
    """kura 宛の request にだけ header を足す handler。"""

    handler_order = 100

    def http_request(self, req):
        key = api_key()
        if key and not req.has_header(HEADER) and is_kura_url(req.full_url):
            req.add_unredirected_header(HEADER, key)
        return req

    https_request = http_request


_orig_urlopen = urllib.request.urlopen


def _key_urlopen(url, data=None, *args, **kwargs):
    """`urlopen` そのものを包む。

    **なぜ opener だけでは足りないか** (2026-09-05 実測): `urlopen(req, context=ctx)`
    は `context` が渡されると **install_opener した opener を使わず**、その場で新しい
    opener を作る。caller の heredoc は全部 `ssl.create_default_context()` を渡して
    いるので、handler を入れただけでは header が乗らなかった — deploy 後も observe が
    鳴り続けた理由がこれ。呼び出しの入口で Request に足せば、どちらの経路でも乗る。
    """
    key = api_key()
    if key:
        if isinstance(url, urllib.request.Request):
            if not url.has_header(HEADER) and is_kura_url(url.full_url):
                url.add_unredirected_header(HEADER, key)
        elif isinstance(url, str) and is_kura_url(url):
            req = urllib.request.Request(url, data=data)
            req.add_unredirected_header(HEADER, key)
            url, data = req, None
    return _orig_urlopen(url, data, *args, **kwargs)


def install_api_key_opener() -> bool:
    """この process の urlopen に、kura 宛だけ header を足す。

    handler (opener 経由) と urlopen の wrap の両方を入れる — `context=` を渡す
    呼び出しは opener を迂回するため。
    """
    if not api_key():
        return False
    opener = urllib.request.build_opener(_KuraKeyHandler())
    urllib.request.install_opener(opener)
    if urllib.request.urlopen is not _key_urlopen:
        urllib.request.urlopen = _key_urlopen
    return True


def install_requests_key() -> bool:
    """`requests` を使う caller 用。Session.request を包み、kura 宛だけ header を足す。"""
    key = api_key()
    if not key:
        return False
    try:
        import requests
    except ImportError:
        return False
    if getattr(requests.sessions.Session.request, "_kura_key_wrapped", False):
        return True
    original = requests.sessions.Session.request

    def request(self, method, url, **kwargs):
        if is_kura_url(str(url)):
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault(HEADER, key)
            kwargs["headers"] = headers
        return original(self, method, url, **kwargs)

    request._kura_key_wrapped = True  # noqa: SLF001
    requests.sessions.Session.request = request
    return True


def api_key_headers(extra: dict | None = None, *, url: str | None = None) -> dict:
    """Request(headers=...) を自分で組む呼び出し点のための dict。

    `url` を渡すと kura 宛のときだけ載せる (第三者サイトへ渡す header を組む用途で
    誤って呼んでも漏れない)。
    """
    headers = dict(extra or {})
    key = api_key()
    if key and (url is None or is_kura_url(url)):
        headers[HEADER] = key
    return headers
