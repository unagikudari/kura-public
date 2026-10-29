"""Optional, separately versioned source extensions installed by compose_extensions.

No implementation is imported by core at module load. An absent extension is a
normal core-only configuration; a present but broken extension fails startup.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import re
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# an extension's id is its own; core knows none of them by name
EXTENSION_ID = re.compile(r'[a-z][a-z0-9_]{0,31}')


@lru_cache(maxsize=1)
def installed() -> dict[str, dict]:
    state_path = _ROOT / '.kura-extensions.json'
    if not state_path.exists():
        return {}
    state = json.loads(state_path.read_text())
    if state.get('schema') != 1:
        raise RuntimeError('unsupported extension installation state')
    result = {}
    for ext in state['extensions']:
        name = ext['id']
        if not EXTENSION_ID.fullmatch(name) or name in result:
            raise RuntimeError('invalid extension installation state')
        for relative, expected in ext['files'].items():
            target = _ROOT / relative
            if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(_ROOT):
                raise RuntimeError(f'invalid installed extension file: {relative}')
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f'extension file changed; compose from its source repository: {relative}')
        result[name] = ext
    return result


@lru_cache(maxsize=2)
def load(name: str):
    if name not in installed():
        return None
    # A broken installed extension must not silently disappear. Only absence is optional.
    return importlib.import_module(f'api.optional.{name}')


def include_routers(app, *, machine_gate) -> list[str]:
    loaded = []
    for name in installed():
        module = load(name)
        module.include_routers(app, machine_gate=machine_gate)
        loaded.append(name)
    return loaded


def assert_composed_as_pinned() -> None:
    """Refuse when the installed extensions are not the set this core release pins.

    The pin file is this deployment's own (the public tree has none): without it there is
    nothing to compare, and whatever is installed, possibly nothing, is the install. With it,
    a checkout pulled but not yet recomposed is the stale case this refuses.
    """
    pins_path = _ROOT / 'release/extension_sources.json'
    have = {name: ext['commit'] for name, ext in installed().items()}
    if not pins_path.exists():
        return
    want = {name: pin['commit'] for name, pin in json.loads(pins_path.read_text())['extensions'].items()}
    if have != want:
        raise RuntimeError(f'installed extensions {have} are not the pinned set {want}; compose')


def require(name: str):
    from fastapi import HTTPException  # this module also serves the tagger, which has no fastapi

    module = load(name)
    if module is None:
        raise HTTPException(501, detail=f'{name} extension is not installed')
    return module


async def annotate_pools(db, pool_ids) -> dict[int, dict[str, dict]]:
    """What the installed extensions know about these pools: {pool_id: {extension id: data}}.

    An extension contributes by defining `annotate_pools(db, pool_ids) -> {pool_id: dict}`
    in its module. A `domain` in its data names the site a pool came from, for an
    extension that knows it better than the pool's own source URL does.
    """
    out: dict[int, dict[str, dict]] = {}
    if not pool_ids:
        return out
    for name in installed():
        hook = getattr(load(name), 'annotate_pools', None)
        if hook is None:
            continue
        for pool_id, data in (await hook(db, list(pool_ids))).items():
            if data:
                out.setdefault(pool_id, {})[name] = data
    return out


async def annotate_tag_links(db, links) -> dict[int, dict[str, dict]]:
    """What the installed extensions know about these artist links: {link_id: {extension id: data}}.

    `links` are {id, pool_id, url} dicts; an extension contributes by defining
    `annotate_tag_links(db, links) -> {link_id: dict}`.
    """
    out: dict[int, dict[str, dict]] = {}
    if not links:
        return out
    for name in installed():
        hook = getattr(load(name), 'annotate_tag_links', None)
        if hook is None:
            continue
        for link_id, data in (await hook(db, list(links))).items():
            if data:
                out.setdefault(link_id, {})[name] = data
    return out


async def status_cards(db) -> list[dict]:
    """The cards installed extensions add to the pipeline status page.

    Each is {title, stats: [{label, value, color?}]}, from an extension's
    `status_cards(db)`; core renders them without knowing what they count.
    """
    cards: list[dict] = []
    for name in installed():
        hook = getattr(load(name), 'status_cards', None)
        if hook is not None:
            cards += [{**card, 'extension': name} for card in await hook(db)]
    return cards


def annotated_domain(annotations: dict[str, dict]) -> str | None:
    """The domain an extension named for a pool, if one did."""
    return next((data['domain'] for data in annotations.values() if data.get('domain')), None)


def versions() -> dict[str, dict]:
    return {name: {key: ext[key] for key in ('version', 'commit')}
            for name, ext in installed().items()}
