"""取得元ごとの規則を**受け取る**ための core 側の継ぎ目 (kura#66 G6)。

Kura core は「この URL の pool 名は何か」「この domain はどの worker が担うか」と
いう**問い**を持つ。答えを持つのは取得元の adapter で、core は答えを知らないまま
成立する — 未知の取得元は generic な既定 (URL 末尾セグメントの名前、`default`
worker、pool scope なし) に落ちるだけで、機能が止まることはない。

なぜ registry か: 2026-09-05 の実測で、`pool_naming` / `pools` / task router の
3 file に 7 つの取得元それぞれの規則が
直接書かれていた。site を 1 つ足すたびに core を触る形で、G6 の「site-specific
adapter が無くても主要機能が動く」は成立していなかった。

adapter は **lazy に、名前で** 読み込む: 最初に問われたときに一度だけ探す。app の
起動を通らない worker / script からも同じ答えが返る必要があるため (通らない経路で
表が空になると、routing が黙って変わる)。
"""

from __future__ import annotations

import importlib
import logging
import re
from typing import Callable

_log = logging.getLogger(__name__)

#: 取得元 adapter の名前。core が持ってよいのは名前だけ。
SITE_ADAPTER = "api.site_domains"

DOMAIN_PATTERNS: list[tuple[re.Pattern[str], str]] = []
_SEARCH_CANONICALIZERS: list[tuple[str, Callable[[str], str | None]]] = []
_POOL_NAME_RULES: list[Callable[[str], str | None]] = []
_CANONICAL_URL_RULES: list[Callable[[str], str | None]] = []
_URL_VARIANT_RULES: list[Callable[[str], list[str]]] = []
_SCOPE_RULES: list[Callable[[str], str | None]] = []
_PRECREATE_VETOES: list[Callable[[str], bool]] = []
_AGENT_SUFFIX: dict[str, str] = {}
_TASK_KINDS: dict[str, Callable[[str], str]] = {}
_WALK_DOMAINS: set[str] = set()
_PLATFORM_UNCENSORED: dict[str, bool] = {}
_SEARCH_SOURCE_PATTERNS: list[tuple[str, bool]] = []  # (regex, feed のみか)
_NO_PRECREATE_DOMAINS: set[str] = set()

_loaded = False


# --- 登録 (adapter が呼ぶ) ---------------------------------------------------

def register_domain(pattern, domain: str) -> None:
    DOMAIN_PATTERNS.append(
        (re.compile(pattern) if isinstance(pattern, str) else pattern, domain))


def register_search_canonicalizer(domain: str, fn) -> None:
    global _SEARCH_CANONICALIZERS
    _SEARCH_CANONICALIZERS = [
        (d, f) for d, f in _SEARCH_CANONICALIZERS if d != domain] + [(domain, fn)]


def register_pool_name_rule(fn) -> None:
    _POOL_NAME_RULES.append(fn)


def register_canonical_url_rule(fn) -> None:
    _CANONICAL_URL_RULES.append(fn)


def register_url_variant_rule(fn) -> None:
    _URL_VARIANT_RULES.append(fn)


def register_scope_rule(fn) -> None:
    _SCOPE_RULES.append(fn)


def register_precreate_veto(fn) -> None:
    """「この URL の pool を先に作っても post が入らない」を宣言する。"""
    _PRECREATE_VETOES.append(fn)


def register_agent_suffix(domain: str, suffix: str) -> None:
    _AGENT_SUFFIX[domain] = suffix


def register_task_kind(domain: str, fn, *, walk_pipeline: bool = False,
                       precreate_pool: bool = True) -> None:
    """domain の task kind 判定と、その domain の扱いを登録する。

    walk_pipeline=True … legacy の task 表ではなく source 表を進める経路
    precreate_pool=False … pool は upload 時に per-set で作られるので先に作らない
    """
    _TASK_KINDS[domain] = fn
    if walk_pipeline:
        _WALK_DOMAINS.add(domain)
    if not precreate_pool:
        _NO_PRECREATE_DOMAINS.add(domain)


# --- 読み込み ---------------------------------------------------------------

def register_platform_censorship(platform: str, uncensored: bool) -> None:
    """その platform の作品が既定で無修正かどうか (artist link の既定値に使う)。

    登録の無い platform は「不明」— 既定値を入れず、artist ごとに確認させる。
    """
    _PLATFORM_UNCENSORED[platform] = uncensored


def register_search_source_pattern(regex: str, *, feed: bool = False) -> None:
    """search subscription / feed として扱う source URL の形。

    feed=True は「search ではないが item 単位の記録が進捗の権威にならない」もの
    (channel feed 等)。SQL 側で使うので、登録されるのは正規表現の文字列。
    """
    _SEARCH_SOURCE_PATTERNS.append((regex, feed))


def platform_uncensored_default(platform: str | None) -> bool | None:
    _ensure_loaded()
    return _PLATFORM_UNCENSORED.get((platform or "").lower())


def search_source_regex(*, include_feeds: bool = False) -> str:
    """search subscription を選ぶ SQL 正規表現。登録が無ければ**何にも一致しない**。

    取得元を 1 つも知らない構成に search subscription は存在しない — そこで
    「全部に一致」に倒すと、無関係な source を search 扱いして取り込みを止める。
    """
    _ensure_loaded()
    pats = [r for r, feed in _SEARCH_SOURCE_PATTERNS if include_feeds or not feed]
    if not pats:
        return "$^"          # POSIX ERE で決して一致しない
    return "|".join(f"({p})" for p in pats)


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True  # 失敗しても再試行しない (毎回 import を試して遅くしない)
    try:
        importlib.import_module(SITE_ADAPTER).register(_self())
    except ModuleNotFoundError:
        _log.info("%s は不在 — 取得元を 1 つも知らない構成として動く", SITE_ADAPTER)
    except Exception:
        _log.exception("%s の登録に失敗", SITE_ADAPTER)


def _self():
    import sys
    return sys.modules[__name__]


# --- 問い (core が呼ぶ) ------------------------------------------------------

def detect_domain(url: str | None) -> str | None:
    """URL の domain。未知の http(s) は 'default'、URL 自体が無ければ None。"""
    if not url:
        return None
    _ensure_loaded()
    for pat, dom in DOMAIN_PATTERNS:
        if pat.search(url):
            return dom
    return "default"


def registered_domains() -> list[str]:
    _ensure_loaded()
    out: list[str] = []
    for _pat, dom in DOMAIN_PATTERNS:
        if dom not in out:
            out.append(dom)
    return out


def canonical_search_identity(url: str | None) -> str | None:
    """search URL の canonical identity。名乗る adapter が無ければ None。"""
    if not url:
        return None
    _ensure_loaded()
    for _domain, fn in _SEARCH_CANONICALIZERS:
        got = fn(url)
        if got:
            return got
    return None


def pool_name(url: str) -> str | None:
    _ensure_loaded()
    for fn in _POOL_NAME_RULES:
        got = fn(url)
        if got:
            return got
    return None


def canonical_url(url: str) -> str | None:
    _ensure_loaded()
    for fn in _CANONICAL_URL_RULES:
        got = fn(url)
        if got:
            return got
    return None


def url_variants(url: str) -> list[str]:
    """同じ対象を指す別 URL (mirror 等)。既定は無し。"""
    _ensure_loaded()
    out: list[str] = []
    for fn in _URL_VARIANT_RULES:
        out += fn(url) or []
    return out


def scope(url: str) -> str | None:
    _ensure_loaded()
    for fn in _SCOPE_RULES:
        got = fn(url)
        if got:
            return got
    return None


def precreate_eligible(url: str) -> bool:
    """pool を先に作ってよいか。veto した adapter が 1 つでもあれば False。"""
    _ensure_loaded()
    return not any(veto(url) for veto in _PRECREATE_VETOES)


def agent_suffix(domain: str | None) -> str:
    _ensure_loaded()
    return _AGENT_SUFFIX.get(domain or "default", "default")


def task_kind(domain: str | None, url: str) -> str | None:
    _ensure_loaded()
    fn = _TASK_KINDS.get(domain or "")
    return fn(url) if fn else None


def walk_pipeline_domains() -> set[str]:
    _ensure_loaded()
    return set(_WALK_DOMAINS)


def precreates_pool(domain: str | None) -> bool:
    _ensure_loaded()
    return (domain or "") not in _NO_PRECREATE_DOMAINS
