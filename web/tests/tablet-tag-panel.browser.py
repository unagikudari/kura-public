"""Single-view tag panel default by device: isolated build + synthetic API, no writes.
A tablet opens with its tag panel (see-through background); a phone opens with the image alone, also when
it is held in landscape, and rotating keeps either one's state — including a person's toggle.
Run after npm run build: uv run --with playwright python web/tests/tablet-tag-panel.browser.py
Override CHROMIUM only to use an existing local browser executable.
"""
import base64, hashlib, hmac, json, os, socket, subprocess, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
SECRET='tablet-tag-panel-synthetic-test-only'
SVG='<svg xmlns="http://www.w3.org/2000/svg" width="800" height="1000"><rect width="800" height="1000" fill="#456"/></svg>'
def tag(name): return dict(names=[name],category='general',usages=1,pinned=None)
def post(i):
 return dict(id=i,version=1,creationTime=None,lastEditTime=None,safety='safe',source=None,originalFilename=f'fixture-{i}',type='image',mimeType='image/svg+xml',fileSize=100,canvasWidth=800,canvasHeight=1000,contentUrl=f'/fixture/{i}.svg',thumbnailUrl=f'/fixture/{i}.svg',flags=[],tags=[tag('alpha'),tag('beta')],user=None,score=0,ownFavorite=False,tagCount=2,favoriteCount=0,commentCount=0,noteCount=0,pools=[])
POSTS={i:post(i) for i in (1000,1001)};WRITES=[]
def response(url):
 p=urlparse(url).path
 if p=='/api/posts':return dict(results=list(POSTS.values()),total=2,offset=0,limit=40,query='')
 if p=='/api/tag-categories':return [dict(name='general',color='#aaccdd',default=True)]
 if p=='/api/tag-relations':return []
 if p.endswith('/index'):return {'idx':int(p.split('/')[3])-1000}
 if p.startswith('/api/post/') and len(p.split('/'))==4:return POSTS[int(p.split('/')[3])]
 if p=='/api/pools':return dict(results=[],total=0,offset=0,limit=40,query='')
 return {}
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  body=json.dumps(response(self.path)).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
api=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=api.serve_forever,daemon=True).start()
with socket.socket() as port_probe:
 port_probe.bind(('127.0.0.1',0));port=port_probe.getsockname()[1]
base=f'http://127.0.0.1:{port}'
env={**os.environ,'HOST':'127.0.0.1','PORT':str(port),'ORIGIN':base,'INTERNAL_API_URL':f'http://127.0.0.1:{api.server_port}','PUBLIC_API_URL':'','KURA_SESSION_SECRET':SECRET}
errors=[]
with tempfile.TemporaryFile() as log:
 proc=subprocess.Popen(['node','tests/serve-fixture.mjs'],cwd=ROOT,env=env,stdout=log,stderr=log)
 try:
  deadline=time.monotonic()+15
  while True:
   if proc.poll() is not None:raise RuntimeError('fixture server exited')
   try:urllib.request.urlopen(base+'/login',timeout=.3);break
   except Exception:
    if time.monotonic()>deadline:raise RuntimeError('readiness timeout')
    time.sleep(.1)
  enc=lambda b:base64.urlsafe_b64encode(b).decode().rstrip('=')
  payload=enc(json.dumps(dict(u='fixture',t='fixture-token',e='fixture@example.invalid',exp=int(time.time())+3600)).encode())
  cookie=payload+'.'+enc(hmac.new(SECRET.encode(),payload.encode(),hashlib.sha256).digest())
  with sync_playwright() as pw:
   browser=pw.chromium.launch(headless=True, **({'executable_path':os.environ['CHROMIUM']} if os.environ.get('CHROMIUM') else {}))
   def route(req):
    u=urlparse(req.request.url)
    assert u.hostname=='127.0.0.1', 'external request blocked'
    if u.path.startswith('/fixture/'):return req.fulfill(body=SVG,content_type='image/svg+xml')
    if u.path.startswith('/api/'):return req.fulfill(json=response(req.request.url))
    if u.path.startswith('/m/'):
     if req.request.method!='GET' and u.path.startswith('/m/post/'):WRITES.append(u.path)
     return req.fulfill(json={})
    req.continue_()
   def open_viewer(screen,viewport,touch=True):
    ctx=browser.new_context(screen=screen,viewport=viewport,has_touch=touch,is_mobile=touch,locale='ja-JP')
    ctx.add_cookies([dict(name='kura_session',value=cookie,url=base),dict(name='kura_cachefix1',value='1',url=base)])
    ctx.route('**/*',route)
    page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(base+'/post/1000?idx=0',wait_until='networkidle');page.wait_for_timeout(300)
    return ctx,page
   def panel(page):
    return page.evaluate("(() => { const el = document.querySelector('.lb-tag-sidebar'); return el && getComputedStyle(el).display !== 'none' ? getComputedStyle(el).backgroundColor : null })()")
   def rotate(page,w,h):
    page.set_viewport_size({'width':w,'height':h});page.wait_for_timeout(300)
   PHONE={'width':390,'height':844};TABLET={'width':820,'height':1180}

   # A phone: the image alone, in either orientation and whichever it was opened in.
   ctx,page=open_viewer(PHONE,{'width':390,'height':844})
   assert panel(page) is None,'phone portrait opened the panel'
   rotate(page,844,390);assert panel(page) is None,'phone rotated to landscape opened the panel'
   ctx.close()
   ctx,page=open_viewer(PHONE,{'width':844,'height':390})
   assert panel(page) is None,'phone opened in landscape was taken for a tablet'
   rotate(page,390,844);assert panel(page) is None,'phone rotated back opened the panel'
   ctx.close()

   # A tablet: the panel, translucent, in both orientations; a toggle survives rotation.
   ctx,page=open_viewer(TABLET,{'width':820,'height':1180})
   assert panel(page)=='rgba(18, 20, 25, 0.3)',f'tablet panel: {panel(page)}'
   rotate(page,1180,820);assert panel(page)=='rgba(18, 20, 25, 0.3)','tablet rotation closed the panel'
   page.keyboard.press('t');page.wait_for_timeout(200)
   assert panel(page) is None,'t did not close the panel'
   rotate(page,820,1180);assert panel(page) is None,'rotation undid the person\'s choice'
   # The header's tag button switches the panel on screen; it opens no second list.
   tags=page.get_by_role('button',name='タグ',exact=True)
   assert tags.count()==1,f'{tags.count()} tag buttons'
   tags.click();page.wait_for_timeout(200)
   assert panel(page)=='rgba(18, 20, 25, 0.3)','the tag button did not show the panel'
   assert page.locator('.viewer-tag-panel').count()==0,'the tag button opened a second list'
   tags.click();page.wait_for_timeout(200)
   assert panel(page) is None,'the tag button did not hide the panel'
   ctx.close()

   # Manga: the overlay is on by default, and the header's tag button switches it.
   ctx,page=open_viewer(TABLET,{'width':820,'height':1180})
   page.goto(base+'/post/1000?idx=0&mode=manga',wait_until='networkidle');page.wait_for_timeout(300)
   assert page.locator('.vto.bottom').count()==1,'manga overlay is not on by default'
   page.get_by_role('button',name='タグ',exact=True).click();page.wait_for_timeout(200)
   assert page.locator('.vto.bottom').count()==0,'the tag button did not hide the manga overlay'
   assert page.locator('.manga-identities .identity-summary').count()==0,'tags still show with the overlay off'
   assert page.locator('.viewer-tag-panel').count()==0,'the tag button opened a second list'
   page.get_by_role('button',name='タグ',exact=True).click();page.wait_for_timeout(200)
   assert page.locator('.vto.bottom').count()==1,'the tag button did not show the manga overlay'
   ctx.close()

   # A narrow desktop window keeps the image-first view it had.
   ctx,page=open_viewer({'width':1920,'height':1080},{'width':500,'height':800},touch=False)
   assert panel(page) is None,'narrow desktop window opened the panel'
   ctx.close()

   assert not WRITES,WRITES
   assert not errors,errors
   browser.close()
  print('ok: tablet-tag-panel')
 finally:
  proc.terminate();proc.wait(timeout=10);api.shutdown()
