"""取得元固有の部品を、core が**静的に依存せずに**載せるための継ぎ目 (kura#66 G6)。

G6 の要求は「site-specific adapter が無くても主要機能が動く」。宣言ではなく import で
切れていない限り、その主張は主張のままになる。

adapter の module 名は、組み込まれた拡張が自分の api.optional module に
SITE_ADAPTERS / BOOT_HOOKS として宣言する。core はどの名前も持たないので、拡張の
無い構成でも core は起動し、その面が生えないだけになる。adapter が居るかどうかは
runtime の事実であって、build 時の前提ではない。

adapter に期待する形 (すべて任意):

    router                       — APIRouter。あれば app に載る
    register_redactors(registry) — 鍵形を api.services.key_redaction へ登録する
    async def boot(db)           — 起動時に 1 回。失敗しても起動は止めない
    async def sibling_source_pool(db, url) -> int | None
                                 — url が別の取得元から既に pool 化された同じ物なら、その pool

「見つからなかった」は INFO で 1 行残す。黙って消えるのと、無い構成として動くのは
別物なので、どちらであるかは log で分かるようにする。
"""
from __future__ import annotations

import importlib
import logging

from api.services import key_redaction

_log = logging.getLogger(__name__)

def _declared(attr: str) -> tuple:
    """What installed extensions declare under `attr` (SITE_ADAPTERS / BOOT_HOOKS in
    their api.optional module). Core names no adapter itself."""
    from api import extension_hooks
    return tuple(item for name in extension_hooks.installed()
                 for item in getattr(extension_hooks.load(name), attr, ()))


def adapters() -> tuple:
    """The site adapter modules of the installed extensions."""
    return _declared("SITE_ADAPTERS")


def boot_hooks() -> tuple:
    """(module, function) pairs the installed extensions run once at startup."""
    return _declared("BOOT_HOOKS")


def _load(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        _log.info("site adapter %s は不在 — その面を持たない構成として起動する", name)
        return None
    except Exception:
        _log.exception("site adapter %s の読み込みに失敗", name)
        return None


def include_optional_routers(app, *, dependencies=None) -> list[str]:
    """居る adapter の router だけ載せ、載せた名前を返す。

    `dependencies` は core が自分の router に掛けているものと同じものを渡す
    (kura#43 item 1 の X-Api-Key gate)。継ぎ目に移した面だけ gate が外れる、
    という形にしないため — 移設は認可境界を変える操作であってはならない。

    例外は adapter が `MACHINE_GATED = False` を**明示した**とき。browser が呼ぶ面
    (dashboard の HTML、userscript の POST 先) は machine key を持てない — 持たせると
    鍵が browser と配布物の中に置かれる。除外は省略ではなく宣言で表す: 宣言が無い
    module は必ず gate の下に載るので、「書き忘れたから素通し」が起きない
    (owner 裁定 2026-09-05)。宣言した module は、代わりに何が守っているかを
    module の docstring に書く。
    """
    loaded: list[str] = []
    for name in adapters():
        mod = _load(name)
        if mod is None:
            continue
        register = getattr(mod, "register_redactors", None)
        if register is not None:
            register(key_redaction)
        router = getattr(mod, "router", None)
        if router is not None:
            # Only the boolean False opts out (#43). Anything else — no declaration, True,
            # 0, None, a string — stays under the gate; a non-boolean is logged, never
            # read for its truthiness.
            declared = getattr(mod, "MACHINE_GATED", True)
            if not isinstance(declared, bool):
                _log.warning("%s: MACHINE_GATED=%r は真偽値でない — machine gate の下に置く", name, declared)
            gated = declared is not False
            if dependencies and gated:
                app.include_router(router, dependencies=dependencies)
            else:
                if dependencies and not gated:
                    _log.info("%s は browser 面のため machine gate の対象外 "
                              "(MACHINE_GATED=False)", name)
                app.include_router(router)
            loaded.append(name)
    return loaded


async def run_boot_hooks(db) -> None:
    """fail-soft。hook が無くても落ちても、起動は続ける。"""
    for mod_name, fn_name in boot_hooks():
        mod = _load(mod_name)
        fn = getattr(mod, fn_name, None) if mod else None
        if fn is None:
            continue
        try:
            await fn(db)
        except Exception:
            _log.exception("boot hook %s.%s が失敗 (起動は継続)", mod_name, fn_name)


async def find_sibling_source_pool(db, source_url: str):
    """居る adapter に「この source_url は、別の取得元から既に pool 化された同じ物か」を
    聞く。最初に None 以外を返した adapter の答えを使う。

    adapter が居ない構成・答えられない・失敗した、はすべて None — 呼び手は従来どおり
    新しい pool を作る。重複を作る側に倒れるのは、誤って別物を 1 つの pool に混ぜる
    より取り返しが利くから。
    """
    for name in adapters():
        mod = _loaded(name)
        fn = getattr(mod, "sibling_source_pool", None) if mod is not None else None
        if fn is None:
            continue
        try:
            pid = await fn(db, source_url)
        except Exception:
            _log.exception("site adapter %s の sibling_source_pool が失敗", name)
            continue
        if pid is not None:
            return pid
    return None


_LOADED: dict = {}


def _loaded(name: str):
    """_load の結果を覚える。upload のたびに「不在」の INFO を出さないため。"""
    if name not in _LOADED:
        _LOADED[name] = _load(name)
    return _LOADED[name]
