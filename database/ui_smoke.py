#!/usr/bin/env python3
"""注釈 UI の Playwright スモーク。**UI を変えたら必ずこれを通す。**

## なぜ script にするか
「Playwright で確認する」を心がけにしておくと守れない。実際 v1.99.25 で
**犬・馬を書けるようにしたのに画面から使えていなかった** — person カードで
ラベルの存在だけ確認して creature カードを描いていなかったため、
`species` がカード末尾で画面外に落ちているのを見逃した (user 指摘)。
だから「存在するか」ではなく **`kind` を切り替えて実際に描き、位置まで測る**。

## 検査すること
1. JS エラーが 0 (console error / pageerror)
2. worklist が返り、scene が 1 件以上出る
3. **person / creature の両方**で identity 欄 (species) が **viewport 内**にある
4. creature では region グループの先頭が「非人間の部位」
5. **画面下部の combobox** を開いて popup が viewport 内に収まる
   (`below>240` の固定しきい値で 5 項目に切れていた回帰の番犬)
6. 原寸リンクが原本を指し、表示画像は縮小版を指す

## 使い方
    ~/kindle-capture-venv/bin/python database/ui_smoke.py
    ~/kindle-capture-venv/bin/python database/ui_smoke.py --url http://[::1]:8099/

exit 0 = 全部 PASS。1 件でも落ちたら exit 1 (何が落ちたかを出す)。
"""
from __future__ import annotations

import argparse
import os
import sys

URL_DEFAULT = f"http://[{os.environ.get('KURA1_ANNOTATE_HOST', '::1')}]:8099/"

CHECKS: list[tuple[str, bool, str]] = []


def ro(url: str) -> str:
    """**?ro=1 を必ず付ける。** これが無いと検査が注釈者の編集中の下書きを上書きする

    2026-07-28 に一度起き、2026-07-31 に再発した (555215 の編集中に関係 2 本と
    部位 1 つが混入)。ANN_DIR を分ける手は**サーバ起動時に決まる**ので、
    注釈者が使っているサーバに繋ぐ検査には効かない。効くのは client 側の門だけ。
    """
    return url + ("&" if "?" in url else "?") + "ro=1"


def cb_items(pg, loc) -> list:
    """combobox の候補を読む。**native select ではない**ので popup を開く必要がある。

    `select option` で読んでいた版は候補を 1 件も拾わず、「消えている」系の検査が
    **全部素通り**していた (2026-07-31 実測)。**開くのは click、項目を選ぶのは
    mousedown** — 2 つを取り違えると popup が出ないまま 0 件が返る。
    """
    loc.click()
    pg.wait_for_timeout(250)
    items = pg.eval_on_selector_all(".cb-pop .cb-list > *", "es=>es.map(e=>e.textContent)")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(150)
    return items


def chk(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=URL_DEFAULT)
    ap.add_argument("--shot", help="スクリーンショットの書き出し先")
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright

    # ── 狭い画面 (iPhone Pro Max 横) — desktop の後にまとめて検査する ──
    def mobile_checks(b, w, h, label):
        pg = b.new_page(viewport={"width": w, "height": h}, is_mobile=True,
                        has_touch=True, device_scale_factor=3)
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(ro(a.url), wait_until="networkidle", timeout=60_000)
        pg.wait_for_timeout(2000)
        r = pg.evaluate("""() => {
          const g=s=>{const e=document.querySelector(s); if(!e) return null;
            const r=e.getBoundingClientRect(), cs=getComputedStyle(e);
            return {x:r.left,y:r.top,w:r.width,h:r.height,disp:cs.display}};
          const im=g('#im'), rp=g('#panel'), lp=g('#lpanel'), tb=g('#mtabs');
          const ov=(A,B)=>{if(!A||!B||B.disp==='none') return 0;
            const x=Math.max(0,Math.min(A.x+A.w,B.x+B.w)-Math.max(A.x,B.x));
            const y=Math.max(0,Math.min(A.y+A.h,B.y+B.h)-Math.max(A.y,B.y)); return x*y};
          const area=im?im.w*im.h:0;
          // タブを切り替えて片方だけ出ることを確かめる
          let tabOK=false;
          if(tb && tb.disp!=='none'){
            setMTab('lpanel');
            tabOK = getComputedStyle(document.getElementById('panel')).display==='none'
                 && getComputedStyle(document.getElementById('lpanel')).display!=='none';
            setMTab('panel');
          }
          const btn=document.querySelector('#panel .cb-btn');
          const bh=btn?btn.getBoundingClientRect().height:0;
          // 画像は枠の**幅か高さのどちらか**に接していれば最大化されている。
          // 高さだけ / 幅だけを見る検査は、開いている post の縦横比で必ず落ちる。
          const mainEl=document.querySelector('main');
          const ms=mainEl?getComputedStyle(mainEl):null;
          const mr=mainEl?mainEl.getBoundingClientRect():{width:0,height:0};
          const padX=ms?(parseFloat(ms.paddingLeft)+parseFloat(ms.paddingRight)):0;
          const padY=ms?(parseFloat(ms.paddingTop)+parseFloat(ms.paddingBottom)):0;
          const availW=mr.width-padX, availH=mr.height-padY;
          return {imW:Math.round(im?im.w:0), imH:Math.round(im?im.h:0),
                  availW:Math.round(availW), availH:Math.round(availH),
                  覆う:area?Math.round(100*(ov(im,rp)+ov(im,lp))/area):100,
                  tabs: tb ? tb.disp!=='none' : false, tabOK,
                  btnH:Math.round(bh),
                  hscroll:document.documentElement.scrollWidth>innerWidth+1};
        }""")
        chk(f"{label}: JS 例外が無い", not errs, "; ".join(errs[:2]))
        chk(f"{label}: 画像がパネルに覆われない", r["覆う"] <= 2, f"{r['覆う']}%")
        # **幅で判定しない。** 横向きでは高さが律速なので、縦長の画像を開いていると
        # 幅は必ず小さくなる (実測 262x382)。検査すべきは「使える高さを使い切っているか」。
        # 幅を固定値で見ていた最初の版は、開いている post の縦横比で落ちた。
        chk(f"{label}: 画像が枠いっぱいに出ている",
            r["imW"] >= r["availW"] * 0.9 or r["imH"] >= r["availH"] * 0.9,
            f"{r['imW']}x{r['imH']} / 枠 {r['availW']}x{r['availH']}")
        chk(f"{label}: タブバーが出る", r["tabs"])
        chk(f"{label}: タブで片側だけ表示", r["tabOK"])
        chk(f"{label}: タップ対象が 36px 以上 (HIG)", r["btnH"] >= 36, f"{r['btnH']}px")
        chk(f"{label}: 横スクロールしない", not r["hscroll"])
        pg.close()

    with sync_playwright() as pw:
        # primary の APU では GL 指定を付けると Chrome が落ちる。既定引数で開く
        b = pw.chromium.launch(channel="chrome", args=["--disable-dev-shm-usage"])
        pg = b.new_page(viewport={"width": 1500, "height": 1050})
        # **JS 例外と HTTP エラーを分ける。** console.error には resource 404 の
        # ような JS 外の雑音が混ざるので、一緒にすると検査が常に赤くなって
        # 見なくなる (それでは番犬にならない)。
        errs: list[str] = []
        http_bad: list[str] = []
        pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
        pg.on("response", lambda r: http_bad.append(f"{r.status} {r.url}")
              if r.status >= 400 and "favicon" not in r.url else None)
        pg.goto(ro(a.url), wait_until="networkidle", timeout=60_000)
        pg.wait_for_timeout(2000)

        chk("JS 例外が無い", not errs, "; ".join(errs[:3]))
        chk("HTTP エラー応答が無い", not http_bad, "; ".join(http_bad[:3]))

        n = pg.evaluate("() => document.getElementById('nav')?.children.length || 0")
        chk("worklist (画像ナビ) が描けている", (n or 0) > 0, f"scene={n}")

        # **user と同じ操作で kind を切り替える。** `s.kind=...` を直に代入して render()
        # する検査では、combobox の paint() を通らないので
        # 「creature を選ぶとヘッダが崩れる」を**構造的に見逃す** (2026-07-30 実害)。
        # 選択は click ではなく **mousedown** — pick() はそちらに繋がっている。
        for kind, want_group in (("person", None), ("creature", "非人間の部位")):
            r = pg.evaluate(
                """(kind) => {
                  if(!ann.subjects.length) addSubject();
                  ann.subjects[0].kind='person'; render();
                  const card0=document.querySelector('#subjects .subj');
                  const h0=Math.round(card0.querySelector('header').getBoundingClientRect().height);
                  const kd=card0.querySelector('header .cb-btn'); kd.click();
                  const it=[...document.querySelectorAll('.cb-pop .cb-i')]
                            .find(e=>e.querySelector('b')?.textContent===kind);
                  if(!it) return {err:`kind の選択肢 ${kind} が無い`};
                  it.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
                  const card=document.querySelector('#subjects .subj');
                  if(!card) return {err:'カードが無い'};
                  const hd=card.querySelector('header');
                  const btn=hd.querySelector('.cb-btn');
                  const sp=[...card.querySelectorAll('label')]
                            .find(e=>/species/.test(e.textContent));
                  if(!sp) return {err:'species 欄が無い'};
                  const b=sp.getBoundingClientRect();
                  return {picked:ann.subjects[0].kind,
                          y:Math.round(b.top), inView:b.top>=0 && b.bottom<=innerHeight,
                          grp1:card.querySelector('.grp-l')?.textContent||'',
                          h0, h1:Math.round(hd.getBoundingClientRect().height),
                          btnLen:btn.textContent.trim().length};
                }""", kind)
            if r.get("err"):
                chk(f"{kind}: kind 切替", False, r["err"])
                continue
            chk(f"{kind}: 選択が反映される", r["picked"] == kind, f"実際={r['picked']}")
            chk(f"{kind}: species 欄が viewport 内", r["inView"], f"y={r['y']}")
            # 説明文の全文がボタンに入るとヘッダの flex が折り返してカードが崩れる
            chk(f"{kind}: header が崩れない", r["h1"] <= r["h0"] + 6,
                f"{r['h0']}px → {r['h1']}px")
            chk(f"{kind}: combobox のラベルが短い (説明全文でない)",
                r["btnLen"] <= 60, f"{r['btnLen']} 字")
            if want_group:
                chk(f"{kind}: region の先頭が {want_group}",
                    r["grp1"] == want_group, f"実際={r['grp1']!r}")

        # 画面下部の combobox — popup が切れないか
        r = pg.evaluate(
            """() => {
              const c=[...document.querySelectorAll('.cb-btn')].map(b=>{
                const r=b.getBoundingClientRect();
                return {b, top:r.top, below:innerHeight-r.bottom}})
                .filter(x=>x.top>0 && x.top<innerHeight && x.below<340);
              if(!c.length) return {skip:'下部に combobox が無い'};
              const x=c[c.length-1], br=x.b.getBoundingClientRect();
              x.b.click();
              const pop=document.querySelector('.cb-pop');
              if(!pop) return {err:'popup が開かない'};
              const pr=pop.getBoundingClientRect();
              const list=pop.querySelector('.cb-list');
              // **切れていないこと**は 145 項目の語彙では成り立たない不変条件だった
              // (説明を 2 行にした 2026-07-31 に露見)。本当に守りたいのは
              // 「**空いている高さを使い切っている**」— 5 項目で切れるのが問題であって、
              // スクロールできること自体は問題ではない。popup の高さは項目数で決まるので
              // 「広い側だから高いはず」も成り立たない (項目が 3 つなら低くて当然)。
              const clipped = list ? list.scrollHeight > list.clientHeight + 2 : false;
              const avail = Math.max(innerHeight - br.bottom, br.top);
              const wastes = clipped && pr.height < avail - 28;
              return {below:Math.round(x.below), above:Math.round(br.top),
                      popH:Math.round(pr.height), clipped, wastes,
                      avail:Math.round(avail),
                      inView: pr.top>=0 && pr.bottom<=innerHeight+1};
            }""")
        if r.get("skip"):
            chk("下部 combobox の popup", True, r["skip"] + " (skip)")
        elif r.get("err"):
            chk("下部 combobox の popup", False, r["err"])
        else:
            chk("下部 combobox の popup が viewport 内",
                r["inView"], f"下{r['below']} 上{r['above']} popH={r['popH']}")
            # **空いている高さを使い切っているか。** 「切れていない」で判定していた版は、
            # 説明を 2 行にした途端に落ちた — 145 項目は元々収まらない。高さで判定した
            # 版は項目が少ない combobox で必ず落ちた。使い切りなら両方を捉える。
            chk("popup が空いている高さを使い切っている", not r["wastes"],
                f"popH={r['popH']}/空き{r['avail']} 切れ={r['clipped']}")

        r = pg.evaluate("""() => ({orig:document.getElementById('orig')?.getAttribute('href')||'',
                                   im:document.getElementById('im')?.getAttribute('src')||''})""")
        chk("原寸リンクが原本を指す", bool(r["orig"]) and "?w=" not in r["orig"], r["orig"])
        chk("表示画像は縮小版", "?w=" in r["im"], r["im"])

        # ── 0062: 噴出物とピアスを独立した箱にした ────────────────────
        chk("噴出物の箱が左パネルにある", pg.locator("#emis-r").count() == 1)
        chk("出ているかの箱がある", pg.locator("#emis-a").count() == 1)
        # 候補の元になる配列そのものを見る — DOM を掘るより壊れにくい
        ea = pg.evaluate("()=>V.entity_attrs.filter(x=>!V.entity_attr_group[x])")
        chk("排出は「全体の属性」の候補に出ない",
            bool(ea) and not any("ation_state" in o or "squirting" in o for o in ea),
            f"{len(ea)}件")
        n0 = pg.locator("#emis-r .rel").count()
        nr0 = pg.locator("#rels .rel").count()
        pg.click("button:has-text('＋噴出物の行き先')")
        pg.wait_for_timeout(400)
        chk("行き先は噴出物の箱に増える (関係の箱ではない)",
            pg.locator("#emis-r .rel").count() == n0 + 1
            and pg.locator("#rels .rel").count() == nr0,
            f"emis={pg.locator('#emis-r .rel').count()} rels={pg.locator('#rels .rel').count()}")
        # **自分が足した行を見る。** nth() で数えていた版は、下書きに既に噴出物の
        # 関係が入っていると**既存行を指して**落ちた (2026-07-31 実測)。
        # 検査は fixture の中身に依存させない
        newrow = pg.locator("#emis-r .rel").last
        rel_cb = cb_items(pg, newrow.locator(".cb-btn").nth(2))
        chk("噴出物の候補は排出の語だけ",
            len(rel_cb) == 4 and not any("touching" in o or "striking" in o
                                         for o in rel_cb), f"{len(rel_cb)}件")
        src = cb_items(pg, newrow.locator(".cb-btn").nth(1))
        chk("出どころを器官に縛らない (膣口・口も選べる)",
            any("vaginal_opening" in o for o in src) and any("mouth" in o for o in src),
            f"{len(src)}件")
        pg.locator("#emis-r .rel").last.locator("button.del").click()
        pg.wait_for_timeout(300)
        chk("噴出物の行を消せる", pg.locator("#emis-r .rel").count() == n0)

        if pg.locator("#subjects .subj").count():
            first = pg.locator("#subjects .subj").first
            np0 = first.locator(".row").count()
            first.locator("button:has-text('＋ピアス')").first.click()
            pg.wait_for_timeout(400)
            chk("ピアスは専用の箱で足せる",
                pg.locator("#subjects .subj").first.locator(".row").count() == np0 + 1)
            oi = cb_items(pg, pg.locator("#subjects .cb-btn.empty")
                              .filter(has_text="身につけているもの").first)
            chk("身につけているものの候補からピアスが消えている",
                bool(oi) and not any(o.startswith("piercing") for o in oi), f"{len(oi)}件")
            pg.locator("#subjects .subj").first.locator("button.del").last.click()
            pg.wait_for_timeout(300)

        # ── 語彙の説明は markdown で書いてある。素の文字列で出さない ────
        # **自前で 1 行足してから**見る (前段で消しているので、landing post に
        # 依存させない。2026-07-31 に依存で timeout した)
        pg.click("button:has-text('＋噴出物の行き先')")
        pg.wait_for_timeout(400)
        pg.locator("#emis-r .rel").last.locator(".cb-btn").nth(2).click()
        pg.wait_for_timeout(400)
        raw = pg.eval_on_selector_all(".cb-pop .cb-i i", "es=>es.map(e=>e.textContent)")
        chk("説明に ** がそのまま出ていない",
            bool(raw) and not any("**" in x for x in raw), f"{len(raw)}件")
        chk("強調が要素になっている",
            pg.locator(".cb-pop .cb-i i b").count() > 0)
        chk("説明を innerHTML で入れていない (語彙が HTML を注入できない)",
            not pg.evaluate("()=>document.querySelector('.cb-pop .cb-i i')"
                            "?.innerHTML.includes('<span')"))
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)

        # ── popup が画面からはみ出ない (右パネルは欄が広いので必ず起きる) ──
        over, clipped = [], 0
        btns = pg.locator("#subjects .cb-btn")
        for i in range(min(btns.count(), 8)):
            btns.nth(i).click()
            pg.wait_for_timeout(200)
            r = pg.evaluate("""() => {const e=document.querySelector('.cb-pop');
                if(!e) return null; const b=e.getBoundingClientRect();
                const it=e.querySelector('.cb-i i');
                return {over:b.right-innerWidth, left:b.left,
                        one: it ? it.clientHeight < 18 : false}}""")
            if r:
                over.append(r["over"])
                clipped += 1 if r["one"] and r["over"] > 0 else 0
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(120)
        chk("右パネルの popup が画面からはみ出さない",
            bool(over) and max(over) <= 0, f"最大 {max(over) if over else '-'}px")
        chk("popup が画面の左外に出ない", bool(over) and min(over) > -10000)

        # ── 0064: 文で入れる (人が書いた展開の型) ──────────────────────
        n_rel0 = pg.evaluate("()=>ann.relations.length")
        pg.fill("#say", "p1 vaginal sex p2 from behind")
        pg.click("button:has-text('適用')")
        pg.wait_for_timeout(600)
        st = pg.evaluate("""() => ({n:ann.relations.length,
            ins: ann.relations.some(r=>r.relation==='inserting_into'
                 && r.object.region==='vaginal_opening' && r.subject.region==='penis'),
            beh: ann.relations.some(r=>r.relation==='behind'),
            reg: (ann.subjects.find(s=>s.ref==='person_2')||{}).regions||[]})""")
        chk("一文で関係が 2 本入る", st["n"] == n_rel0 + 2 and st["ins"] and st["beh"],
            f"{n_rel0}->{st['n']} ins={st['ins']} behind={st['beh']}")
        chk("関係で使う部位が見えている部位にも入る",
            "vaginal_opening" in st["reg"], str(st["reg"])[:80])
        chk("長い型が先に当たる (from behind が behind に食われない)", st["beh"])
        pg.fill("#say", "p1 zzz p2 nonsense")
        pg.click("button:has-text('適用')")
        pg.wait_for_timeout(400)
        out = pg.inner_text("#sayout")
        chk("解釈できなかった語を黙って捨てない",
            "zzz" in out and "nonsense" in out, out[:70])
        pg.click("button:has-text('型の一覧')")
        pg.wait_for_timeout(300)
        chk("型の一覧が出る", "vaginal sex" in pg.inner_text("#sayout"))

        # ── ?ro=1 なら下書きを一切保存しない ──────────────────────────
        chk("読むだけモードが効いている", pg.evaluate("()=>RO === true"))
        posted = []
        pg.on("request", lambda r: posted.append(r.url)
              if "/api/draft" in r.url else None)
        pg.evaluate("()=>draft()")
        pg.wait_for_timeout(500)
        chk("読むだけモードで下書きを送らない", not posted, str(posted[:1]))

        # ── 0065 + 語彙の横断検索 ─────────────────────────────────────
        for q, want in (("泡", "soap_foam"), ("dogeza", "dogeza"),
                        ("膣", "vaginal_opening"), ("排尿", "urination")):
            pg.fill("#find", q)
            pg.click("button:has-text('探す')")
            pg.wait_for_timeout(300)
            txt = pg.inner_text("#findout")
            chk(f"語を探す: {q} が引ける", want in txt,
                f"{pg.locator('#findout .f-line').count()}件")
        chk("どこに属する語かが出る", "部位" in pg.inner_text("#findout")
            or "ポーズ" in pg.inner_text("#findout"))
        # **語ごとに見る。** 文字列まるごとの一致だと打ち間違い 1 文字で 0 件になる
        for q, want in (("holdin own legs", "holding own legs"),   # g が抜けている
                        ("legs own", "own_legs"),                  # 語順が逆
                        ("抱え 腕", "holding")):                    # 日本語 + 英語
            pg.fill("#find", q)
            pg.click("button:has-text('探す')")
            pg.wait_for_timeout(300)
            chk(f"語ごとに引ける: {q}", want in pg.inner_text("#findout"),
                pg.inner_text("#findout").split(chr(10))[0][:50])
        pg.fill("#find", "zzzqqq")
        pg.click("button:has-text('探す')")
        pg.wait_for_timeout(300)
        chk("当たらないときは言い換えを促す",
            "該当なし" in pg.inner_text("#findout")
            and "短く" in pg.inner_text("#findout"))
        chk("語数は数えて出す (固定文で書かない)",
            pg.inner_text("#vcount").strip() != "全語彙"
            and "語" in pg.inner_text("#vcount"), pg.inner_text("#vcount"))
        pg.fill("#find", "soap_foam")
        pg.click("button:has-text('探す')")
        pg.wait_for_timeout(300)
        row = pg.locator("#findout .f-line").first
        chk("どの部位にも付く属性は部位を選ばせる", row.locator(".cb").count() == 1)
        row.locator(".cb-btn").first.click()
        pg.wait_for_timeout(250)
        pg.fill(".cb-search", "thigh")
        pg.wait_for_timeout(250)
        pg.locator(".cb-pop .cb-i").first.dispatch_event("mousedown")
        pg.wait_for_timeout(250)
        pg.locator("#findout button:has-text('入れる')").first.click()
        pg.wait_for_timeout(400)
        got = pg.evaluate("""()=>(ann.subjects[act].attributes||[])
            .filter(a=>a.attribute==='surface_substance'&&a.value==='soap_foam')""")
        chk("部位付きで入る (位置を言わない所見にならない)",
            bool(got) and bool(got[-1].get("region")), str(got[-1] if got else "-")[:70])

        # ── 0070: 自由入力は「書いてある文字」だけ ─────────────────────
        chk("自由入力を許す属性を DB から取っている",
            pg.evaluate("()=>JSON.stringify(V.text_attrs)") == '["surface_marking"]',
            pg.evaluate("()=>JSON.stringify(V.text_attrs)"))
        pg.fill("#find", "surface_marking")
        pg.click("button:has-text('探す')")
        pg.wait_for_timeout(350)
        # 部位を選ぶ欄がある行 = any_region の属性値。先頭が説明行のこともある
        row = pg.locator("#findout .f-line").filter(has=pg.locator(".cb")).first
        row.locator(".cb-btn").first.click()
        pg.wait_for_timeout(250)
        pg.fill(".cb-search", "buttock")
        pg.wait_for_timeout(250)
        pg.locator(".cb-pop .cb-i").first.dispatch_event("mousedown")
        pg.wait_for_timeout(250)
        pg.locator("#findout button:has-text('入れる')").first.click()
        pg.wait_for_timeout(500)
        boxes = pg.locator("#subjects input[placeholder*='書いてある文字']")
        chk("書いてある文字の欄が出る", boxes.count() >= 1, f"{boxes.count()}個")
        if boxes.count():
            boxes.first.fill("正正")
            pg.wait_for_timeout(300)
            got = pg.evaluate("""()=>(ann.subjects[act].attributes||[])
                .filter(a=>a.attribute==='surface_marking').map(a=>a.text)""")
            chk("入力が所見に載る", "正正" in got, str(got))
        others = pg.evaluate("""()=>[...document.querySelectorAll('#subjects .row,#subjects .e-line')]
            .filter(r=>r.querySelector("input[placeholder*='書いてある文字']")).length""")
        chk("自由入力の欄が他の属性に生えていない",
            others <= pg.evaluate("""()=>(ann.subjects[act].attributes||[])
                .filter(a=>a.attribute==='surface_marking').length"""), str(others))

        # ── 0072: 型は検索の単位でもある ──────────────────────────────
        pg.fill("#find", "羽交い締め")
        pg.click("button:has-text('探す')")
        pg.wait_for_timeout(400)
        chk("日本語の別名で型を引ける", "full nelson" in pg.inner_text("#findout"),
            pg.inner_text("#findout")[:60])
        chk("従来タグとの対応が出る", "full_nelson" in pg.inner_text("#findout"))
        pg.fill("#find", "vaginal sex")
        pg.click("button:has-text('探す')")
        pg.wait_for_timeout(400)
        pg.locator("#findout button:has-text('この形の画像を探す')").first.click()
        pg.wait_for_timeout(9000)
        out = pg.inner_text("#findout")
        chk("型で分解された所見を 1 語で引ける",
            "件" in out and "351399" in out, out[out.find("件") - 8:][:80]
            if "件" in out else out[:80])

        # ── 型の主体は「選択中の人物」(常に subjects[0] だと関係が逆になる) ──
        if pg.locator("#subjects .subj").count() >= 2:
            seen = []
            for want in (0, 1):
                pg.evaluate(f"()=>{{act={want}; render()}}")
                pg.wait_for_timeout(250)
                # **前段で入った full_nelson を先に消す。** 残っていると型の重複排除で
                # 何も足されず、検査が空振りしたまま通ってしまう (2026-07-31 実測)
                pg.evaluate("()=>{ann.relations=ann.relations"
                            ".filter(r=>r.relation!=='full_nelson'); render()}")
                pg.wait_for_timeout(200)
                n0 = pg.evaluate("()=>ann.relations.length")
                pg.fill("#find", "羽交い締め")
                pg.click("button:has-text('探す')")
                pg.wait_for_timeout(350)
                row = pg.locator("#findout .f-line").filter(
                    has=pg.locator("button:has-text('この形の画像を探す')")).first
                row.locator("button:has-text('入れる')").click()
                pg.wait_for_timeout(700)
                got = pg.evaluate(f"""()=>ann.relations.slice({n0})
                    .filter(r=>r.relation==='full_nelson')
                    .map(r=>r.subject.ref+'>'+r.object.ref)[0]||''""")
                seen.append((pg.evaluate("()=>ann.subjects[act].ref"), got))
            chk("型の主体は選択中の人物 (関係が逆にならない)",
                len(seen) == 2 and all(g and g.startswith(ref + ">")
                                       for ref, g in seen), str(seen))
            chk("選ぶ人物を変えると向きが変わる",
                len({g for _, g in seen}) == 2, str(seen))

        # ── 文に ref を書かなくても選択中の人物が主体になる ───────────
        if pg.locator("#subjects .subj").count() >= 2:
            seen2 = []
            for want in (0, 1):
                pg.evaluate(f"""()=>{{act={want};
                    ann.relations=ann.relations.filter(r=>r.relation!=='full_nelson');
                    render()}}""")
                pg.wait_for_timeout(250)
                n0 = pg.evaluate("()=>ann.relations.length")
                pg.fill("#say", "羽交い締め")
                pg.click("button:has-text('適用')")
                pg.wait_for_timeout(600)
                seen2.append((
                    pg.evaluate("()=>ann.subjects[act].ref"),
                    pg.evaluate(f"""()=>ann.relations.slice({n0})
                        .filter(r=>r.relation==='full_nelson')
                        .map(r=>r.subject.ref+'>'+r.object.ref)[0]||''""")))
            chk("ref を書かなくても選択中の人物が主体になる",
                len(seen2) == 2 and all(g and g.startswith(r + ">") for r, g in seen2),
                str(seen2))
            chk("$s / $o という名前の人物を作らない",
                not pg.evaluate("()=>ann.subjects.some(s=>String(s.ref).startsWith('$'))"),
                pg.evaluate("()=>JSON.stringify(ann.subjects.map(s=>s.ref))"))

        # ── 主体は「文で入れる」の隣で選べる (見えない全体選択に頼らない) ──
        chk("主体の欄が文の隣にある", pg.locator("#saywho .cb-btn").count() == 1)
        if pg.locator("#subjects .subj").count() >= 2:
            seen3 = []
            for idx in (0, 1):
                pg.evaluate("()=>{ann.relations=ann.relations"
                            ".filter(r=>r.relation!=='full_nelson'); render()}")
                pg.wait_for_timeout(200)
                pg.locator("#saywho .cb-btn").click()
                pg.wait_for_timeout(300)
                pg.locator(".cb-pop .cb-i").nth(idx).dispatch_event("mousedown")
                pg.wait_for_timeout(400)
                n0 = pg.evaluate("()=>ann.relations.length")
                pg.fill("#say", "羽交い締め")
                pg.click("button:has-text('適用')")
                pg.wait_for_timeout(600)
                seen3.append((
                    pg.inner_text("#saywho").strip(),
                    pg.evaluate(f"""()=>ann.relations.slice({n0})
                        .filter(r=>r.relation==='full_nelson')
                        .map(r=>r.subject.ref+'>'+r.object.ref)[0]||''""")))
            chk("主体の欄で選んだ人物が行為の主体になる",
                len(seen3) == 2 and all(g and g.startswith(w + ">") for w, g in seen3),
                str(seen3))
            chk("主体の欄はカードの選択とも連動する",
                pg.evaluate("()=>ann.subjects[act].ref") == seen3[-1][0],
                f"{pg.evaluate('()=>ann.subjects[act].ref')} vs {seen3[-1][0]}")

        # ── 確定して次へ (DB には触らず、client の分岐だけ見る) ─────────
        # **/api/commit は本物を呼ばない** — 検査が観測を書いてはいけない
        pg.route("**/api/commit", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body='{"errors":[],"written":{"observation":3,"asset_region":0,'
                 '"relation":0,"unresolved":0,"skipped":0}}'))
        wl0 = pg.evaluate("()=>WL.length")
        cur0 = pg.evaluate("()=>cur")
        img0 = pg.get_attribute("#im", "src")
        pg.click("#gobtn")
        pg.wait_for_timeout(2500)
        chk("確定して次へで別の post に移る",
            pg.evaluate("()=>cur") != cur0, f"{cur0} -> {pg.evaluate('()=>cur')}")
        chk("画像が切り替わる", pg.get_attribute("#im", "src") != img0)
        chk("確定しても一覧の pool が外れない",
            pg.evaluate("()=>WL.length") == wl0,
            f"{wl0} -> {pg.evaluate('()=>WL.length')}")
        pg.unroute("**/api/commit")

        # 失敗したら進まない (既に観測がある scene でも)
        pg.route("**/api/commit", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body='{"errors":["わざと失敗させた"]}'))
        cur1 = pg.evaluate("()=>cur")
        pg.click("#gobtn")
        pg.wait_for_timeout(1500)
        chk("確定に失敗したら進まない", pg.evaluate("()=>cur") == cur1)
        # **失敗は目立たなければ「押しても何も起きない」と同じ。**
        # 2026-07-31: 非推奨になった species が選択肢に残っていたせいで確定が毎回
        # 落ちていたが、エラーがメッセージ欄にしか出ず気付けなかった
        chk("確定の失敗が目立つ場所に出る",
            not pg.locator("#cerr").is_hidden()
            and "確定できません" in pg.inner_text("#cerr"),
            pg.inner_text("#cerr")[:60])
        # **6 秒で元に戻る**ので、待ってから見ると通らない。押した直後に見る
        chk("失敗したらボタン自身が言う",
            "確定できません" in pg.inner_text("#gobtn")
            or "確定できません" in pg.inner_text("#cerr"), pg.inner_text("#gobtn"))
        pg.unroute("**/api/commit")

        # 非推奨の語は選ばせない (選べると確定が毎回落ちる)
        chk("非推奨の species を候補に出さない",
            not pg.evaluate("()=>V.species.some(s=>/非推奨/.test(s.n||''))"),
            str(pg.evaluate("()=>V.species.filter(s=>/非推奨/.test(s.n||'')).map(s=>s.slug)")))

        # 古い版で操作していないかを画面が自分で見る
        b_srv = pg.evaluate("()=>fetch('/api/build').then(r=>r.json())")
        chk("サーバの指紋を画面が持っている",
            pg.evaluate("()=>BUILD_AT_LOAD") == b_srv["build"],
            f"{pg.evaluate('()=>BUILD_AT_LOAD')} vs {b_srv['build']}")
        chk("指紋が一致していれば警告を出さない",
            pg.locator("#stale").is_hidden())

        # ── ポーズの箱から「関係で書く形」にも辿れる ───────────────────
        # 抱える・羽交い締めは設計上 pose ではなく関係だが、注釈者はポーズを探す。
        # ここに出さないと「どこにも無い」と読まれる (user 指摘 2026-07-31)
        btn = pg.locator("#subjects .cb-btn.empty").filter(has_text="ポーズ").first
        btn.click()
        pg.wait_for_timeout(350)
        pg.fill(".cb-search", "抱え")
        pg.wait_for_timeout(400)
        items = pg.eval_on_selector_all(".cb-pop .cb-list > *",
                                        "es=>es.map(e=>e.textContent)")
        chk("ポーズの箱に「関係で書く形」が出る",
            any("関係で書く形" in x for x in items), f"{len(items)}件")
        idx = [i for i, x in enumerate(items) if "hugging own legs" in x]
        chk("抱える形がポーズの箱から見つかる", bool(idx))
        if idx:
            # **0074 で判断が変わった。** 両手が揃った確定的な形は pose にしたので、
            # 抱える形は関係ではなく pose として入る (user 判断 2026-07-31)
            np0 = pg.evaluate("()=>(ann.subjects[act].poses||[]).length")
            pg.locator(".cb-pop .cb-i").nth(idx[0] - 1).dispatch_event("mousedown")
            pg.wait_for_timeout(800)
            chk("抱える形は pose として入る (両手が揃った確定的な形)",
                pg.evaluate("""()=>(ann.subjects[act].poses||[])
                    .some(p=>p.pose==='hugging_own_legs')""")
                and pg.evaluate("()=>(ann.subjects[act].poses||[]).length") == np0 + 1)
        else:
            pg.keyboard.press("Escape")

        # ── arm フィルタ + producer 色分けパネル (0079 の続き, round4 二腕化) ──
        pg.goto(ro(a.url.rstrip("/")+"?pool=7989&arm=H"), wait_until="networkidle")
        pg.wait_for_timeout(1500)
        if pg.locator("#nav .th").count():
            chk("arm=H で arm H の scene だけ出る",
                pg.locator("#nav .th").count() == pg.locator("#nav .arm-H").count()
                and pg.locator("#nav .arm-M").count() == 0,
                f"th={pg.locator('#nav .th').count()} H={pg.locator('#nav .arm-H').count()}")
            chk("開いた scene が arm H",
                pg.evaluate("()=>{const c=cur;return (WL.find(w=>w.post_id==c)||{}).arm}") == "H")
            chk("フィルタのリンクが出る", "arm H" in pg.inner_text("#prog"))
            chk("確定済み所見のパネルがある", pg.locator("#obsreview").count() == 1)
            # producer 別に色分けする土台 (行のクラス) があること
            chk("モデル/人間の色分けクラスが定義されている",
                pg.evaluate("""()=>{const s=[...document.styleSheets].flatMap(x=>{
                    try{return [...x.cssRules]}catch(e){return []}}).map(r=>r.selectorText||'');
                    return s.some(x=>x.includes('obs-model'))&&s.some(x=>x.includes('obs-human'))}"""))

        # ── 突合チップは**各コントロールの真横**に出る (2026-07-31 統合。
        #    旧: conflictBar/オーバーレイ行が右パネルの離れた場所にあった) ──
        # fixture は固定 post にしない — 注釈が進むと食い違いチップが尽きて
        # 偽 FAIL する (2026-08-01 実害、friction/ui-smoke-fixture-coupled)。
        # model 所見のある候補から「bad チップが残っている scene」を探して使う。
        found = False
        for cand in (648990, 555457, 555451, 959941, 989333, 729269, 195522):
            pg.goto(ro(a.url.rstrip("/") + f"?post={cand}"), wait_until="networkidle")
            pg.wait_for_timeout(2000)
            if (pg.locator("#subjects .subj").count()
                    and pg.locator("#subjects .mchip.bad").count()):
                found = True
                break
        if not found:
            print("  注記: 食い違いチップの残る scene が無い — 突合チップ検査は skip")
        if found:
            chk("種族/性別コントロールの脇に突合チップが出る",
                pg.locator("#subjects .mchip[data-axis=species],"
                           " #subjects .mchip[data-axis=presentation]").count() >= 1)
            chk("食い違いが強調される (bad クラス)",
                pg.locator("#subjects .mchip.bad").count() >= 1)
            chk("チップが一致/食い違いで色分け",
                pg.locator("#subjects .mchip.ok").count()
                + pg.locator("#subjects .mchip.bad").count() >= 1)
            chk("食い違いチップに ✓採用/✗不採用 が直接付く",
                pg.locator("#subjects .mchip.bad .mic.ok").count() >= 1
                and pg.locator("#subjects .mchip.bad .mic.no").count() >= 1)
            # ── コンパクト化 (2026-08-01): 下書き済みの値はチップ再掲せず一致印 ──
            chk("下書き済みの部位は人チップとして再掲しない",
                pg.evaluate("""()=>[...document.querySelectorAll('#subjects .subj')]
                  .every((card,i)=>{
                    const regs=new Set((ann.subjects[i]||{}).regions||[]);
                    return [...card.querySelectorAll('.mchip.hu[data-axis=region]')]
                      .every(c=>!regs.has(c.textContent.replace(/^人 /,'').replace(/ ?✓$/,'')))})"""))
            chk("下書きのタグ・行に人/モの一致印が付く",
                pg.locator("#subjects .hm-m").count() >= 1)
            chk("チップの tooltip に語の説明が入る",
                pg.evaluate("""()=>[...document.querySelectorAll('.mchip,.hm-m')]
                  .some(c=>(c.title||'').includes('\\n'))"""))
            chk("tooltip にマークダウン記号 (**/`) が残っていない",
                pg.evaluate("""()=>[...document.querySelectorAll('.mchip,.hm-m,.ck')]
                  .every(c=>!/\\*\\*|`/.test(c.title||''))"""))
            chk("tooltip の説明が簡潔 (1 行 65 字以内)",
                pg.evaluate("""()=>[...document.querySelectorAll('.mchip,.hm-m,.ck')]
                  .every(c=>(c.title||'').split('\\n').every(l=>l.length<=65))"""))
            # ✓ を押しても 5 秒はまだ書かない — 元に戻すトーストが出て取り消せる
            nmic = pg.locator("#subjects .mchip.bad .mic.ok").count()
            if nmic:
                pg.locator("#subjects .mchip.bad .mic.ok").first.click()
                pg.wait_for_timeout(300)
                chk("✓ は猶予トーストが出てすぐには書かれない",
                    pg.locator(".undo-toast").count() == 1)
                pg.locator(".undo-toast button").click()
                pg.wait_for_timeout(300)
                chk("元に戻すで取り消せる (トースト消滅)",
                    pg.locator(".undo-toast").count() == 0
                    and pg.evaluate("()=>PENDING===null"))
            # 橙の部位チップは本体クリックで下書きに採れる。**左端を突く** —
            # 中央クリックは ✓✗ アイコンに当たり得て、ro 検査なのに DB へ
            # 採用が書かれてしまう
            card0 = pg.locator("#subjects .subj").first
            nb = card0.locator(".mchip.bad[data-axis=region]").count()
            if nb:
                before = pg.evaluate("()=>(ann.subjects[0].regions||[]).length")
                card0.locator(".mchip.bad[data-axis=region]").first.click(
                    position={"x": 4, "y": 4})
                pg.wait_for_timeout(500)
                chk("モデルのみの部位をクリックで下書きに採れる",
                    pg.evaluate("()=>(ann.subjects[0].regions||[]).length") == before + 1)

        # ── pool オブザベーションビュー (2026-08-01: 画像確認と印付けを同一画面に) ──
        # 検証プールが存在するときだけ検査 (fixture-coupled にしない)
        n_err0 = len(errs)
        pg.goto(ro(a.url.rstrip("/") + "/pool/8010"), wait_until="networkidle")
        pg.wait_for_timeout(2000)
        ncards = pg.locator("#grid .card").count()
        if ncards:
            chk("pool view: カードと値ボタンが出る",
                ncards >= 1 and pg.locator(".vbtn").count() >= 1)
            chk("pool view: JS 例外が無い", len(errs) == n_err0)
            # dry 書き込みが 200 で通る (実書き込みはしない)
            ok = pg.evaluate("""async()=>{
              const c=document.querySelector('.vbtn'); if(!c) return false;
              const r=await fetch('/api/quick_attr',{method:'POST',body:JSON.stringify(
                {post:206141,ref:'person_1',attribute:'flush_intensity',
                 value:'light',region:'face',dry:true})});
              return (await r.json()).ok===true}""")
            chk("pool view: quick_attr dry が通る", ok)
        else:
            print("  注記: pool 8010 が空/不在 — pool view 検査は skip")

        # ── anchor モード (実クリックは絶対にしない — 座標の実書き込みになる) ──
        n_err0 = len(errs)
        pg.goto(ro(a.url.rstrip("/") + "/pool/8011?mode=anchor"), wait_until="networkidle")
        pg.wait_for_timeout(2000)
        if pg.locator("#grid .card").count():
            chk("anchor view: カードと人物ボタンが出る",
                pg.locator(".vbtn").count() >= 1)
            chk("anchor view: JS 例外が無い", len(errs) == n_err0)
            ok = pg.evaluate("""async()=>{
              const r=await fetch('/api/quick_anchor',{method:'POST',body:JSON.stringify(
                {post:729259,ref:'person_1',x:0.5,y:0.2,dry:true})});
              return (await r.json()).ok===true}""")
            chk("anchor view: quick_anchor dry が通る", ok)
        else:
            print("  注記: pool 8011 が空/不在 — anchor view 検査は skip")

        if a.shot:
            pg.screenshot(path=a.shot)
        pg.close()

        # **横向き前提** (user 判断 2026-07-30)。縦は「覆わない」条件を満たせないので
        # 検査しない — 満たせない前提を検査に入れると常に赤くなって番犬にならない。
        mobile_checks(b, 932, 430, "ProMax横")
        mobile_checks(b, 1024, 768, "iPad横")
        b.close()

    bad = [c for c in CHECKS if not c[1]]
    for name, ok, detail in CHECKS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    print(f"\nPASS {len(CHECKS)-len(bad)} / FAIL {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
