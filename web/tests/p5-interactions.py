#!/usr/bin/env python
"""p5-interactions.py — P5 最終トランシェ (plan slug:kura-web-componentization) の
real-interaction acceptance: FlashMessage/createFlash (flash-toast-pattern) +
lib/hideVerify.ts (hide-verify-logic) + PoolVerifyBadge (pool-verify-badge)。

golden (view-smoke.py) は静止画の不変性しか証明しない — flash は過渡表示で
golden に写らず、hide-verify は cookie→param→server filter の配線、badge は
分岐鎖の意味論が対象なので実操作で assert する (plan finding 7)。

assert する契約:
  flash   … VerifyTagOverlay 下部パネルで chip 2 段階タップ→本削除で
            .vto-flash.removed (− name + 元に戻す) が出現、元に戻す click で
            .vto-flash.restored (＋ name 復元) → 3s で auto-dismiss。
            ★ 使い捨て+復元 (disposable + cleaned): 削除→undo で POST_MANGA の
            タグ集合は開始時と同一に戻る (post_tag は set 意味論 — order 列なし)。
            集合が壊れていたら PATCH 復旧ネット。route guard が対象 post への
            PATCH 以外の変異を全 abort。
  hide-verify … Header の漏斗 toggle click で kura_hideverify cookie が 1/0 に
            flip + fetchPools が hide_verify=true を渡す。v1.80.2 意味論
            (verified/excluded を隠し pending は隠さない) を実 fetch と
            pool カード badge の DOM で assert。
  badge   … PoolVerifyBadge card variant (検証待ち=橙 #e08a2e / 済み=緑 #2f9e6f)
            の computed style、chip variant (PoolTitleBar) の未設定表示。
            chip は click しない (verify-status PATCH = fixture 変異になるため)。

usage (kindle venv の playwright で実行):
    ~/kindle-capture-venv/bin/python web/tests/p5-interactions.py
    ~/kindle-capture-venv/bin/python web/tests/p5-interactions.py --capture DIR
        # A/B 用静止画 (pool 一覧 q=HEADVERIFY、count badge は mask) を保存。
        # pool 一覧は golden 集合に無い live surface なので、抽出前後の build を
        # 同条件で撮って --compare する (view-smoke と同じ ±2 tolerance)。
    ~/kindle-capture-venv/bin/python web/tests/p5-interactions.py --compare DIR_A DIR_B
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import sync_playwright

import kura_harness as H
from fixtures import MANGA_LOCALSTORAGE_BASE, ORIGIN, POOL, POOL_NAME, POST_MANGA

CHANNEL_TOLERANCE = 2  # view-smoke.py と同一の AA ジッタ許容

FAILS: list[str] = []
BLOCKED_MUTATIONS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "OK  " if cond else "FAIL"
    print(f"{mark} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def guard_routes(page, allow_patch_post: int | None = None) -> None:
    """GET/HEAD 以外を abort する変異ガード。allow_patch_post 指定時のみ
    その post への /m/post/{id} PATCH (タグ削除/undo の本体) を通す。"""
    def handler(route):
        req = route.request
        if req.method in ("GET", "HEAD"):
            return route.continue_()
        if (allow_patch_post is not None and req.method == "PATCH"
                and req.url.endswith(f"/m/post/{allow_patch_post}")):
            return route.continue_()
        BLOCKED_MUTATIONS.append(f"{req.method} {req.url}")
        return route.abort()
    page.route("**/*", handler)


def get_post_tags(page, post_id: int) -> tuple[list[str], int]:
    """(タグ名リスト(順序保持), version) — page context の cookie で認証。"""
    r = page.request.get(f"{ORIGIN}/api/post/{post_id}")
    d = r.json()
    return [t["names"][0] for t in d["tags"]], d["version"]


# ── site 1: overlay flash (FlashMessage overlay variant + createFlash) ──
def assert_overlay_flash(page) -> None:
    guard_routes(page, allow_patch_post=POST_MANGA)
    H.goto_settled(
        page,
        f"{ORIGIN}/post/{POST_MANGA}?q=pool%3A{POOL}&mode=manga",
        local_storage={
            **MANGA_LOCALSTORAGE_BASE,
            "kura_verify_header": "1",
            "kura_verify_overlay": "1",
        },
    )
    tags_before, _ = get_post_tags(page, POST_MANGA)

    dels = page.locator("button.tag-chip.tc-del")
    n0 = dels.count()
    check("flash: deletable chips render", n0 >= 2, f"count={n0}")
    if n0 < 2:
        return
    victim = dels.nth(n0 - 1)
    name = victim.evaluate("el => el.childNodes[0].textContent")

    # 2 段階タップ → 本削除 → removed flash + 元に戻す
    victim.click()
    page.wait_for_timeout(250)
    victim.click()
    try:
        page.wait_for_selector(".vto-flash.removed", timeout=10000)
    except Exception:
        check("flash: removed flash appears", False, "timeout")
        restore_tags(page, tags_before)
        return
    txt = page.locator(".vto-flash.removed .vto-flash-txt").inner_text()
    check("flash: removed flash appears", True)
    check("flash: removed text is − name", txt == f"− {name}", f"txt={txt!r} name={name!r}")
    undo = page.locator(".vto-flash .vto-undo")
    check("flash: undo button rendered", undo.count() == 1)
    check("flash: undo label 元に戻す", undo.inner_text() == "元に戻す",
          f"label={undo.inner_text()!r}")
    n1 = page.locator("button.tag-chip.tc-del").count()
    check("flash: chip removed from overlay", n1 == n0 - 1, f"{n0} → {n1}")

    # 元に戻す click → restored flash → タグ復元
    undo.click()
    try:
        page.wait_for_selector(".vto-flash.restored", timeout=10000)
        check("flash: undo → restored flash", True)
    except Exception:
        check("flash: undo → restored flash", False, "timeout")
        restore_tags(page, tags_before)
        return
    rtxt = page.locator(".vto-flash.restored .vto-flash-txt").inner_text()
    check("flash: restored text is ＋ name 復元", rtxt == f"＋ {name} 復元", f"txt={rtxt!r}")
    page.wait_for_timeout(400)
    n2 = page.locator("button.tag-chip.tc-del").count()
    check("flash: chip restored in overlay", n2 == n0, f"{n0} → {n2}")

    # restored flash は 3s で auto-dismiss (createFlash タイマー配線の実証)
    page.wait_for_timeout(3400)
    check("flash: restored auto-dismisses (3s)", page.locator(".vto-flash").count() == 0)

    # ★ cleanup 検証: fixture 不変量はタグ「集合」— post_tag に order 列は無く
    # (post.tags = set 意味論)、GET の並びは DB の格納順という付随情報。
    # golden が描く表示 (VerifyTagOverlay) はカテゴリ内 name ソートなので
    # 集合が同一なら描画も同一。集合が壊れていたら PATCH で復旧して FAIL。
    tags_after, version = get_post_tags(page, POST_MANGA)
    same_set = sorted(tags_after) == sorted(tags_before)
    check("flash: tag set unchanged after delete+undo (fixture cleaned)", same_set,
          f"before={sorted(tags_before)} after={sorted(tags_after)}")
    if not same_set:
        restore_tags(page, tags_before, version)


def restore_tags(page, tags: list[str], version: int | None = None) -> None:
    """fixture 保全ネット: POST_MANGA のタグを開始時の並びへ PATCH で戻す。"""
    if version is None:
        _, version = get_post_tags(page, POST_MANGA)
    r = page.request.patch(f"{ORIGIN}/m/post/{POST_MANGA}",
                           data={"version": version, "tags": tags})
    if not r.ok:
        print(f"WARN: fixture restore PATCH failed: {r.status}")


# ── site 2: hide-verify toggle (lib/hideVerify.ts + Header + fetchPools param) ──
def assert_hide_verify(page, ctx) -> None:
    guard_routes(page)
    H.goto_settled(page, f"{ORIGIN}/?mode=pools&q=HEADVERIFY")

    def cookie_val() -> str | None:
        for c in ctx.cookies(ORIGIN):
            if c["name"] == "kura_hideverify":
                return c["value"]
        return None

    check("hide-verify: cookie initially unset/0", cookie_val() in (None, "0"),
          f"cookie={cookie_val()}")
    pending0 = page.locator(".badge-verify-pending").count()
    done0 = page.locator(".badge-verify-done").count()
    check("hide-verify: OFF shows pending badges", pending0 >= 1, f"pending={pending0}")
    check("hide-verify: OFF shows verified badges", done0 >= 1, f"done={done0}")

    # toggle ON: fetchPools が hide_verify=true を渡すことを実 request で捕捉
    with page.expect_request(lambda r: "hide_verify=true" in r.url, timeout=10000) as req:
        page.locator("button.verify-toggle").click()
    check("hide-verify: toggle passes hide_verify=true to fetchPools",
          "api/pools" in req.value.url, f"url={req.value.url}")
    page.wait_for_timeout(800)
    check("hide-verify: cookie flips to 1", cookie_val() == "1", f"cookie={cookie_val()}")
    active = page.locator("button.verify-toggle").evaluate("el => el.classList.contains('active')")
    check("hide-verify: header toggle shows active", bool(active))

    # v1.80.2 意味論 (DOM): pending は残り、verified は消える
    pending1 = page.locator(".badge-verify-pending").count()
    done1 = page.locator(".badge-verify-done").count()
    check("hide-verify: ON keeps pending pools visible", pending1 >= 1, f"pending={pending1}")
    check("hide-verify: ON hides verified pools", done1 == 0, f"done={done1}")

    # v1.80.2 意味論 (server 応答): hide_verify=true の結果に verified/excluded ゼロ
    res = page.evaluate(
        """async () => {
             const r = await fetch('/api/pools?query=HEADVERIFY&offset=0&limit=200'
                                   + '&sort=id&dir=desc&hide_verify=true');
             const d = await r.json();
             return d.results.map(p => p.verifyStatus ?? null);
           }"""
    )
    check("hide-verify: server result has pending", "pending" in res, f"statuses={set(res)}")
    check("hide-verify: server result has no verified/excluded",
          not any(s in ("verified", "excluded") for s in res), f"statuses={set(res)}")

    # cookie 永続: reload 後も ON のまま (readHideVerify 経路)
    H.goto_settled(page, f"{ORIGIN}/?mode=pools&q=HEADVERIFY")
    active2 = page.locator("button.verify-toggle").evaluate("el => el.classList.contains('active')")
    check("hide-verify: state persists across reload (cookie read)", bool(active2))
    done2 = page.locator(".badge-verify-done").count()
    check("hide-verify: reload keeps verified hidden", done2 == 0, f"done={done2}")

    # toggle OFF に戻す (後続 section / golden への影響を残さない)
    page.locator("button.verify-toggle").click()
    page.wait_for_timeout(800)
    check("hide-verify: toggle OFF flips cookie to 0", cookie_val() == "0",
          f"cookie={cookie_val()}")


# ── site 3: PoolVerifyBadge 描画 (card 色 / chip 未設定) ──
def assert_badges(page) -> None:
    guard_routes(page)  # ここは読み取りのみ — chip click は fixture 変異なので禁止
    H.goto_settled(page, f"{ORIGIN}/?mode=pools&q=HEADVERIFY")
    pend = page.locator(".badge-verify-pending").first
    done = page.locator(".badge-verify-done").first
    if pend.count():
        bg = pend.evaluate("el => getComputedStyle(el).backgroundColor")
        check("badge: pending card badge #e08a2e", "224, 138, 46" in bg, f"bg={bg}")
        check("badge: pending card label 検", pend.inner_text() == "検")
    else:
        check("badge: pending card badge present", False, "no pending badge in view")
    if done.count():
        bg = done.evaluate("el => getComputedStyle(el).backgroundColor")
        check("badge: verified card badge #2f9e6f", "47, 158, 111" in bg, f"bg={bg}")
        check("badge: verified card label 済", done.inner_text() == "済")
    else:
        check("badge: verified card badge present", False, "no verified badge in view")

    # PoolTitleBar chip variant (POOL は verify 未設定 fixture — click しない)
    H.goto_settled(page, f"{ORIGIN}/?q=pool%3A{POOL}")
    chip = page.locator(".pool-verify-chip")
    check("badge: title-bar chip renders", chip.count() == 1)
    if chip.count():
        check("badge: chip label 検証: 未設定", chip.inner_text() == "検証: 未設定",
              f"label={chip.inner_text()!r}")
        unset = chip.evaluate("el => el.classList.contains('unset')")
        check("badge: chip unset class", bool(unset))
        name = page.locator(".pool-title-name").inner_text()
        check("badge: chip sits in PoolTitleBar of fixture pool", name == POOL_NAME,
              f"name={name!r}")


# ── A/B 静止画 (pool 一覧は golden 集合に無い live surface) ──
def ab_capture(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = H.launch_browser(p)
        ctx = H.new_context(browser)
        page = ctx.new_page()
        H.goto_settled(page, f"{ORIGIN}/?mode=pools&q=HEADVERIFY")
        page.screenshot(path=str(dest / "pool-list.png"),
                        mask=[page.locator(".pool-count-badge")])
        browser.close()
    print(f"captured A/B set → {dest}")


def ab_compare(a: Path, b: Path) -> int:
    from PIL import Image, ImageChops
    rc = 0
    for name in ["pool-list.png"]:
        ia, ib = Image.open(a / name).convert("RGB"), Image.open(b / name).convert("RGB")
        if ia.size != ib.size:
            print(f"FAIL {name}: size mismatch {ia.size} vs {ib.size}")
            rc = 1
            continue
        diff = ImageChops.difference(ia, ib)
        bad = sum(1 for px in diff.getdata() if max(px) > CHANNEL_TOLERANCE)
        if bad == 0:
            print(f"OK   {name}: pixel diff 0 (±{CHANNEL_TOLERANCE})")
        else:
            out = a.parent / f"{name}.ab-diff.png"
            diff.save(out)
            print(f"FAIL {name}: {bad} pixels differ (diff: {out})")
            rc = 1
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", metavar="DIR", help="A/B 静止画を DIR へ保存して終了")
    ap.add_argument("--compare", nargs=2, metavar=("DIR_A", "DIR_B"), help="A/B 静止画 2 組を比較")
    args = ap.parse_args()

    if args.compare:
        return ab_compare(Path(args.compare[0]), Path(args.compare[1]))
    if args.capture:
        ab_capture(Path(args.capture))
        return 0

    with sync_playwright() as p:
        browser = H.launch_browser(p)
        ctx = H.new_context(browser)
        page = ctx.new_page()
        try:
            assert_overlay_flash(page)
        finally:
            page.close()
        page = ctx.new_page()
        try:
            assert_hide_verify(page, ctx)
        finally:
            page.close()
        page = ctx.new_page()
        try:
            assert_badges(page)
        finally:
            page.close()
        browser.close()

    if BLOCKED_MUTATIONS:
        print(f"note: {len(BLOCKED_MUTATIONS)} non-GET request(s) blocked by mutation guard")
    if FAILS:
        print(f"\nFAIL: {len(FAILS)} assertion(s): " + "; ".join(FAILS))
        return 1
    print("\nOK: all P5 interaction contracts hold (mutation-guarded, fixture cleaned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
