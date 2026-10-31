#!/usr/bin/env python
"""p3-interactions.py — TagSuggestInput 抽出 (plan slug:kura-web-componentization P3) の
real-interaction acceptance。

契約表 web/tests/p3-dropdown-contracts.json の全 row を、6 site の実 DOM に対する
Playwright 実操作で assert する (row id ↔ assert label が 1:1 対応)。golden
(view-smoke.py) は静止画しか証明しない — runes 抽出で壊れるのは挙動なので、
keyboard nav / commit 意味論 / divergence (Header ':' 抑止・SPV blur 非クローズ・
Lightbox 上方向 popup 等) を実測する (plan finding 2/7)。

★ 変異ゼロ保証: context-level route guard が GET 以外の全 request を abort する。
  L6/S5 (Enter 空入力 = saveTags 配線) はこの abort を逆手に取り、PATCH が
  発火して error 表示になることで「配線されている」ことを永続化ゼロで証明する。

usage (kindle venv の playwright で実行):
    ~/kindle-capture-venv/bin/python web/tests/p3-interactions.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import sync_playwright

import kura_harness as H
from fixtures import MANGA_LOCALSTORAGE_BASE, ORIGIN, POOL, POST_MANGA, POST_SINGLE, VERIFY_TARGET_TAG

FAILS: list[str] = []
BLOCKED_MUTATIONS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "OK  " if cond else "FAIL"
    print(f"{mark} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def guard_route(route) -> None:
    """GET 以外は全て abort — fixture 変異ゼロの機械保証。"""
    if route.request.method == "GET":
        route.continue_()
    else:
        BLOCKED_MUTATIONS.append(f"{route.request.method} {route.request.url}")
        route.abort()


SUG = ".tsi-sugs"
ITEM = ".tsi-sug"
HL = ".tsi-sug.hl"


def wait_dropdown(page, scope, timeout=6000):
    scope.locator(SUG).wait_for(state="visible", timeout=timeout)


def type_query(page, inp, text: str) -> None:
    inp.click()
    inp.fill("")
    inp.type(text, delay=25)


# ── site 1: BulkTagBar (bulk-grid @ /?bulk=1 / bulk-manga @ verify header) ──
def assert_bulk(page) -> None:
    H.goto_settled(page, f"{ORIGIN}/?q=pool%3A{POOL}&bulk=1")
    bar = page.locator(".btb.g")
    inp = bar.locator(".tsi-input")

    # B1 trigger: 220ms debounce → ≤8 件
    type_query(page, inp, "so")
    wait_dropdown(page, bar)
    n = bar.locator(ITEM).count()
    check("B1 bulk: input → dropdown (≤8)", 0 < n <= 8, f"count={n}")

    # B10 states: name+usages+catColor / gibberish → 非表示
    has_usage = bar.locator(f"{ITEM} .tsi-usage").count() == n
    style0 = bar.locator(ITEM).first.get_attribute("style") or ""
    check("B10 bulk: items have usages + catColor inline style", has_usage and "color" in style0,
          f"usage={has_usage} style={style0!r}")

    # B2 keyboard: ArrowDown/Up clamp
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    idx2 = bar.locator(ITEM).evaluate_all("els => els.findIndex(e => e.classList.contains('hl'))")
    page.keyboard.press("ArrowUp")
    idx1 = bar.locator(ITEM).evaluate_all("els => els.findIndex(e => e.classList.contains('hl'))")
    check("B2 bulk: ArrowDown/Up move highlight", idx2 == 1 and idx1 == 0, f"down2={idx2} up1={idx1}")

    # B3 commit: Enter with highlight → input=picked name (残る) + target chip
    picked = bar.locator(ITEM).first.locator(".tsi-name").inner_text()
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)
    val = inp.input_value()
    chip = bar.locator(".chip")
    check("B3 bulk: Enter(hl) → value kept = picked, chip set, dropdown closed",
          val == picked and chip.count() == 1 and chip.inner_text() == picked
          and bar.locator(SUG).count() == 0,
          f"val={val!r} picked={picked!r} chips={chip.count()}")

    # B5 commit-raw: highlight 無し Enter → 生値 commit
    type_query(page, inp, VERIFY_TARGET_TAG)
    wait_dropdown(page, bar)
    page.keyboard.press("Enter")  # fresh results → highlight=-1
    page.wait_for_timeout(150)
    check("B5 bulk: Enter(no hl) commits raw input",
          inp.input_value() == VERIFY_TARGET_TAG and bar.locator(".chip").inner_text() == VERIFY_TARGET_TAG)

    # B6 commit-mouse: mousedown pick
    type_query(page, inp, "so")
    wait_dropdown(page, bar)
    name0 = bar.locator(ITEM).first.locator(".tsi-name").inner_text()
    bar.locator(ITEM).first.click()
    page.wait_for_timeout(150)
    check("B6 bulk: mousedown pick commits", inp.input_value() == name0 and bar.locator(SUG).count() == 0)

    # B7 escape: 閉じるが value 保持 + focus 継続
    type_query(page, inp, "so")
    wait_dropdown(page, bar)
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    focused = page.evaluate("() => document.activeElement?.classList?.contains('tsi-input')")
    check("B7 bulk: Escape closes, value kept, input focused",
          bar.locator(SUG).count() == 0 and inp.input_value() == "so" and bool(focused))

    # B8 blur: 150ms 後に閉じる (Tab で次要素へ focus 移動 — .apply は
    # 選択 0 件で disabled のため focus() では blur が発火しない)
    type_query(page, inp, "so")
    wait_dropdown(page, bar)
    page.keyboard.press("Tab")
    page.wait_for_timeout(400)
    check("B8 bulk: blur closes dropdown after 150ms", bar.locator(SUG).count() == 0)

    # B4 trigger-empty: クリアで即時非表示
    type_query(page, inp, "so")
    wait_dropdown(page, bar)
    inp.fill("")
    page.wait_for_timeout(100)
    check("B4 bulk: clearing input hides dropdown immediately", bar.locator(SUG).count() == 0)

    # B9 keyguard (manga variant): input 内 ArrowLeft がページ nav に漏れない
    H.goto_settled(
        page,
        f"{ORIGIN}/post/{POST_MANGA}?q=pool%3A{POOL}&mode=manga",
        local_storage={
            **MANGA_LOCALSTORAGE_BASE,
            "kura_verify_header": "1",
            "kura_verify_overlay": "0",
            "kura_verify_target": VERIFY_TARGET_TAG,
        },
    )
    mbar = page.locator(".btb.m")
    minp = mbar.locator(".tsi-input")
    minp.click()
    minp.fill("gre")
    url_before = page.url
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(600)
    check("B9 bulk-manga: arrows in input don't page-navigate (stopPropagation all)",
          page.url == url_before, f"{url_before} → {page.url}")


# ── site 2: Header ──
def assert_header(page) -> None:
    H.goto_settled(page, f"{ORIGIN}/?q=pool%3A{POOL}")
    wrap = page.locator("header .tsi-header")
    inp = wrap.locator(".tsi-input")

    # H1 trigger: 最後の token でサジェスト (≤8)
    type_query(page, inp, "so")
    wait_dropdown(page, wrap)
    n = wrap.locator(ITEM).count()
    check("H1 header: token → dropdown (≤8)", 0 < n <= 8, f"count={n}")

    # H9 states-position: 全幅 + catColor
    iw = inp.bounding_box()["width"]
    sw = wrap.locator(SUG).bounding_box()["width"]
    style0 = wrap.locator(ITEM).first.get_attribute("style") or ""
    check("H9 header: dropdown spans input width, items colored",
          abs(iw - sw) < 3 and "color" in style0, f"input={iw} sugs={sw} style={style0!r}")

    # H5 keyboard: open 時のみ操作
    page.keyboard.press("ArrowDown")
    idx = wrap.locator(ITEM).evaluate_all("els => els.findIndex(e => e.classList.contains('hl'))")
    check("H5 header: ArrowDown highlights when open", idx == 0, f"idx={idx}")

    # H6a commit-enter (highlight): token 置換、navigate しない
    picked = wrap.locator(ITEM).first.locator(".tsi-name").inner_text()
    url_before = page.url
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    check("H6a header: Enter(hl) replaces token, no navigation",
          inp.input_value() == f"{picked} " and page.url == url_before,
          f"val={inp.input_value()!r} url={page.url}")

    # H3 divergence: 複数 token — 最後だけ置換 + 末尾 space
    type_query(page, inp, "solo so")
    wait_dropdown(page, wrap)
    picked = wrap.locator(ITEM).first.locator(".tsi-name").inner_text()
    wrap.locator(ITEM).first.click()
    page.wait_for_timeout(150)
    check("H3 header: pick replaces ONLY last token (+trailing space)",
          inp.input_value() == f"solo {picked} ", f"val={inp.input_value()!r}")

    # H4 divergence: '-' prefix 保持
    type_query(page, inp, "-so")
    wait_dropdown(page, wrap)
    picked = wrap.locator(ITEM).first.locator(".tsi-name").inner_text()
    wrap.locator(ITEM).first.click()
    page.wait_for_timeout(150)
    check("H4 header: '-' prefix preserved on pick",
          inp.input_value() == f"-{picked} ", f"val={inp.input_value()!r}")

    # H2 divergence: ':' token はサジェスト抑止
    type_query(page, inp, f"pool:{POOL}")
    page.wait_for_timeout(700)
    check("H2 header: ':' qualifier token suppresses suggestions", wrap.locator(SUG).count() == 0)

    # H7 escape: dropdown を閉じる。type=search + preventDefault 無し (旧実装と
    # 同じ) なので Chrome native の search-clear が並走して value も消える —
    # これが legacy 挙動 (契約 H7)。focus は維持。
    type_query(page, inp, "so")
    wait_dropdown(page, wrap)
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    focused = page.evaluate("() => document.activeElement?.classList?.contains('tsi-input')")
    check("H7 header: Escape closes dropdown (+native search clear), focus kept",
          wrap.locator(SUG).count() == 0 and inp.input_value() == "" and bool(focused))

    # H8 blur: 閉じる
    type_query(page, inp, "so")
    wait_dropdown(page, wrap)
    page.locator("header button[type=submit]").focus()
    page.wait_for_timeout(400)
    check("H8 header: blur closes dropdown", wrap.locator(SUG).count() == 0)

    # H6b commit-enter-native: highlight 無し Enter → form submit → /?q=
    type_query(page, inp, VERIFY_TARGET_TAG)
    wait_dropdown(page, wrap)
    page.keyboard.press("Enter")  # highlight=-1 → 素通し = native submit
    page.wait_for_url(f"**/?q={VERIFY_TARGET_TAG}", timeout=10000)
    check("H6b header: Enter(no hl) → native form submit navigates", True)


# ── site 3: Lightbox ──
def assert_lightbox(page) -> None:
    H.goto_settled(page, f"{ORIGIN}/?q=pool%3A{POOL}&open=lightbox",
                   local_storage={"kura_open_mode": "lightbox"})
    page.wait_for_selector(".thumb", timeout=15000)
    page.locator(".thumb").first.click()
    page.wait_for_selector(".lb-tagbar", timeout=15000)
    page.wait_for_timeout(400)
    bar = page.locator(".lb-tagbar")
    wrap = bar.locator(".tsi-lightbox")
    inp = wrap.locator(".tsi-input")

    # L1 trigger: 250ms debounce → ≤10 件
    type_query(page, inp, "so")
    wait_dropdown(page, wrap)
    n = wrap.locator(ITEM).count()
    check("L1 lightbox: input → dropdown (≤10)", 0 < n <= 10, f"count={n}")

    # L2 divergence: 上方向 popup
    ib = inp.bounding_box()
    sb = wrap.locator(SUG).bounding_box()
    check("L2 lightbox: dropdown opens UPWARD", sb["y"] + sb["height"] <= ib["y"] + 2,
          f"sugs_bottom={sb['y'] + sb['height']} input_top={ib['y']}")

    # L3 divergence: open 中 .lb-tagbar.focused
    focused_cls = bar.evaluate("el => el.classList.contains('focused')")
    check("L3 lightbox: tagbar.focused while dropdown open", bool(focused_cls))

    # L4 commit: 未付与 tag を pick → chip +1、input クリア (ローカルのみ)
    chips = bar.locator(".tag-chip.tc-lightbox")
    n0 = chips.count()
    names = [t.strip() for t in bar.locator(".tag-chip.tc-lightbox .tc-name").all_inner_texts()]
    items = wrap.locator(ITEM)
    pick_idx = -1
    for i in range(items.count()):
        cand = items.nth(i).locator(".tsi-name").inner_text()
        if cand not in names:
            pick_idx = i
            break
    check("L4a lightbox: found un-pending suggestion to pick", pick_idx >= 0)
    if pick_idx >= 0:
        items.nth(pick_idx).click()
        page.wait_for_timeout(200)
        n1 = bar.locator(".tag-chip.tc-lightbox").count()
        check("L4b lightbox: pick adds chip locally + clears input",
              n1 == n0 + 1 and inp.input_value() == "" and wrap.locator(SUG).count() == 0,
              f"{n0} → {n1} val={inp.input_value()!r}")
        check("L3b lightbox: .focused drops after close",
              not bar.evaluate("el => el.classList.contains('focused')"))

        # L7 dedupe: 同じ tag を再 pick → 数変わらず
        added = items  # re-query below
        type_query(page, inp, "so")
        wait_dropdown(page, wrap)
        names_now = [t.strip() for t in bar.locator(".tag-chip.tc-lightbox .tc-name").all_inner_texts()]
        re_idx = -1
        for i in range(wrap.locator(ITEM).count()):
            if wrap.locator(ITEM).nth(i).locator(".tsi-name").inner_text() in names_now:
                re_idx = i
                break
        if re_idx >= 0:
            wrap.locator(ITEM).nth(re_idx).click()
            page.wait_for_timeout(200)
            n2 = bar.locator(".tag-chip.tc-lightbox").count()
            check("L7 lightbox: picking already-pending tag doesn't duplicate", n2 == n1, f"{n1} → {n2}")
        else:
            page.keyboard.press("Escape")
            check("L7 lightbox: picking already-pending tag doesn't duplicate", False, "no pending tag in sugs")

    # L5 commit-raw: 未マッチ生値 Enter → ローカル chip
    type_query(page, inp, "p3zz-raw-tag")
    page.wait_for_timeout(500)
    nb = bar.locator(".tag-chip.tc-lightbox").count()
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    check("L5 lightbox: Enter(raw) adds local chip",
          bar.locator(".tag-chip.tc-lightbox").count() == nb + 1 and inp.input_value() == "")

    # L8 divergence-keyguard: 入力中 ←/→ は post nav に漏れない
    type_query(page, inp, "zz")
    url_before = page.url
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(400)
    check("L8 lightbox: arrows while typing don't navigate posts", page.url == url_before,
          f"{url_before} → {page.url}")

    # L6 commit-empty-save: 空 Enter → saveTags 配線 (route guard が PATCH を abort → error 表示)
    inp.fill("")
    page.wait_for_timeout(200)
    blocked_before = len(BLOCKED_MUTATIONS)
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    err = bar.locator(".lb-save-msg").count()
    fired = any("PATCH" in b for b in BLOCKED_MUTATIONS[blocked_before:])
    check("L6 lightbox: Enter(empty) wires saveTags (PATCH fired+aborted, error msg)",
          fired and err == 1, f"patch_fired={fired} msg={err}")

    # L9 escape 3 段: dropdown close → input blur (lightbox 残存) → lightbox close
    type_query(page, inp, "so")
    wait_dropdown(page, wrap)
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    still_focused = page.evaluate("() => document.activeElement?.classList?.contains('tsi-input')")
    s1 = wrap.locator(SUG).count() == 0 and bool(still_focused) and bar.count() == 1
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    blurred = not page.evaluate("() => document.activeElement?.classList?.contains('tsi-input')")
    s2 = blurred and page.locator(".lb-tagbar").count() == 1
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    s3 = page.locator(".lb-tagbar").count() == 0
    check("L9 lightbox: Escape steps close-dropdown → blur (lb stays) → close-lb",
          s1 and s2 and s3, f"s1={s1} s2={s2} s3={s3}")

    # L10 blur: 閉じる (lightbox 再オープン — 編集は非永続なので復元済みのはず)
    page.locator(".thumb").first.click()
    page.wait_for_selector(".lb-tagbar", timeout=10000)
    page.wait_for_timeout(400)
    bar = page.locator(".lb-tagbar")
    wrap = bar.locator(".tsi-lightbox")
    inp = wrap.locator(".tsi-input")
    check("L-nonpersist lightbox: reopen restores original chips (edits were local)",
          bar.locator(".tag-chip.tc-lightbox").count() == n0)
    type_query(page, inp, "so")
    wait_dropdown(page, wrap)
    bar.locator(".lb-save").focus()
    page.wait_for_timeout(400)
    check("L10 lightbox: blur closes dropdown after 150ms", wrap.locator(SUG).count() == 0)
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")


# ── site 4: SinglePostView ──
def assert_single(page) -> None:
    H.goto_settled(page, f"{ORIGIN}/post/{POST_SINGLE}?q=pool%3A{POOL}")
    edit = page.locator(".spv-tag-edit")
    inp = edit.locator(".tsi-input")

    # S1 trigger
    type_query(page, inp, "so")
    wait_dropdown(page, edit)
    n = edit.locator(ITEM).count()
    check("S1 single: input → dropdown (≤10)", 0 < n <= 10, f"count={n}")

    # S2 position: 下方向 + 行全幅 (input より広い = save ボタン含む行に展開)
    ib = inp.bounding_box()
    sb = edit.locator(SUG).bounding_box()
    check("S2 single: dropdown below input and spans edit row",
          sb["y"] >= ib["y"] + ib["height"] - 2 and sb["width"] > ib["width"] + 20,
          f"sugs_w={sb['width']} input_w={ib['width']}")

    # S7 divergence: blur では閉じない
    edit.locator(".spv-save").focus()
    page.wait_for_timeout(400)
    check("S7 single: dropdown SURVIVES blur (no blur-close divergence)",
          edit.locator(SUG).count() == 1)

    # S3 commit: 未付与 pick → chip +1, input clear (ローカル)
    inp.click()
    chips = page.locator(".spv-tags .tag-chip.tc-single")
    n0 = chips.count()
    names = [t.strip() for t in page.locator(".spv-tags .tag-chip.tc-single .tc-name").all_inner_texts()]
    items = edit.locator(ITEM)
    pick_idx = -1
    for i in range(items.count()):
        if items.nth(i).locator(".tsi-name").inner_text() not in names:
            pick_idx = i
            break
    check("S3a single: found un-pending suggestion to pick", pick_idx >= 0)
    if pick_idx >= 0:
        items.nth(pick_idx).click()
        page.wait_for_timeout(200)
        n1 = page.locator(".spv-tags .tag-chip.tc-single").count()
        check("S3b single: pick adds chip locally + clears input",
              n1 == n0 + 1 and inp.input_value() == "" and edit.locator(SUG).count() == 0,
              f"{n0} → {n1}")

    # S4 commit-raw
    type_query(page, inp, "p3zz-raw-tag")
    page.wait_for_timeout(500)
    nb = page.locator(".spv-tags .tag-chip.tc-single").count()
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    check("S4 single: Enter(raw) adds local chip",
          page.locator(".spv-tags .tag-chip.tc-single").count() == nb + 1 and inp.input_value() == "")

    # S8 keyguard: 入力中 ←/→ は strip nav に漏れない
    type_query(page, inp, "zz")
    url_before = page.url
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(400)
    check("S8 single: arrows while typing don't navigate", page.url == url_before,
          f"{url_before} → {page.url}")

    # S5 commit-empty-save: 空 Enter → saveTags 配線 (PATCH abort → err 表示)
    inp.fill("")
    page.wait_for_timeout(200)
    blocked_before = len(BLOCKED_MUTATIONS)
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    fired = any("PATCH" in b for b in BLOCKED_MUTATIONS[blocked_before:])
    err = edit.locator(".spv-save-msg.err").count()
    check("S5 single: Enter(empty) wires saveTags (PATCH fired+aborted, error msg)",
          fired and err == 1, f"patch_fired={fired} err={err}")

    # S6 escape: open→close / closed→blur
    type_query(page, inp, "so")
    wait_dropdown(page, edit)
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    still_focused = page.evaluate("() => document.activeElement?.classList?.contains('tsi-input')")
    s1 = edit.locator(SUG).count() == 0 and bool(still_focused)
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    blurred = not page.evaluate("() => document.activeElement?.classList?.contains('tsi-input')")
    check("S6 single: Escape closes then blurs (page intact)", s1 and blurred and page.url == page.url)

    # 非永続の証明: reload で復元
    H.goto_settled(page, f"{ORIGIN}/post/{POST_SINGLE}?q=pool%3A{POOL}")
    check("S-nonpersist single: reload restores original chips",
          page.locator(".spv-tags .tag-chip.tc-single").count() == n0)


# ── site 5: post/[id] manga — dead code 削除の確認 ──
def assert_post_manga(page) -> None:
    # verify overlay ON でも '+ tag' 追加 input は存在しない (P1: .ov-tags 撤去済み)
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
    n_addinput = page.locator("input[placeholder='+ tag']").count()
    check("P1a post-manga: no '+ tag' add-input anywhere (dead code deleted)", n_addinput == 0,
          f"count={n_addinput}")

    # verify OFF の素のマンガビューアには suggestion component が 1 つも無い
    H.goto_settled(
        page,
        f"{ORIGIN}/post/{POST_MANGA}?q=pool%3A{POOL}&mode=manga",
        local_storage=dict(MANGA_LOCALSTORAGE_BASE),
    )
    n_tsi = page.locator(".tsi").count()
    n_addinput = page.locator("input[placeholder='+ tag']").count()
    check("P1b post-manga(plain): no TagSuggestInput in DOM (header 検索欄除く)",
          n_tsi <= 1 and n_addinput == 0,  # ヘッダー検索欄 (site 2) の 1 個のみ許容
          f"tsi={n_tsi} addinput={n_addinput}")


# ── site 6: tags/edit merge ──
def assert_merge(page) -> None:
    H.goto_settled(page, f"{ORIGIN}/tags/edit")
    page.wait_for_selector(".tag-row", timeout=15000)
    rows_before = page.locator(".tag-row").count()
    page.locator(".tag-row .act-btn", has_text="合併").first.click()
    panel = page.locator(".panel--row")
    panel.wait_for(state="visible", timeout=5000)
    wrap = panel.locator(".tsi-merge")
    inp = wrap.locator(".tsi-input")

    # T1 trigger
    type_query(page, inp, "so")
    wait_dropdown(page, wrap)
    n = wrap.locator(ITEM).count()
    check("T1 merge: input → dropdown (≤8)", 0 < n <= 8, f"count={n}")

    # T2 divergence: 非着色 + usages
    style0 = wrap.locator(ITEM).first.get_attribute("style")
    has_usage = wrap.locator(f"{ITEM} .tsi-usage").count() == n
    check("T2 merge: items NOT colored, usages shown",
          (style0 is None or "color" not in style0) and has_usage,
          f"style={style0!r} usage={has_usage}")

    # T3 commit: pick → value=picked、merge は実行されない
    page.keyboard.press("ArrowDown")
    picked = wrap.locator(ITEM).first.locator(".tsi-name").inner_text()
    blocked_before = len(BLOCKED_MUTATIONS)
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    no_mutation = len(BLOCKED_MUTATIONS) == blocked_before
    exec_btn = panel.locator(".save-btn.red")
    check("T3 merge: Enter(hl) sets value, closes dropdown, does NOT merge",
          inp.input_value() == picked and wrap.locator(SUG).count() == 0
          and exec_btn.is_enabled() and no_mutation
          and page.locator(".tag-row").count() == rows_before,
          f"val={inp.input_value()!r} picked={picked!r} no_mut={no_mutation}")

    # T4 commit-mouse
    type_query(page, inp, "so")
    wait_dropdown(page, wrap)
    name0 = wrap.locator(ITEM).first.locator(".tsi-name").inner_text()
    wrap.locator(ITEM).first.click()
    page.wait_for_timeout(150)
    check("T4 merge: mousedown pick sets value", inp.input_value() == name0 and wrap.locator(SUG).count() == 0)

    # T5 keyboard-enter-inert: highlight 無し Enter は何もしない
    type_query(page, inp, "so")
    wait_dropdown(page, wrap)
    url_before = page.url
    blocked_before = len(BLOCKED_MUTATIONS)
    page.keyboard.press("Enter")  # fresh → highlight=-1、onEnterNoPick 無し → 素通し (form 無し = no-op)
    page.wait_for_timeout(300)
    check("T5 merge: Enter(no hl) is inert (value/url unchanged, no request)",
          inp.input_value() == "so" and page.url == url_before
          and len(BLOCKED_MUTATIONS) == blocked_before)

    # T6 escape
    if wrap.locator(SUG).count() == 0:
        type_query(page, inp, "so")
        wait_dropdown(page, wrap)
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    focused = page.evaluate("() => document.activeElement?.classList?.contains('tsi-input')")
    check("T6 merge: Escape closes dropdown, input focused",
          wrap.locator(SUG).count() == 0 and bool(focused))

    # T7 blur
    type_query(page, inp, "so")
    wait_dropdown(page, wrap)
    panel.locator(".cancel-btn").focus()
    page.wait_for_timeout(400)
    check("T7 merge: blur closes dropdown after 150ms", wrap.locator(SUG).count() == 0)


def main() -> int:
    with sync_playwright() as p:
        browser = H.launch_browser(p)
        ctx = H.new_context(browser)
        # ★ 変異ゼロ保証: GET 以外は context 全体で abort
        ctx.route("**/*", guard_route)
        for fn in (assert_bulk, assert_header, assert_lightbox, assert_single,
                   assert_post_manga, assert_merge):
            page = ctx.new_page()
            try:
                fn(page)
            finally:
                page.close()
        browser.close()

    if BLOCKED_MUTATIONS:
        print(f"\n(blocked non-GET requests: {len(BLOCKED_MUTATIONS)} — 全て abort 済み、変異なし)")
        for b in BLOCKED_MUTATIONS:
            print(f"  BLOCKED {b}")
    if FAILS:
        print(f"\nFAIL: {len(FAILS)} assertion(s): " + "; ".join(FAILS))
        return 1
    print("\nOK: all TagSuggestInput dropdown contracts hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
