"""Single-view editor regression: isolated build + synthetic API, no live writes.
Prepare Chromium: uv run --with playwright playwright install chromium
Override CHROMIUM only to use an existing local browser executable.
Run after npm run build: uv run --with playwright python web/tests/single-tag-sidebar.browser.py
"""
import base64, hashlib, hmac, json, os, re, socket, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[1]
SECRET='single-sidebar-synthetic-test-only'
SVG='<svg xmlns="http://www.w3.org/2000/svg" width="800" height="1000"><rect width="800" height="1000" fill="#456"/></svg>'
def tag(name): return dict(names=[name],category='general',usages=1,pinned=None)
def post(i):
 return dict(id=i,version=1,creationTime=None,lastEditTime=None,safety='safe',source=None,originalFilename=f'fixture-{i}',type='image',mimeType='image/svg+xml',fileSize=100,canvasWidth=800,canvasHeight=1000,contentUrl=f'/fixture/{i}.svg',thumbnailUrl=f'/fixture/{i}.svg',flags=[],tags=[tag('alpha'),tag('beta'),dict(tag('fixture_character'),category='character')]+[tag(f'long_fixture_tag_{j}') for j in range(60)],user=None,score=0,ownFavorite=False,tagCount=62,favoriteCount=0,commentCount=0,noteCount=0,pools=[])
POSTS={i:post(i) for i in (1000,1001)};WRITES=[]
def response(url):
 u=urlparse(url);p=u.path;q=parse_qs(u.query)
 if p=='/api/posts':return dict(results=list(POSTS.values()),total=2,offset=0,limit=40,query='')
 if p=='/api/tag-categories':return [dict(name='general',color='#aaccdd',default=True)]
 if p=='/api/tag-relations':return []
 if p.endswith('/index'):return {'idx':int(p.split('/')[3])-1000}
 if p.startswith('/api/post/') and len(p.split('/'))==4:return POSTS[int(p.split('/')[3])]
 if p=='/api/tags':return [dict(names=['canonical_name','alias_name'],category='general',usages=1)]
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
  import urllib.request
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
   ctx=browser.new_context(viewport={'width':1280,'height':900})
   ctx.add_cookies([dict(name='kura_session',value=cookie,url=base),dict(name='kura_cachefix1',value='1',url=base)])
   delayed_pin=[];delayed_save=[]
   hold_pin={'enabled':False}
   def route(req):
    u=urlparse(req.request.url)
    assert u.hostname=='127.0.0.1', 'external request blocked'
    if u.path.startswith('/fixture/'):return req.fulfill(body=SVG,content_type='image/svg+xml')
    if u.path.startswith('/api/'):return req.fulfill(json=response(req.request.url))
    if u.path.startswith('/m/'):
     body=req.request.post_data_json;WRITES.append((u.path,body));pid=int(u.path.split('/')[3]);p=POSTS[pid]
     if u.path.endswith('/tag-pin'):
      if hold_pin['enabled']:
       delayed_pin.append((req,pid,body));return
      for t in p['tags']:
       if t['names'][0]==body['tag']:t['pinned']=body['state']
      return req.fulfill(json=dict(post_id=pid,tag=body['tag'],pinned=body['state']))
     assert req.request.method=='PATCH',req.request.method
     assert body['version']==p['version']
     old={t['names'][0]:t for t in p['tags']};p['tags']=[old.get(n,tag(n)) for n in body['tags']];p['version']+=1
     delayed_save.append((req,p));return
    assert req.request.method=='GET','unexpected write'
    req.continue_()
   ctx.route('**/*',route)
   page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
   page.goto(base+'/post/1000?idx=0',wait_until='networkidle')
   panel=page.get_by_role('complementary',name='タグ編集')
   expect(panel).to_have_count(1);expect(page.locator('.vto')).to_have_count(0);expect(page.locator('.lb-tagbar')).to_have_count(0)
   expect(panel.get_by_role('button',name='保存',exact=True)).to_be_disabled()
   def chip(name):return panel.locator('.tag-chip').filter(has=page.locator('.tc-name',has_text=name))
   chip('alpha').get_by_role('button',name='remove',exact=True).click();expect(chip('alpha')).to_have_count(0)
   panel.get_by_role('button',name='変更を戻す').click();expect(chip('alpha')).to_have_count(1)
   chip('alpha').get_by_role('button',name='remove',exact=True).click()
   entry=panel.get_by_placeholder('+ tag');entry.fill('alias_name');expect(panel.locator('.tsi-sug')).to_have_count(1);entry.press('ArrowDown');entry.press('Enter')
   expect(chip('canonical_name')).to_have_count(1)
   expect(chip('canonical_name').get_by_role('button',name='lock',exact=True)).to_have_count(0)
   saving_dialogs=[]
   def during_save(dialog):saving_dialogs.append(dialog.type);dialog.dismiss()
   page.on('dialog',during_save)
   panel.get_by_role('button',name='保存',exact=True).click()
   expect(panel.get_by_role('button',name='保存中…',exact=True)).to_be_disabled()
   entry.blur();page.keyboard.press('ArrowRight');expect(page.locator('.lb-id')).to_have_text('#1000')
   page.locator('.lb-close').click();expect(panel).to_have_count(1)
   page.locator('.lb-top').get_by_role('link',name='マンガ',exact=True).click()
   assert 'mode=manga' not in page.url
   assert not saving_dialogs, 'A submitted save must never be offered as discardable'
   assert len(delayed_save)==1
   req,saved_post=delayed_save.pop();req.fulfill(json=saved_post)
   expect(panel.get_by_role('status')).to_contain_text('saved')
   page.remove_listener('dialog',during_save)
   # A pending pin must prevent save and deletion of that same tag.
   chip('long_fixture_tag_0').get_by_role('button',name='remove',exact=True).click()
   expect(panel.get_by_role('button',name='保存',exact=True)).to_be_enabled()
   hold_pin['enabled']=True
   chip('beta').get_by_role('button',name='lock',exact=True).click()
   expect(panel.get_by_role('button',name='保存',exact=True)).to_be_disabled()
   chip('beta').get_by_role('button',name='remove',exact=True).click();expect(chip('beta')).to_have_count(1)
   assert len(delayed_pin)==1
   req,pid,body=delayed_pin.pop()
   for t in POSTS[pid]['tags']:
    if t['names'][0]==body['tag']:t['pinned']=body['state']
   hold_pin['enabled']=False
   req.fulfill(json=dict(post_id=pid,tag=body['tag'],pinned=body['state']))
   expect(panel.get_by_role('button',name='保存',exact=True)).to_be_enabled()
   panel.get_by_role('button',name='変更を戻す').click()
   expect(chip('beta').locator('.tc-lock')).to_have_class(re.compile(r'\bon\b'));expect(chip('beta').get_by_role('button',name='remove',exact=True)).to_have_count(0)
   entry.blur();page.keyboard.press('ArrowRight');page.wait_for_url('**/post/1001?**');page.wait_for_load_state('networkidle');expect(page.locator('.lb-id')).to_have_text('#1001');page.keyboard.press('ArrowLeft');page.wait_for_url('**/post/1000?**');page.wait_for_load_state('networkidle');expect(page.locator('.lb-id')).to_have_text('#1000')
   expect(chip('canonical_name')).to_have_count(1);expect(chip('alpha')).to_have_count(0);expect(chip('beta').locator('.tc-lock')).to_have_class(re.compile(r'\bon\b'))
   page.reload(wait_until='networkidle');expect(chip('canonical_name')).to_have_count(1);expect(chip('alpha')).to_have_count(0)
   # Identity preferences apply inside the sidebar and in the stage when hidden.
   expect(panel.locator('.identity-summary')).to_have_count(1)
   expect(page.locator('.viewer-identities')).to_have_count(0)
   page.get_by_role('button',name='タグ編集を閉じる').click()
   expect(page.locator('.viewer-identities .identity-summary')).to_have_count(1)
   page.locator('.lb-top').get_by_role('button',name='タグ',exact=True).click()
   ctx.add_cookies([dict(name='kura_identity_display',value=quote(json.dumps({'viewer':False})),url=base)])
   page.reload(wait_until='networkidle');expect(page.locator('.identity-summary')).to_have_count(0)
   page.get_by_role('button',name='タグ編集を閉じる').click();expect(page.locator('.identity-summary')).to_have_count(0)
   page.locator('.lb-top').get_by_role('button',name='タグ',exact=True).click()
   ctx.add_cookies([dict(name='kura_identity_display',value=quote(json.dumps({'viewer':True})),url=base)])
   page.reload(wait_until='networkidle');expect(panel.locator('.identity-summary')).to_have_count(1)
   # All discard decisions are explicit; accepting internal navigation prompts once.
   dialogs=[];decision={'accept':False}
   def handle_dialog(dialog):
    dialogs.append(dialog.type)
    dialog.accept() if decision['accept'] else dialog.dismiss()
   page.on('dialog',handle_dialog)
   def prompt(action,accept=False):
    decision['accept']=accept;before=len(dialogs)
    with page.expect_event('dialog'):
     action()
    assert len(dialogs)==before+1
   def dirty():
    chip('canonical_name').get_by_role('button',name='remove',exact=True).click()
    expect(panel.get_by_role('button',name='保存',exact=True)).to_be_enabled()
   def wheel_next():
    page.wait_for_timeout(350)
    page.locator('.lb-stage').dispatch_event('wheel',dict(deltaY=100,bubbles=True,cancelable=True))
   dirty();prompt(wheel_next)
   expect(page.locator('.lb-id')).to_have_text('#1000');expect(chip('canonical_name')).to_have_count(0)
   prompt(wheel_next,True)
   page.wait_for_url('**/post/1001?**');page.wait_for_load_state('networkidle')
   assert len(dialogs)==2,'duplicate internal-navigation prompt'
   page.keyboard.press('ArrowLeft');page.wait_for_url('**/post/1000?**');page.wait_for_load_state('networkidle')
   expect(chip('canonical_name')).to_have_count(1)
   dirty();prompt(lambda: page.locator('.lb-close').click())
   expect(panel).to_have_count(1);expect(chip('canonical_name')).to_have_count(0)
   prompt(lambda: page.locator('.lb-close').click(),True);page.wait_for_url('**/?**');page.wait_for_load_state('networkidle')
   assert len(dialogs)==4,'duplicate close prompt'
   page.goto(base+'/post/1000?idx=0',wait_until='networkidle')
   dirty();prompt(lambda: chip('beta').locator('.tc-name').click())
   expect(chip('canonical_name')).to_have_count(0)
   prompt(lambda: chip('beta').locator('.tc-name').click(),True);page.wait_for_url('**/?q=beta');page.wait_for_load_state('networkidle')
   assert len(dialogs)==6,'duplicate direct-route prompt'
   page.go_back(wait_until='networkidle');expect(panel).to_have_count(1)
   dirty();prompt(lambda: page.evaluate('history.forward()'))
   expect(panel).to_have_count(1);expect(chip('canonical_name')).to_have_count(0)
   prompt(lambda: page.evaluate('history.forward()'),True);page.wait_for_url('**/?q=beta');page.wait_for_load_state('networkidle')
   page.go_back(wait_until='networkidle');expect(panel).to_have_count(1)
   dirty();prompt(lambda: page.evaluate('location.reload()'))
   assert dialogs[-1]=='beforeunload';expect(chip('canonical_name')).to_have_count(0)
   prompt(lambda: page.evaluate('location.reload()'),True)
   expect(chip('canonical_name')).to_have_count(1)
   page.wait_for_load_state('networkidle');assert len(dialogs)==10,'duplicate reload/history prompt'
   page.remove_listener('dialog',handle_dialog)
   page.screenshot(path='/tmp/single-tag-sidebar-desktop.png')
   for width,height in ((390,844),(320,844),(390,450)):
    page.set_viewport_size({'width':width,'height':height})
    page.reload(wait_until='networkidle')
    expect(panel).to_have_count(0)
    expect(page.locator('.viewer-identities')).not_to_be_visible()
    assert page.evaluate("localStorage.getItem('kura_single_tag_sidebar')")=='1'
    page.locator('.lb-top').get_by_role('button',name='タグ',exact=True).click();expect(panel).to_be_visible()
    box=panel.bounding_box();assert box and 0<=box['x']<20 and box['x']+box['width']<=width
    scroller=panel.locator('.lb-tag-scroll');assert scroller.evaluate('(e)=>e.scrollHeight>e.clientHeight')
    scroller.evaluate('(e)=>e.scrollTop=e.scrollHeight');assert scroller.evaluate('(e)=>e.scrollTop>0')
    panel.get_by_placeholder('+ tag').fill('alias_name');expect(panel.locator('.tsi-sug')).to_be_visible()
    sb=panel.locator('.tsi-sug').bounding_box();assert sb and sb['x']>=0 and sb['x']+sb['width']<=width and sb['y']>0
    panel.get_by_placeholder('+ tag').press('Escape')
    page.screenshot(path=f'/tmp/single-tag-sidebar-{width}x{height}.png')
    page.get_by_role('button',name='タグ編集を閉じる').click();expect(panel).to_have_count(0)
    expect(page.locator('.viewer-identities')).not_to_be_visible()
    assert page.evaluate("localStorage.getItem('kura_single_tag_sidebar')")=='1'
    page.locator('.lb-top').get_by_role('button',name='タグ',exact=True).click();expect(panel).to_have_count(1)
   page.evaluate("localStorage.setItem('kura_verify_overlay','1'); localStorage.setItem('kura_verify_header','1')")
   page.goto(base+'/post/1000?idx=0&mode=manga',wait_until='networkidle')
   expect(page.locator('.lb-tag-sidebar')).to_have_count(0)
   expect(page.locator('.vto.bottom')).to_have_count(1)
   assert not errors,errors
   assert len(WRITES)==2,WRITES
   print(json.dumps({'status':'PASS','checks':['sole left editor','delayed save blocks next/close/SPA without discard prompt','draft remove/undo','alias suggestion canonical add/save','pin prevents removal; delayed pin blocks save and same-tag removal','navigation/reload persistence','320/390px scroll and suggestions reachable','hide/reopen','identity preferences in sidebar/stage','dirty navigation/close/direct-route/history/reload dismiss and accept, one prompt'],'fixture_writes':len(WRITES),'console_errors':errors}))
   browser.close()
 finally:
  proc.terminate();proc.wait(timeout=10);api.shutdown()
