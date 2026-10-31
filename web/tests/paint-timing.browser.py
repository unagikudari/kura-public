"""paintTiming counts what the person can see, waits for decode, and ends on an empty view.

Self-contained: the module is transpiled with the web's own TypeScript and run in Chromium
against hand-built pages (no server, no kura data). Run: python web/tests/paint-timing.browser.py
"""
import json
import os
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

WEB = Path(__file__).resolve().parents[1]
JS = subprocess.run(
    ["node", "-e", """
const ts=require('typescript'),fs=require('fs');
const src=fs.readFileSync(process.argv[1],'utf8');
process.stdout.write(ts.transpileModule(src,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText);
""", str(WEB / "src/lib/paintTiming.ts")], cwd=WEB, check=True, capture_output=True, text=True).stdout

PAGE = """<!doctype html><html><body style="margin:0">
<div id="scroller" style="height:200px;overflow:auto;position:relative">
  <button class="thumb" style="display:block;width:160px;height:160px;padding:0"><img id="seen" width="150" height="150"></button>
  <div style="height:120px"></div>
  <button class="thumb" style="display:block;width:160px;height:160px;padding:0"><img id="clipped" width="150" height="150"></button>
</div>
<button class="thumb" style="display:block;width:160px;height:160px;padding:0"><img id="covered" width="150" height="150"></button>
<div id="overlay" style="position:absolute;left:0;top:200px;width:300px;height:200px;background:#000"></div>
<div id="slots"></div>
</body></html>"""


def png(page):
    return page.evaluate("()=>{const c=document.createElement('canvas');c.width=c.height=8;c.getContext('2d').fillRect(0,0,8,8);return c.toDataURL()}")


def main():
    with sync_playwright() as p:
        # PW_CHANNEL=chrome uses the installed Chrome where Playwright's own build is absent.
        browser = p.chromium.launch(channel=os.environ.get("PW_CHANNEL") or None)
        page = browser.new_page(viewport={"width": 800, "height": 600})
        page.set_content(PAGE)
        page.evaluate("()=>{window.__sent=[];navigator.sendBeacon=(u,b)=>{b.text().then(t=>window.__sent.push(JSON.parse(t)));return true}}")
        page.add_script_tag(content=JS.replace("export function", "function") + "\nwindow.PT={viewState,measure};", type="module")
        page.wait_for_function("()=>window.PT")
        src = png(page)
        page.evaluate("s=>{for(const i of document.images)i.src=s}", src)
        page.wait_for_function("()=>[...document.images].every(i=>i.complete)")

        # Only #seen is visible: #clipped is inside the window but clipped by the scroller,
        # #covered is under the overlay. Before decode() resolves it is not yet drawn.
        first = page.evaluate("()=>PT.viewState()")
        assert first["visible"] == 1, first
        page.wait_for_function("()=>PT.viewState().unfilled===0", timeout=3000)

        # A decode that fails is not drawn; it is counted as failed, and a measurement whose
        # only remaining images failed ends instead of waiting for the timeout.
        page.evaluate("""s=>{const i=document.getElementById('seen');HTMLImageElement.prototype.decode=function(){return this===i?Promise.reject(new Error('x')):Promise.resolve()};
          i.src=s+'#again'}""", src)
        page.wait_for_function("()=>{const v=PT.viewState();return v.failed===1}", timeout=3000)
        state = page.evaluate("()=>PT.viewState()")
        assert state == {"visible": 1, "unfilled": 1, "failed": 1}, state
        page.evaluate("()=>PT.measure('scroll', performance.now()-500)")
        page.wait_for_function("()=>window.__sent.length===1", timeout=2000)
        sent = page.evaluate("()=>window.__sent.pop()")
        assert sent["failed"] == 1 and not sent.get("timed_out"), sent

        # A placeholder the person can see is an undrawn slot.
        page.evaluate("()=>{document.getElementById('overlay').remove();document.getElementById('slots').innerHTML='<div class=\"missing-slot\" style=\"width:100px;height:100px\"></div>'}")
        page.wait_for_timeout(100)
        state = page.evaluate("()=>PT.viewState()")
        assert state["unfilled"] >= 1, state

        # A view with nothing to draw ends at once instead of polling until the timeout.
        page.set_content("<!doctype html><p>settings</p>")
        page.evaluate("()=>{window.__sent=[];navigator.sendBeacon=(u,b)=>{b.text().then(t=>window.__sent.push(JSON.parse(t)));return true}}")
        page.add_script_tag(content=JS.replace("export function", "function") + "\nwindow.PT={viewState,measure};", type="module")
        page.wait_for_function("()=>window.PT")
        page.evaluate("()=>PT.measure('back', performance.now())")
        page.wait_for_function("()=>window.__sent.length===1", timeout=2000)
        sent = page.evaluate("()=>window.__sent[0]")
        assert sent["kind"] == "back" and sent["visible"] == 0 and sent["unfilled"] == 0 and not sent.get("timed_out"), sent
        assert sent["filled_ms"] < 1000, sent
        browser.close()
    print(json.dumps({"visible_only": True, "drawn": True, "decode_failure_counted": True, "placeholder_counted": True, "empty_view_ends": True}))


if __name__ == "__main__":
    main()
