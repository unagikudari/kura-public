"""Standalone Chrome regression with synthetic API only; no live Kura writes.
Run after npm run build with a Python environment containing Playwright.
"""
import base64, hashlib, hmac, json, os, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'localization-browser-test-only-not-a-production-key'
TAGS = [dict(id=i, version=1, names=[name], localizedNames={'ja':'共通名','en':en}, category='character', categoryColor='#ffc878', usages=0, description='', creationTime=None,lastEditTime=None) for i,name,en in [(91,'first_character','First'),(92,'second_character','Second')]]
WRITES=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def send(self,value,status=200):
  data=json.dumps(value,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Access-Control-Allow-Origin','*');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_GET(self):
  p=urlparse(self.path);q=parse_qs(p.query)
  if p.path=='/api/tag-categories':return self.send([dict(name='character',color='#ffc878',default=False)])
  if p.path=='/api/tag-relations':return self.send([])
  if p.path=='/api/tag-browser':return self.send(dict(categories=[dict(name='character',color='#ffc878',default=False)],tagsByCat={'character':TAGS}))
  if p.path=='/api/tags':
   if int(q.get('limit',['10'])[0])<=50 and not q.get('category'):return self.send(TAGS)
   return self.send(dict(query='',offset=0,limit=60,total=2,results=TAGS))
  if p.path=='/api/posts':return self.send(dict(query='',offset=0,limit=40,total=0,results=[]))
  if p.path=='/api/pools':return self.send(dict(query='',offset=0,limit=40,total=0,results=[]))
  return self.send({})
 def do_PATCH(self):
  if self.headers.get('Transfer-Encoding') == 'chunked':
   chunks=[]
   while True:
    size=int(self.rfile.readline().strip().split(b';')[0],16)
    if size == 0:
     self.rfile.readline();break
    chunks.append(self.rfile.read(size));self.rfile.read(2)
   raw=b''.join(chunks)
  else:raw=self.rfile.read(int(self.headers['Content-Length']))
  body=json.loads(raw);WRITES.append((self.path,body));assert self.path=='/api/tag-by-id/91?localize=true' 
  if '__proto__' in body.get('localizedNames',{}):return self.send({'detail':{'fix':'invalid locale'}},422)
  TAGS[0].update(body);TAGS[0]['version']+=1;self.send(TAGS[0])

server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
api=f'http://127.0.0.1:{server.server_port}'
# Bind an isolated development port. Fail if occupied; never stop another server.
port=18423
base=f'http://127.0.0.1:{port}'
env={**os.environ,'HOST':'127.0.0.1','PORT':str(port),'ORIGIN':base,'INTERNAL_API_URL':api,'PUBLIC_API_URL':api,'KURA_SESSION_SECRET':SECRET,'KURA_NAV_LINKS':os.environ.get('KURA_TEST_NAV_LINKS','')}
errors=[]
with tempfile.TemporaryFile() as log:
 proc=subprocess.Popen(['node','tests/serve-fixture.mjs'],cwd=ROOT,env=env,stdout=log,stderr=log)
 try:
  import urllib.request
  deadline=time.monotonic()+15
  while True:
   if proc.poll() is not None:raise RuntimeError('fixture web exited')
   try:
    urllib.request.urlopen(base+'/login',timeout=0.3);break
   except Exception:
    if time.monotonic()>deadline:raise RuntimeError('fixture web readiness timeout')
    time.sleep(0.1)
  enc=lambda b:base64.urlsafe_b64encode(b).decode().rstrip('=')
  payload=enc(json.dumps(dict(u='fixture',t='fixture-token',e='fixture@example.invalid',exp=int(time.time())+3600)).encode())
  cookie=payload+'.'+enc(hmac.new(SECRET.encode(),payload.encode(),hashlib.sha256).digest())
  with sync_playwright() as pw:
   browser=pw.chromium.launch(executable_path=os.environ.get('CHROMIUM'),headless=True)
   context=browser.new_context(viewport={'width':1280,'height':900})
   context.add_cookies([dict(name='kura_session',value=cookie,url=base),dict(name='kura_cachefix1',value='1',url=base)])
   page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
   page.goto(base+'/tags',wait_until='networkidle')
   expect(page.locator('.tc-pill').filter(has_text='共通名 (first_character)')).to_have_count(1)
   expect(page.locator('.tc-pill').filter(has_text='共通名 (second_character)')).to_have_count(1)
   page.get_by_role('button',name='メニュー',exact=True).click()
   expected_links=json.loads(os.environ.get('KURA_TEST_NAV_LINKS') or '[]')
   for link in expected_links:
    expect(page.get_by_role('link',name=link['label'],exact=False)).to_have_attribute('href',link['url'])
   if not expected_links:
    expect(page.locator('.menu-label').filter(has_text='Links')).to_have_count(0)
   if os.environ.get('KURA_SCREENSHOT_DIR'):
    output=Path(os.environ['KURA_SCREENSHOT_DIR']);output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/'navigation-desktop.png'))
   page.get_by_label('タグの表示言語').select_option('en')
   expect(page.locator('.tc-pill').filter(has_text='First')).to_have_count(1)
   page.reload(wait_until='networkidle')
   expect(page.locator('.tc-pill').filter(has_text='First')).to_have_count(1)
   search=page.get_by_placeholder('タグ・キーワード / #ID');search.fill('共通名')
   expect(page.locator('.tsi-name').filter(has_text='first_character')).to_have_count(1)
   expect(page.locator('.tsi-name').filter(has_text='second_character')).to_have_count(1)
   search.press('ArrowDown');search.press('Enter')
   expect(search).to_have_value('first_character ')
   page.goto(base+'/tags/edit',wait_until='networkidle')
   page.get_by_role('button',name='編集',exact=True).first.click()
   expect(page.get_by_label('言語コード')).to_have_count(2)
   page.get_by_role('button',name='+ 言語を追加',exact=True).click()
   page.get_by_label('言語コード').last.fill('__proto__')
   page.get_by_label('言語別表示名').last.fill('must not disappear')
   page.get_by_role('button',name='保存',exact=True).click()
   expect(page.locator('.err')).to_contain_text('invalid locale')
   assert WRITES[-1][1]['localizedNames']['__proto__']=='must not disappear'
   assert TAGS[0]['localizedNames']['ja']=='共通名'
   page.get_by_role('button',name='表示名を削除',exact=True).last.click()
   page.get_by_label('言語別表示名').first.fill('新しい表示名')
   page.get_by_role('button',name='保存',exact=True).click()
   expect(page.get_by_label('言語コード')).to_have_count(0)
   assert WRITES and WRITES[-1][1]['names']==['first_character']
   assert WRITES[-1][1]['localizedNames']['ja']=='新しい表示名'
   page.set_viewport_size({'width':390,'height':844})
   page.get_by_role('button',name='メニュー',exact=True).click()
   if os.environ.get('KURA_SCREENSHOT_DIR'):
    page.screenshot(path=str(Path(os.environ['KURA_SCREENSHOT_DIR'])/'navigation-mobile.png'))
   page.get_by_label('タグの表示言語').select_option('ja')
   assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile horizontal overflow'
   page.get_by_label('タグの表示言語').select_option('en')
   # Check inner scroll containers too: the body can fit while an editor is clipped.
   touch=browser.new_context(viewport={'width':390,'height':844},has_touch=True,is_mobile=True)
   touch.add_cookies([dict(name='kura_session',value=cookie,url=base)])
   mobile=touch.new_page();mobile.on('pageerror',lambda e:errors.append(str(e)))
   for width in (390,320):
    mobile.set_viewport_size({'width':width,'height':844})
    for route in ('/tags/manage','/tags/edit'):
     mobile.goto(base+route,wait_until='networkidle')
     if route.endswith('/manage'):expect(mobile.locator('.card')).to_have_count(2)
     if route.endswith('/edit'):
      mobile.get_by_role('button',name='編集',exact=True).first.click()
      for label in ('言語コード','言語別表示名'):
       for control in mobile.get_by_label(label,exact=True).all():
        box=control.bounding_box();assert box and box['x']>=0 and box['x']+box['width']<=width,(width,label,box)
      actions=mobile.locator('.tag-row').first.locator('.act-btn').evaluate_all('(els)=>els.map(e=>({x:e.getBoundingClientRect().x,w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height}))')
      assert len(actions)==4 and all(a['x']>=0 and a['x']+a['w']<=width and a['h']>=44 for a in actions),actions
     assert mobile.evaluate('document.documentElement.scrollWidth<=innerWidth && [...document.querySelectorAll(".scroll-area,.panel,.topbar")].every(e=>e.scrollWidth<=e.clientWidth+1)'),(width,route,'inner overflow')
    mobile.get_by_role('button',name='メニュー',exact=True).click()
    menu=mobile.locator('.menu').bounding_box();assert menu and menu['x']>=0 and menu['x']+menu['width']<=width,menu
   touch.close()
   # A second SSR session must not inherit the first session's English locale.
   fresh=browser.new_context();fresh.add_cookies([dict(name='kura_session',value=cookie,url=base)])
   p2=fresh.new_page();p2.goto(base+'/tags',wait_until='networkidle');expect(p2.locator('.tc-pill').filter(has_text='新しい表示名')).to_have_count(1)
   assert not errors,errors
   browser.close()
   print('PASS: same-label candidates, canonical selection, language switch/reload, stable-ID edit, mobile layout, SSR isolation')
 finally:
  proc.terminate()
  try:proc.wait(timeout=5)
  except subprocess.TimeoutExpired:proc.kill();proc.wait()
  server.shutdown();server.server_close()
