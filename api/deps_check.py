"""venv が `api/requirements.txt` と同期しているかを**起動時に**検査する。

## なぜ必要か (2026-07-30 実地)
trainer の kura-api が **8 時間以上落ちていた**。原因は venv に `mcp` が入っていない
ことで、`api/requirements.txt` には `mcp>=1.0` が**宣言済み**だった — つまり
「依存が未宣言」ではなく「**venv が自分の requirements と同期していない**」。

見つけにくかった理由がそのまま設計要件になっている:

* 症状が **`api/routers/mcp_server.py` の中の `ModuleNotFoundError`** として出るので、
  「router が壊れた」ように見えて「venv が古い」に辿り着かない。
* `Restart=` で **20,000 回以上 respawn** していた。journal は同じ traceback で埋まる。
* `systemctl show` の **`NRestarts=0` は嘘をつく** — 死ぬのは worker で unit ではない。
  疎通の判定は `ss -ltn | grep 8001` しか信用できない。

だから本 module がやるのは **traceback を診断に変えること**。router を import する
**前に**走らせて、「どの package が足りないか」「何を打てば直るか」を 1 行で出す。

## 落とさない (既定は warn)
起動を止めても respawn ループは変わらないので、**既定では警告だけ**。
ここを hard fail にすると、requirements の書き方 (extras / marker / 配布名の違い) を
本 module が読み違えた瞬間に**自分が原因の停止**を作る。それは直したい障害より悪い。
`KURA_STRICT_DEPS=1` を置いたときだけ落ちる (deploy の前検査用)。

standalone でも走る (deploy の前検査はこれ):
    python3 -m api.deps_check                      # 警告のみ、常に exit 0
    KURA_STRICT_DEPS=1 python3 -m api.deps_check   # 不足があれば exit 1
    KURA_REQUIREMENTS=/path/to/req.txt python3 -m api.deps_check   # 対象の差し替え
"""
from __future__ import annotations

import os
import pathlib
import sys

# 既定は repo 内の requirements。`KURA_REQUIREMENTS` で差し替えられる —
# 検査そのものを検査するため (異常系を実サービスで再現する手段が無いと、
# 「動いているつもりの検査」になる。実際この module の最初の版は無検出だった)。
REQ_FILE = pathlib.Path(
    os.environ.get("KURA_REQUIREMENTS")
    or pathlib.Path(__file__).resolve().parent / "requirements.txt")


def _problems(req_file: pathlib.Path | None = None) -> list[str]:
    """不足 / 版違反の一覧。**判定できないものは黙って通す** (誤検知で止めない)。

    `req_file` を default 引数で `REQ_FILE` に束縛しない — def 時に値が固まるので
    module 変数を差し替えても効かず、**テストが常に空を返して「合格」に見えた**
    (2026-07-30、この検査自体の最初の版がそれで無検出だった)。
    """
    req_file = req_file if req_file is not None else REQ_FILE
    try:
        import importlib.metadata as md
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name
    except Exception:                                   # noqa: BLE001
        # packaging が無い環境では検査自体を諦める。検査の不在で起動を妨げない。
        return []
    if not req_file.exists():
        return []

    installed: dict[str, str] = {}
    for dist in md.distributions():
        name = (dist.metadata or {}).get("Name")
        if name:
            installed[canonicalize_name(name)] = dist.version

    out: list[str] = []
    for raw in req_file.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):            # -r / -e 等は追わない
            continue
        try:
            req = Requirement(line)
        except Exception:                               # noqa: BLE001
            continue                                    # 読めない行は判定しない
        # 環境マーカー (sys_platform 等) が偽なら対象外
        if req.marker is not None and not req.marker.evaluate():
            continue
        key = canonicalize_name(req.name)
        have = installed.get(key)
        if have is None:
            out.append(f"{req.name}: 未 install (要求 {req.specifier or 'any'})")
            continue
        # extras (uvicorn[standard] 等) の中身までは追わない — 配布本体の有無と版だけ見る
        if req.specifier and not req.specifier.contains(have, prereleases=True):
            out.append(f"{req.name}: {have} が入っているが要求は {req.specifier}")
    return out


def check(strict: bool | None = None,
          req_file: pathlib.Path | None = None) -> list[str]:
    """検査して stderr に出す。strict なら不足があれば SystemExit(1)。"""
    if strict is None:
        strict = os.environ.get("KURA_STRICT_DEPS", "") not in ("", "0", "false", "False")
    probs = _problems(req_file)
    if not probs:
        return probs
    venv = pathlib.Path(sys.executable).parent
    head = ("[deps] venv が api/requirements.txt と同期していません — "
            f"{len(probs)} 件。**router の ModuleNotFoundError はこれが原因**")
    fix = f"[deps] 直し方: {venv}/pip install -r {req_file or REQ_FILE}"
    for ln in (head, *(f"[deps]   - {p}" for p in probs), fix):
        print(ln, file=sys.stderr, flush=True)
    if strict:
        raise SystemExit(1)
    return probs


if __name__ == "__main__":
    check()
