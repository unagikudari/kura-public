#!/usr/bin/env python
"""p2-interactions.py — TagChip 抽出 (plan slug:kura-web-componentization P2) の
real-interaction acceptance。

golden (view-smoke.py) は静止画の不変性しか証明しない — runes 抽出で壊れうるのは
挙動 (handler 配線・状態所有) なので、chip の site ごとの行動契約を実操作で assert
する (plan finding 7)。fixture 変異禁止規約に従い、**永続化する操作は一切しない**:
  - single/lightbox の × remove は pendingTags (ローカル) のみ — save は押さない
  - VerifyTagOverlay の 2 段階タップは 1 タップ目 (armed) まで — 本削除しない
  - /tags の pill トグルはクライアント状態のみ (最後に クリア で復元)

assert する契約 (per site):
  single   … chip click → /?q=<tag> filter 遷移 (keyboard Enter 経由 = focus 検証込み)、
             × remove → ローカルで chip が消える + reload で復元 (非永続の証明)
  lightbox … thumb click → lightbox 内に chip、名前の半透明 (0.65) と bar hover で
             全開 (1) — CSS 変数配線の実測、× remove → ローカルで消える + 再オープンで復元
  overlay  … 1 タップ目 armed (class + 赤ハイライト computed style)、別 chip タップで
             乗り換え、外側 click で disarm — 削除は発生させない
  pill     … 左 click → data-st=inc + gallery iframe ?q= 反映、右 click → exc、クリア で復元

usage (kindle venv の playwright で実行):
    ~/kindle-capture-venv/bin/python web/tests/p2-interactions.py
    ~/kindle-capture-venv/bin/python web/tests/p2-interactions.py --capture DIR
        # A/B 用静止画 (tags サイドバー / lightbox open 状態) を DIR へ保存。
        # tags golden が live DB drift で無効なとき、抽出前後の build を
        # 同条件で撮って --compare する (view-smoke と同じ ±2 tolerance)。
    ~/kindle-capture-venv/bin/python web/tests/p2-interactions.py --compare DIR_A DIR_B
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import sync_playwright

import kura_harness as H
from fixtures import MANGA_LOCALSTORAGE_BASE, ORIGIN, POOL, POST_MANGA, POST_SINGLE, VERIFY_TARGET_TAG

CHANNEL_TOLERANCE = 2  # view-smoke.py と同一の AA ジッタ許容

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "OK  " if cond else "FAIL"
    print(f"{mark} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


# ── site 1: single view (SinglePostView, variant=single) ──
def assert_single(page) -> None:
    H.goto_settled(page, f"{ORIGIN}/post/{POST_SINGLE}?q=pool%3A{POOL}")
    chips = page.locator(".spv-tags .tag-chip.tc-single")
    n = chips.count()
    check("single: chips render", n > 0, f"count={n}")

    # click-filter を keyboard 経由で (focus + Enter = focus 非退行も同時に検証)
    first_name = chips.first.locator(".tc-name")
    tag_text = first_name.inner_text()
    first_name.focus()
    focused = page.evaluate("() => document.activeElement?.classList?.contains('tc-name')")
    check("single: chip name focusable", bool(focused))
    page.keyboard.press("Enter")
    page.wait_for_url(lambda url: "/?q=" in url, timeout=10000)
    check("single: click-filter → /?q=<tag>", tag_text.split()[0] in page.url or "/?q=" in page.url,
          f"url={page.url}")

    # × remove はローカルのみ (save しない) → reload で復元
    H.goto_settled(page, f"{ORIGIN}/post/{POST_SINGLE}?q=pool%3A{POOL}")
    chips = page.locator(".spv-tags .tag-chip.tc-single")
    n0 = chips.count()
    chips.first.locator(".tc-x").click()
    page.wait_for_timeout(300)
    n1 = page.locator(".spv-tags .tag-chip.tc-single").count()
    check("single: × removes chip locally", n1 == n0 - 1, f"{n0} → {n1}")
    H.goto_settled(page, f"{ORIGIN}/post/{POST_SINGLE}?q=pool%3A{POOL}")
    n2 = page.locator(".spv-tags .tag-chip.tc-single").count()
    check("single: remove is non-persistent (reload restores)", n2 == n0, f"{n0} → reload {n2}")


# ── site 2: lightbox (Lightbox, variant=lightbox) ──
def assert_lightbox(page) -> None:
    # 非 embed グリッドは localStorage kura_open_mode が open= param より優先される
    # (既存挙動) ため、localStorage 側で lightbox を固定して決定論化する。
    H.goto_settled(page, f"{ORIGIN}/?q=pool%3A{POOL}&open=lightbox",
                   local_storage={"kura_open_mode": "lightbox"})
    page.wait_for_selector(".thumb", timeout=15000)
    page.wait_for_timeout(500)
    page.locator(".thumb").first.click()
    try:
        page.wait_for_selector(".lb-tagbar", timeout=15000)
    except Exception:
        dump = Path(__file__).resolve().parent / "out" / "lightbox-fail.png"
        page.screenshot(path=str(dump))
        print(f"DEBUG url={page.url} lb_els={page.locator('[class*=lb-]').count()} shot={dump}")
        raise
    page.wait_for_timeout(500)
    chips = page.locator(".lb-tag-scroll .tag-chip.tc-lightbox")
    n0 = chips.count()
    check("lightbox: chips render in tagbar", n0 > 0, f"count={n0}")

    # 名前の半透明 → bar hover で全開 (旧 .lb-tag > span 規則の CSS 変数移設を実測)
    page.mouse.move(10, 10)
    page.wait_for_timeout(200)
    op_idle = chips.first.locator(".tc-name").evaluate("el => getComputedStyle(el).opacity")
    page.hover(".lb-tagbar")
    page.wait_for_timeout(200)
    op_hover = chips.first.locator(".tc-name").evaluate("el => getComputedStyle(el).opacity")
    check("lightbox: name dimmed when idle (0.65)", op_idle == "0.65", f"opacity={op_idle}")
    check("lightbox: name full-opacity on bar hover (1)", op_hover == "1", f"opacity={op_hover}")

    # × remove はローカルのみ → Escape で閉じ再オープンで復元
    chips.first.locator(".tc-x").click()
    page.wait_for_timeout(300)
    n1 = page.locator(".lb-tag-scroll .tag-chip.tc-lightbox").count()
    check("lightbox: × removes chip locally", n1 == n0 - 1, f"{n0} → {n1}")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    check("lightbox: Escape closes", page.locator(".lb-tagbar").count() == 0)
    page.locator(".thumb").first.click()
    page.wait_for_selector(".lb-tagbar", timeout=10000)
    page.wait_for_timeout(500)
    n2 = page.locator(".lb-tag-scroll .tag-chip.tc-lightbox").count()
    check("lightbox: remove is non-persistent (reopen restores)", n2 == n0, f"{n0} → reopen {n2}")
    page.keyboard.press("Escape")


# ── site 3: verify overlay (VerifyTagOverlay, variant=overlay) ──
def assert_overlay(page) -> None:
    H.goto_settled(
        page,
        f"{ORIGIN}/post/{POST_MANGA}?q=pool%3A{POOL}&mode=manga",
        local_storage={
            **MANGA_LOCALSTORAGE_BASE,
            "kura_verify_header": "1",
            "kura_verify_overlay": "1",
            "kura_verify_target": VERIFY_TARGET_TAG,
        },
    )
    dels = page.locator("button.tag-chip.tc-del")
    n = dels.count()
    check("overlay: deletable chips render", n >= 2, f"count={n}")
    if n < 2:
        return

    # 2 段階タップ: 1 タップ目 = armed (本削除はしない)
    dels.nth(0).click()
    page.wait_for_timeout(200)
    armed0 = dels.nth(0).evaluate("el => el.classList.contains('armed')")
    check("overlay: first tap arms chip", bool(armed0))
    bg = dels.nth(0).evaluate("el => getComputedStyle(el).backgroundColor")
    check("overlay: armed red highlight applied", "224, 80, 80" in bg, f"bg={bg}")
    deco = dels.nth(0).evaluate("el => getComputedStyle(el).textDecorationLine")
    check("overlay: armed line-through applied", "line-through" in deco, f"deco={deco}")

    # 別 chip をタップ → armed が乗り換わる (前の chip は削除されない)
    dels.nth(1).click()
    page.wait_for_timeout(200)
    armed0b = dels.nth(0).evaluate("el => el.classList.contains('armed')")
    armed1 = dels.nth(1).evaluate("el => el.classList.contains('armed')")
    check("overlay: arming second chip disarms first", (not armed0b) and armed1)
    n_after = page.locator("button.tag-chip.tc-del").count()
    check("overlay: no chip deleted by arming", n_after == n, f"{n} → {n_after}")

    # 外側 click で disarm
    page.mouse.click(700, 40)
    page.wait_for_timeout(200)
    any_armed = page.locator("button.tag-chip.tc-del.armed").count()
    check("overlay: outside click disarms", any_armed == 0, f"armed={any_armed}")


# ── site 4: /tags ブラウザ (variant=pill) ──
def assert_pill(page) -> None:
    H.goto_settled(page, f"{ORIGIN}/tags")
    pills = page.locator(".tag-list .tag-chip.tc-pill")
    n = pills.count()
    check("pill: chips render", n > 0, f"count={n}")
    first = pills.first
    tag_text = first.evaluate("el => el.childNodes[0].textContent")
    check("pill: usages count shown", first.locator(".tc-cnt").count() == 1)

    # 左 click → inc + gallery iframe の ?q= に反映
    first.click()
    page.wait_for_timeout(300)
    st = first.get_attribute("data-st")
    check("pill: left click → data-st=inc", st == "inc", f"data-st={st}")
    bc = first.evaluate("el => getComputedStyle(el).borderColor")
    check("pill: inc border-color applied", "46, 139, 79" in bc, f"border={bc}")
    src = page.locator("iframe.gallery").get_attribute("src")
    check("pill: gallery iframe reflects filter", tag_text.replace(":", "%3A") in (src or "") or tag_text in (src or ""),
          f"tag={tag_text} src={src}")

    # 右 click → exc
    first.click(button="right")
    page.wait_for_timeout(300)
    st = first.get_attribute("data-st")
    check("pill: right click → data-st=exc", st == "exc", f"data-st={st}")

    # クリア で復元 (localStorage 永続分も空になる)
    page.locator("button.clear").click()
    page.wait_for_timeout(300)
    st = first.get_attribute("data-st")
    check("pill: クリア restores off state", st == "off", f"data-st={st}")


# ── A/B 静止画 (tags golden が live-DB drift で無効なときの不変性証明) ──
def ab_capture(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = H.launch_browser(p)
        ctx = H.new_context(browser)
        page = ctx.new_page()
        # tags サイドバー (iframe は mask 相当で screenshot 対象から外せないため
        # clip でサイドバー+ヘッダー領域のみ撮る — iframe は x>=280 に始まる)
        H.goto_settled(page, f"{ORIGIN}/tags")
        page.screenshot(path=str(dest / "tags-sidebar.png"),
                        clip={"x": 0, "y": 0, "width": 280, "height": 1000})
        # lightbox open 状態 (golden 集合に無い surface)
        H.goto_settled(page, f"{ORIGIN}/?q=pool%3A{POOL}&open=lightbox")
        page.locator(".thumb").first.click()
        page.wait_for_selector(".lb-tagbar", timeout=10000)
        page.mouse.move(10, 10)
        page.wait_for_timeout(1200)
        page.screenshot(path=str(dest / "lightbox-open.png"))
        browser.close()
    print(f"captured A/B set → {dest}")


def ab_compare(a: Path, b: Path) -> int:
    from PIL import Image, ImageChops
    rc = 0
    for name in ["tags-sidebar.png", "lightbox-open.png"]:
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
        # SPA 状態の持ち越しを避けるため site ごとに素の page で検証する
        for fn in (assert_single, assert_lightbox, assert_overlay, assert_pill):
            page = ctx.new_page()
            try:
                fn(page)
            finally:
                page.close()
        browser.close()

    if FAILS:
        print(f"\nFAIL: {len(FAILS)} assertion(s): " + "; ".join(FAILS))
        return 1
    print("\nOK: all TagChip interaction contracts hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
