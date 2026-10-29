"""kura_harness.py — kura web UI 検証の共有 Playwright ハーネス。

plan slug:kura-web-componentization Step 2 で tmp/verify-view-smoke/round*.py
系譜 (HMAC kura_session mint + kindle venv playwright + swiftshader) を
institutionalize したもの。view-smoke.py (golden) と後続 phase の
real-interaction assert スクリプトが共用する。

実行系 (kura-shot / round7.sh と同じ):
    ~/kindle-capture-venv/bin/python web/tests/view-smoke.py ...
credentials は env (KURA_SESSION_SECRET / KURA_USER / KURA_TOKEN) があればそれを、
なければ稼働中の kura プロセス environ (/proc) と api/env から自己解決する。

再現性の作法 (plan Step 2 (b)〜(d)):
  - viewport 固定 1400×1000 (fixtures.VIEWPORT)
  - reduced_motion='reduce' で起動 + transition/animation/caret 殺しの CSS 注入
  - networkidle → document.fonts.ready → 可視 <img> 全 complete を待って撮影
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import time
from pathlib import Path

from fixtures import HOST, VIEWPORT

API_ENV = Path.home() / "kura" / "api" / "env"

# swiftshader ソフトウェアレンダリング: GPU 非依存で pixel-deterministic
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-gpu",
    "--use-gl=swiftshader",
    "--in-process-gpu",
    "--disable-dev-shm-usage",
    "--force-color-profile=srgb",
    "--hide-scrollbars",
]

# アニメーション・トランジション・キャレット点滅の完全停止 (plan Step 2 (c))。
# prefers-reduced-motion だけでは opt-in していない CSS が動き続けるため
# 全要素強制。
# .version-pill / .build-info: Header のビルドバージョン表示は release ごとに
# 変わる (VERSION bump で全 golden が偽陽性で割れる — 2026-07-19 実測) ので
# 不可視化する。UI 検証対象ではないメタ表示のみが対象。
KILL_MOTION_CSS = """
*, *::before, *::after {
  transition: none !important;
  animation: none !important;
  caret-color: transparent !important;
}
html { scroll-behavior: auto !important; }
.version-pill, .build-info { visibility: hidden !important; }
"""


def _procenv(pattern: str, key: str) -> str | None:
    """稼働プロセスの environ から値を取る (round7.sh の procenv の移植)。"""
    try:
        pid = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True
        ).stdout.split()
        if not pid:
            return None
        environ = Path(f"/proc/{pid[0]}/environ").read_bytes()
        for entry in environ.split(b"\0"):
            k, _, v = entry.partition(b"=")
            if k.decode(errors="replace") == key:
                return v.decode()
    except OSError:
        return None
    return None


def _fileval(path: Path, key: str) -> str | None:
    try:
        for line in path.read_text().splitlines():
            m = re.match(rf"^{re.escape(key)}=(.*)$", line)
            if m:
                return m.group(1).strip("\"'")
    except OSError:
        return None
    return None


#: web が session cookie の署名に使う鍵。`web/.env.server` に置かれ、web
#: プロセスの環境に入る (kura#59)。
WEB_ENV = Path(__file__).resolve().parents[1] / ".env.server"


def resolve_credentials() -> tuple[str, str, str]:
    """(secret, user, token) — env 優先、なければ稼働プロセス/env ファイルから。

    secret は **session cookie の署名鍵** で、kura#59 以降 `KURA_SESSION_SECRET`
    です。以前は `KURA_SECRET` で、無ければ literal `'change-me'` に落ちていました
    — その fallback こそが「production の cookie が repo に書いてある文字列で
    署名されている」状態を作り、しかもそれを不可視にしていたものです。ここでも
    fallback は置きません: 鍵が取れなければ、cookie を鋳造しても web 側が
    必ず弾くので、**署名できないことを黙って偽造で埋めない**。
    """
    secret = (os.environ.get("KURA_SESSION_SECRET")
              or _procenv("node build/index.js", "KURA_SESSION_SECRET")
              or _fileval(WEB_ENV, "KURA_SESSION_SECRET"))
    if not secret:
        raise RuntimeError(
            "could not resolve KURA_SESSION_SECRET (env, /proc, web/.env.server all "
            "failed). The harness mints a kura_session cookie with it; without the "
            "same key the web side rejects every cookie it makes (kura#59).")
    user = os.environ.get("KURA_USER") or _procenv("uvicorn api.main", "KURA_USER") or _fileval(API_ENV, "KURA_USER")
    token = os.environ.get("KURA_TOKEN") or _procenv("uvicorn api.main", "KURA_TOKEN") or _fileval(API_ENV, "KURA_TOKEN")
    if not user or not token:
        raise RuntimeError("could not resolve KURA_USER/KURA_TOKEN (env, /proc, api/env all failed)")
    return secret, user, token


def mint_session_cookie() -> str:
    """kura_session cookie 値 (HMAC-SHA256 署名付き payload) を鋳造する。"""
    secret, user, token = resolve_credentials()
    sess = {"u": user, "t": token, "e": "", "exp": int(time.time()) + 3600}
    payload = base64.urlsafe_b64encode(json.dumps(sess).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{payload}.{sig}"


def launch_browser(p):
    """sync_playwright ハンドル p からハーネス標準のブラウザを起動する。"""
    return p.chromium.launch(channel="chrome", args=LAUNCH_ARGS)


def new_context(browser):
    """固定 viewport + reduced-motion + 認証 cookie 済みの context。

    motion-kill CSS は init script で全 navigation に注入する (SvelteKit の
    load 直後の client-side 遷移で add_style_tag の execution context が
    破壊されるため、最初の paint から効く init script が正しい経路)。
    """
    ctx = browser.new_context(
        viewport=VIEWPORT,
        reduced_motion="reduce",
        ignore_https_errors=True,
        device_scale_factor=1,
    )
    ctx.add_init_script(
        "document.addEventListener('DOMContentLoaded', () => {"
        "  const s = document.createElement('style');"
        f"  s.textContent = {json.dumps(KILL_MOTION_CSS)};"
        "  document.documentElement.appendChild(s);"
        "});"
    )
    ctx.add_cookies([{
        "name": "kura_session",
        "value": mint_session_cookie(),
        "domain": HOST,
        "path": "/",
        "httpOnly": True,
        "secure": True,
    }])
    return ctx


def goto_settled(page, url: str, *, local_storage: dict[str, str] | None = None,
                 settle_ms: int = 1200, timeout_ms: int = 30000) -> None:
    """URL へ遷移し、描画が完全静止するまで待つ (plan Step 2 (c)(d))。

    local_storage 指定時は一度 goto → localStorage 設定 → reload
    (SvelteKit が初期描画で読むため)。
    """
    try:
        page.goto(url, wait_until="networkidle", timeout=timeout_ms)
    except Exception:
        pass  # networkidle timeout は後続の明示 wait で回収する
    if local_storage:
        _retry_eval(
            page,
            "kv => { for (const [k, v] of Object.entries(kv)) localStorage.setItem(k, v); }",
            local_storage,
        )
        try:
            page.reload(wait_until="networkidle", timeout=timeout_ms)
        except Exception:
            pass
    _retry_eval(page, "() => document.fonts.ready")
    # 可視域 (±200px マージン) の <img> が全て decode 済みになるまで待つ。
    # 遅延ロードの画面外 img は永遠に complete にならないので可視域のみ。
    try:
        page.wait_for_function(
            """() => [...document.images].every(img => {
                 const r = img.getBoundingClientRect();
                 const near = r.bottom > -200 && r.top < innerHeight + 200 &&
                              r.right > -200 && r.left < innerWidth + 200;
                 return !near || !img.currentSrc || (img.complete && img.naturalWidth > 0);
               })""",
            timeout=15000,
        )
    except Exception:
        print(f"WARN: visible-image settle timed out on {url}")
    page.wait_for_timeout(settle_ms)


def _retry_eval(page, expr: str, arg=None, attempts: int = 3):
    """load 直後の client-side 遷移で execution context が破壊されることが
    あるため、evaluate は短い wait を挟んで retry する。"""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return page.evaluate(expr, arg) if arg is not None else page.evaluate(expr)
        except Exception as e:  # noqa: BLE001 — playwright Error のみ実際は来る
            last = e
            page.wait_for_timeout(400)
    raise last
