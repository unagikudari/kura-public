"""鍵を持ちうる文字列が durable な面へ出る前に通す、core 側の 1 箇所。

なぜ core にこれがあり、パターンが無いのか (kura#66 G6, 2026-09-05):

取り込み監査表の error 列は失敗についての散文で、失敗した URL がそこに補間される。
source によっては URL が復号鍵を fragment に持つので、2026-07-16 の 776 行が「バグ報告に
貼られやすい列」に鍵を蓄えた (migration 0146)。**書き込み口で消す**という不変量は
core のものだが、「鍵がどういう形をしているか」は site adapter の知識で、core が
知っていてよいものではない。だから core は registry だけを持ち、pattern は
adapter が登録する。

adapter が居ない構成 (public artifact) では登録がゼロで、`redact` は恒等変換に
なる。そこで鍵が素通りするのではないか — という問いへの答えは
migration 0146 が入れた鍵禁止の CHECK 制約。あの CHECK は app とは独立に鍵の形を
**拒否**するので、登録漏れは「静かに保存」ではなく「loud な INSERT 失敗」として
出る。redaction は通常運転でその CHECK を鳴らさないための層であって、
最後の砦ではない。
"""
from __future__ import annotations

from typing import Callable

_REDACTORS: list[tuple[str, Callable[[str], str]]] = []


def register(name: str, fn: Callable[[str], str]) -> None:
    """adapter が自分の鍵形を登録する。同名の再登録は差し替え (reload 耐性)。"""
    global _REDACTORS
    _REDACTORS = [(n, f) for n, f in _REDACTORS if n != name] + [(name, fn)]


def registered() -> list[str]:
    """今この process に登録されている redactor 名 (観測用)。"""
    return [n for n, _ in _REDACTORS]


def redact(text: str | None) -> str | None:
    """登録された redactor を順に適用する。未登録なら恒等変換。"""
    if not text:
        return text
    for _name, fn in _REDACTORS:
        text = fn(text)
    return text
