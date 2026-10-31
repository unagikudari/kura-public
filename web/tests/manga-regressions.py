#!/usr/bin/env python3
"""Synthetic GET-only browser regression probe; no real data, keys, or mutations.

Run from repository root with a Python environment containing Playwright:
  python web/tests/manga-regressions.py
Requires installed web/node_modules and Playwright Chromium.
Set CHROMIUM to a Chromium executable path, or KURA_UI_BROWSER_CHANNEL=chrome
to use installed Google Chrome instead.
Set KURA_UI_BROWSER_ENGINE=webkit for installed Playwright WebKit; select cases
without CDP touch input. KURA_MANGA_RASTER=1 uses a 2400x3600 PNG fixture.
Pillow is required for image generation and screenshot pixel checks.
Uses loopback ports 5185 (isolated Vite) and 5186 (synthetic GET-only API).
Outputs evidence to /tmp/kura-manga-regressions-results.json; exits nonzero on failure.
"""
import base64, hashlib, hmac, json, os, subprocess, threading, time, io
from PIL import Image
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from playwright.sync_api import sync_playwright
from browser_evidence import write_evidence
HERE=Path(__file__).resolve()
DEFAULT_ROOT=HERE.parents[2] if HERE.parent.name=='tests' else Path.cwd()
ROOT=Path(os.environ.get('KURA_UI_WORKTREE',DEFAULT_ROOT)).resolve()
ORIGIN='http://127.0.0.1:5185'; SECRET='synthetic-browser-audit-session-only'
FIXTURE={'missing':set(),'videos':set()}
PIXELS=io.BytesIO(); Image.new('RGB',(2400,3600),(68,85,102)).save(PIXELS,format='PNG');PNG=PIXELS.getvalue()
SVG='<svg xmlns="http://www.w3.org/2000/svg" width="600" height="900"><rect width="600" height="900" fill="#456"/></svg>'
def post(i):
 return dict(id=i,version=1,creationTime=None,lastEditTime=None,safety='safe',source=None,taggingSource=None,taggingCompletedAt=None,taggingTagsWritten=None,originalFilename=f'synthetic-{i}',type='video' if i in FIXTURE['videos'] else 'image',mimeType='video/mp4' if i in FIXTURE['videos'] else 'image/svg+xml',fileSize=100,canvasWidth=None if i in FIXTURE['missing'] else 600,canvasHeight=None if i in FIXTURE['missing'] else 900,contentUrl='/fixture/manga-video.mp4' if i in FIXTURE['videos'] else f'/fixture/{i}.svg',thumbnailUrl=f'/fixture/thumb-{i}.svg',flags=[],tags=[],user=None,score=0,ownFavorite=False,tagCount=0,favoriteCount=0,commentCount=0,noteCount=0,pools=[])
def response(url):
 u=urlparse(url);p=u.path;q=parse_qs(u.query); query=q.get('query',[''])[0]
 if p=='/api/tag-categories': return []
 if p=='/api/posts':
  offset=int(q.get('offset',['0'])[0]);limit=int(q.get('limit',['40'])[0]); total=1 if query=='new' else 160; base=(9000 if query=='new' else 1000)+(10000 if q.get('sort')==['updated'] else 0)
  return dict(results=[post(base+i) for i in range(offset,min(total,offset+limit))],total=total,offset=offset,limit=limit,query=query)
 if p=='/api/pools':return dict(results=[],total=0,offset=0,limit=40,query=query)
 if p.endswith('/index'):return {'idx':0}
 if p.endswith('/censor-annotations'):return {'results':[]}
 if p.startswith('/api/post/'):return post(int(p.split('/')[3]))
 if p=='/api/tags':return [dict(names=[query+'-result'],category='default',usages=1)]
 if p.endswith('/status'):return {'results':{}}
 return {}
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  body=json.dumps(response(self.path)).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
 def do_POST(self):self.send_error(405)
 do_PUT=do_PATCH=do_DELETE=do_POST

def run():
 api=ThreadingHTTPServer(('127.0.0.1',5186),Handler);threading.Thread(target=api.serve_forever,daemon=True).start()
 env=dict(os.environ,INTERNAL_API_URL='http://127.0.0.1:5186',PUBLIC_API_URL='',KURA_SESSION_SECRET=SECRET)
 subprocess.run(['node','scripts/gen-build-info.cjs'],cwd=ROOT/'web',env=env,check=True)
 log=open('/tmp/kura-manga-regressions-vite.log','w')
 vite=subprocess.Popen(['node','--input-type=module','-e',"import {createServer} from 'vite'; const s=await createServer({server:{host:'127.0.0.1',port:5185,strictPort:true,fs:{allow:ALLOW_PATHS}}});await s.listen();".replace("ALLOW_PATHS",json.dumps([str(ROOT),str((ROOT/"web/node_modules").resolve().parent)]))],cwd=ROOT/'web',env=env,stdout=log,stderr=log)
 results={}; violations=[]; errors=[]; runner_errors=[]; held_images=[]; held_posts=[]; conditions={'image_ids':set(),'fail_images':set(),'odd_thumb':False,'hold_posts':False}; browser_version=None
 try:
  import urllib.request
  for _ in range(100):
   if vite.poll() is not None:raise RuntimeError('Vite exited; see /tmp/kura-manga-regressions-vite.log')
   try:urllib.request.urlopen(ORIGIN+'/login',timeout=1);break
   except Exception:time.sleep(.2)
  else:raise RuntimeError('Vite readiness timed out')
  with sync_playwright() as p:
   engine=os.environ.get('KURA_UI_BROWSER_ENGINE','chromium')
   if engine not in ('chromium','webkit'): raise ValueError('unsupported browser engine')
   browser=(p.webkit.launch() if engine=='webkit' else p.chromium.launch(executable_path=os.environ.get('CHROMIUM'),channel=os.environ.get('KURA_UI_BROWSER_CHANNEL') or None,args=['--no-sandbox','--disable-dev-shm-usage']))
   browser_version=browser.version
   ctx=browser.new_context(viewport={'width':1300,'height':800},has_touch=True)
   enc=lambda b:base64.urlsafe_b64encode(b).rstrip(b'=').decode()
   payload=enc(json.dumps({'u':'synthetic','t':'synthetic','e':'','exp':int(time.time())+3600}).encode());sig=enc(hmac.new(SECRET.encode(),payload.encode(),hashlib.sha256).digest())
   ctx.add_cookies([dict(name='kura_session',value=payload+'.'+sig,url=ORIGIN)])
   def intercept(route):
    req=route.request;u=urlparse(req.url)
    if req.method not in ('GET','HEAD'):
     violations.append(req.method+' '+u.path);route.abort();return
    if u.hostname!='127.0.0.1':violations.append('external '+u.hostname);route.abort();return
    if u.path=='/api/posts' and conditions['hold_posts'] and parse_qs(u.query).get('limit')==['5']:
     held_posts.append(route);return
    if u.path.startswith('/api/'):
     route.fulfill(json=response(req.url));return
    if u.path.startswith('/fixture/') or req.resource_type in ('image','media'):
     if u.path in conditions['fail_images']: route.fulfill(status=404,body='missing preview');return
     if u.path in conditions['image_ids']:held_images.append(route);return
     if conditions['odd_thumb'] and u.path=='/fixture/thumb-1003.svg': route.fulfill(body=SVG.replace('600','300').replace('900','400'),content_type='image/svg+xml');return
     if u.path=='/fixture/manga-video.mp4':
      body=(HERE.parent/'fixtures/manga-video.mp4').read_bytes();route.fulfill(body=body,content_type='video/mp4');return
     if os.environ.get('KURA_MANGA_RASTER'): route.fulfill(body=PNG,content_type='image/png')
     else: route.fulfill(body=SVG,content_type='image/svg+xml')
     return
    route.continue_()
   ctx.add_init_script("window.fixturePreloads=[];window.Image=new Proxy(window.Image,{construct(target,args){const img=Reflect.construct(target,args);window.fixturePreloads.push(img);return img}})")
   ctx.add_init_script('window.fixtureNaturalWidth='+str(2400 if os.environ.get('KURA_MANGA_RASTER') else 600))
   ctx.route('**/*',intercept);page=ctx.new_page();page.set_default_timeout(7000);page.on('pageerror',lambda e:errors.append(str(e)))
   def go(path):
    page.goto(ORIGIN+path,wait_until='domcontentloaded')
    if 'mode=manga' in path:
     # Cold Vite compilation can outlast a fixed delay on CI. Wait for the
     # requested viewer and its client title before measuring wheel behavior.
     page.wait_for_function("document.title.startsWith('#') && document.querySelector('.stream .item.focused')",timeout=15000)
    page.wait_for_timeout(900)
   def release_images():
    conditions['image_ids']=set()
    while held_images:
     route=held_images.pop()
     if urlparse(route.request.url).path=='/fixture/manga-video.mp4':
      route.fulfill(body=(HERE.parent/'fixtures/manga-video.mp4').read_bytes(),content_type='video/mp4');continue
     route.fulfill(body=PNG if os.environ.get('KURA_MANGA_RASTER') else SVG,content_type='image/png' if os.environ.get('KURA_MANGA_RASTER') else 'image/svg+xml')
   def release_posts():
    conditions['hold_posts']=False
    while held_posts:
     route=held_posts.pop();route.fulfill(json=response(route.request.url))
   def test(name,fn):
    if os.environ.get('KURA_MANGA_CASES') and name not in os.environ['KURA_MANGA_CASES'].split(','):return
    try:results[name]=fn()
    except Exception as e:results[name]={'pass_':False,'error':str(e)[:600],'images':page.locator('.item.focused .manga-page').evaluate_all("es=>es.map(e=>({src:e.getAttribute('src'),width:e.naturalWidth,complete:e.complete,full:e.classList.contains('manga-full')}))")}
    finally:
     release_images();release_posts();FIXTURE['missing']=set();FIXTURE['videos']=set();conditions['fail_images']=set();conditions['odd_thumb']=False
    print(json.dumps({'case':name,**results[name]}),flush=True)
   def setup(direction='rtl',scroll='h'):
    go('/login');page.evaluate("([d,s])=>{localStorage.setItem('kura_read_dir',d);localStorage.setItem('kura_scroll_dir',s)}",[direction,scroll]);go('/post/1002?q=old&mode=manga&idx=2')
   def directional_prefetch_priority():
    go('/login');page.evaluate("localStorage.setItem('kura_read_dir','rtl');localStorage.setItem('kura_scroll_dir','h')")
    go('/post/1040?q=old&mode=manga&idx=40')
    initial=page.evaluate("window.fixturePreloads.filter(i=>new URL(i.src).pathname.startsWith('/fixture/')&&!i.src.includes('thumb-')).map(i=>({id:Number(new URL(i.src).pathname.split('/').pop().split('.')[0]),priority:i.fetchPriority}))")
    assert sorted(i['id'] for i in initial)==[1039,1040,1041,1042],initial
    assert all(i['priority']==('high' if i['id']==1040 else 'low') for i in initial),initial
    retained=page.locator('.item[data-pid="1040"] .manga-full')
    page.evaluate("window.priorityRetained=document.querySelector('.item[data-pid=\"1040\"] .manga-full')")
    page.keyboard.press('ArrowRight');page.wait_for_function("document.title==='#1039'")
    page.wait_for_function("document.querySelector('.item[data-pid=\"1039\"] .manga-full')?.fetchPriority==='high'")
    page.wait_for_function("window.fixturePreloads.some(i=>i.src.endsWith('/1037.svg'))")
    same=retained.evaluate('e=>e===window.priorityRetained&&e.complete&&e.naturalWidth>0')
    return dict(pass_=same,initial_full_requests=len(initial),previous_symmetric_budget=5,focus_priority='high',neighbor_priority='low',reverse_prefetch=True,retained_image=same)
   test('directional_prefetch_priority',directional_prefetch_priority)
   def pending_neighbor_priority():
    conditions['image_ids']={'/fixture/1003.svg'}
    setup()
    page.wait_for_function("window.fixturePreloads.some(i=>i.src.endsWith('/1003.svg')&&i.fetchPriority==='low')")
    page.evaluate("window.pendingNeighbor=window.fixturePreloads.find(i=>i.src.endsWith('/1003.svg'))")
    before=sum(urlparse(r.request.url).path=='/fixture/1003.svg' for r in held_images)
    page.keyboard.press('ArrowLeft');page.wait_for_function("document.title==='#1003'")
    page.wait_for_function("window.pendingNeighbor.fetchPriority==='high'")
    same=page.evaluate("window.fixturePreloads.filter(i=>i.src.endsWith('/1003.svg')).length===1")
    after=sum(urlparse(r.request.url).path=='/fixture/1003.svg' for r in held_images)
    release_images()
    page.wait_for_function("document.querySelector('.item.focused .manga-full')?.naturalWidth===window.fixtureNaturalWidth")
    return dict(pass_=same and before==1 and after==1,same_pending_request=same,requests_before=before,requests_after=after,promoted_to_high=True)
   test('pending_neighbor_priority',pending_neighbor_priority)
   def optional_neighbor_dimming():
    go('/login');page.evaluate("localStorage.removeItem('kura_manga_dim_unfocused')")
    setup()
    button=page.get_by_role('button',name='周辺を暗く',exact=True)
    def overlay():return page.locator('.item:not(.focused)').first.evaluate("e=>getComputedStyle(e,'::before').content")
    default_off=button.get_attribute('aria-pressed')=='false' and overlay()=='none'
    button.click();page.wait_for_function("document.querySelector('.stream').classList.contains('dim-unfocused')")
    enabled=overlay()!='none'
    page.reload(wait_until='domcontentloaded');page.wait_for_function("document.querySelector('.stream')?.classList.contains('dim-unfocused')")
    persisted=button.get_attribute('aria-pressed')=='true'
    button.click();page.wait_for_function("!document.querySelector('.stream').classList.contains('dim-unfocused')")
    return dict(pass_=default_off and enabled and persisted and overlay()=='none',default_off=default_off,toggle_enabled=enabled,persisted=persisted)
   test('optional_neighbor_dimming',optional_neighbor_dimming)
   def image_recovery(missing=False):
    setup();
    if missing: FIXTURE['missing']={1003}
    conditions['image_ids']={'/fixture/1003.svg'}
    # Remount the page so the full image is genuinely pending, not cached.
    go('/post/1003?q=old&mode=manga&idx=3')
    item=page.locator('.item[data-pid="1003"]')
    page.wait_for_function("document.querySelector('.item[data-pid=\"1003\"] .manga-preview').naturalWidth > 0")
    pending=item.locator('.manga-preview').count()==1 and item.locator('.manga-full').count()==0
    bounds=page.locator('.manga-full').count()<=5
    before=item.bounding_box()
    conditions['image_ids']=set()
    while held_images:held_images.pop().abort()
    item.locator('.retry').wait_for()
    item.locator('.retry').click()
    page.wait_for_function("()=>{const el=document.querySelector('.item[data-pid=\"1003\"] .manga-full');return el && el.complete && el.naturalWidth>0}")
    after=item.bounding_box()
    return dict(pass_=pending and bounds and abs(before['width']-after['width'])<1 and abs(after['width']/after['height']-2/3)<.01,pending_preview=pending,bounded_full_images=bounds,retry_loaded=True,stable_width=abs(before['width']-after['width'])<1,before_box=before,after_box=after)
   test('image_load_failure_retry',image_recovery)
   test('image_load_failure_retry_without_dimensions',lambda:image_recovery(True))
   def revisited_page_keeps_pixels(scroll='h'):
    setup(scroll=scroll)
    page.wait_for_function("()=>{const i=document.querySelector('.item[data-pid=\"1002\"] .manga-full');return i&&i.complete&&i.naturalWidth>0}")
    page.evaluate("window.retainedMangaImage=document.querySelector('.item[data-pid=\"1002\"] .manga-full')")
    # Once decoded, returning within the mounted window must not depend on a
    # second successful network response or even a working preview.
    conditions['fail_images']={'/fixture/1002.svg','/fixture/thumb-1002.svg'}
    for pid in (1003,1004,1005):
     if scroll=='h':page.keyboard.press('ArrowLeft')
     else:page.locator(f'.item[data-pid="{pid}"]').evaluate("e=>e.scrollIntoView({block:'center',behavior:'instant'})")
     page.wait_for_function("pid=>document.title==='#'+pid",arg=pid)
    retained=page.evaluate("window.retainedMangaImage.isConnected && document.querySelector('.item[data-pid=\"1002\"] .manga-full')===window.retainedMangaImage")
    for pid in (1004,1003,1002):
     if scroll=='h':page.keyboard.press('ArrowRight')
     else:page.locator(f'.item[data-pid="{pid}"]').evaluate("e=>e.scrollIntoView({block:'center',behavior:'instant'})")
     page.wait_for_function("pid=>document.title==='#'+pid",arg=pid)
    page.wait_for_function("()=>{const i=document.querySelector('.item.focused .manga-full');return i&&i.complete&&i.naturalWidth>0}")
    page.wait_for_timeout(250)
    item=page.locator('.item.focused');box=item.bounding_box();viewport=page.viewport_size
    x=int((max(0,box['x'])+min(viewport['width'],box['x']+box['width']))/2)
    y=int((max(0,box['y'])+min(viewport['height'],box['y']+box['height']))/2)
    pixel=Image.open(io.BytesIO(page.screenshot())).convert('RGB').getpixel((x,y))
    return dict(pass_=retained and all(c>30 for c in pixel),same_loaded_element=retained,return_pixel=pixel)
   test('revisited_page_keeps_pixels_horizontal',lambda:revisited_page_keeps_pixels('h'))
   test('revisited_page_keeps_pixels_vertical',lambda:revisited_page_keeps_pixels('v'))
   def painted_pages(scroll='h'):
    setup(scroll=scroll);samples=[]
    for i in range(1002,1010):
     if i>1002:
      if scroll=='h': page.keyboard.press('ArrowLeft');page.wait_for_function("id=>document.title==='#'+id",arg=i)
      else: page.locator(f'.item[data-pid=\"{i}\"]').evaluate("el=>el.scrollIntoView({block:'center',behavior:'instant'})")
     page.wait_for_timeout(400)
     item=page.locator(f'.item[data-pid="{i}"]');box=item.bounding_box()
     viewport=page.viewport_size
     left=max(0,box['x']);right=min(viewport['width'],box['x']+box['width'])
     top=max(0,box['y']);bottom=min(viewport['height'],box['y']+box['height'])
     x=int(left+(right-left)*.65);y=int(top+(bottom-top)*.5)
     pixel=Image.open(io.BytesIO(page.screenshot())).convert('RGB').getpixel((x,y))
     samples.append({'page':i,'pixel':pixel,'visible_width':right-left,'visible_height':bottom-top})
    return dict(pass_=all(all(c>30 for c in s['pixel']) and s['visible_width']>100 and s['visible_height']>100 for s in samples),samples=samples)
   test('painted_raster_horizontal',lambda:painted_pages('h'))
   test('painted_raster_vertical',lambda:painted_pages('v'))
   def missing_preview():
    setup();FIXTURE['missing']={1006};conditions['fail_images']={'/fixture/thumb-1006.svg'}
    go('/post/1006?q=old&mode=manga&idx=3')
    page.wait_for_function("()=>{const el=document.querySelector('.item[data-pid=\"1006\"] .manga-full');return el && el.complete && el.naturalWidth>0}")
    item=page.locator('.item[data-pid="1006"]');box=item.bounding_box()
    return dict(pass_=box['width']>100 and abs(box['width']/box['height']-2/3)<.01,loaded_full=True,page_width=box['width'])
   test('full_image_without_preview_or_dimensions',missing_preview)
   def decode_does_not_gate_paint():
    setup();page.add_init_script("HTMLImageElement.prototype.decode=()=>new Promise(()=>{})")
    go('/post/1007?q=old&mode=manga&idx=7')
    page.wait_for_function("()=>{const el=document.querySelector('.item[data-pid=\"1007\"] .manga-full');return el && el.complete && el.naturalWidth>0}")
    img=page.locator('.item[data-pid="1007"] .manga-full')
    css=img.evaluate("el=>({opacity:getComputedStyle(el).opacity,filter:getComputedStyle(el).filter,position:getComputedStyle(el).position})")
    return dict(pass_=css['opacity']=='1' and css['filter']=='none' and css['position']!='absolute',css=css)
   test('loaded_image_without_decode_promise',decode_does_not_gate_paint)
   def failed_preview_entering_focus():
    setup();conditions['fail_images']={'/fixture/thumb-1005.svg'};conditions['image_ids']={'/fixture/1005.svg'}
    go('/post/1002?q=old&mode=manga&idx=2')
    page.locator('.item[data-pid="1005"] .manga-page').evaluate("el=>el.loading='eager'")
    page.wait_for_function("()=>{const el=document.querySelector('.item[data-pid=\"1005\"] .manga-page');return el.complete && el.naturalWidth===0}")
    for i in (1003,1004,1005):
     page.keyboard.press('ArrowLeft');page.wait_for_function("i=>document.title==='#'+i",arg=i)
    item=page.locator('.item[data-pid="1005"]')
    item.get_by_role('status').wait_for();item.locator('.retry').wait_for()
    return dict(pass_=item.get_by_role('status').inner_text()=='画像を読み込み中…',held_full=True,failed_preview_status=True)
   test('failed_preview_enters_near_window',failed_preview_entering_focus)
   def rounded_thumbnail_ratio():
    setup();conditions['odd_thumb']=True;conditions['image_ids']={'/fixture/1003.svg'}
    go('/post/1003?q=old&mode=manga&idx=3')
    item=page.locator('.item[data-pid="1003"]');page.wait_for_function("document.querySelector('.item[data-pid=\"1003\"] .manga-preview').naturalWidth===300")
    before=item.bounding_box();release_images()
    page.wait_for_function("document.querySelector('.item[data-pid=\"1003\"] .manga-full')")
    page.wait_for_timeout(100);after=item.bounding_box()
    return dict(pass_=abs(before['width']-after['width'])<1 and abs(after['width']/after['height']-2/3)<.01,before_width=before['width'],after_width=after['width'])
   test('rounded_thumbnail_preserves_page_geometry',rounded_thumbnail_ratio)
   def repeated_window_turns():
    setup();go('/post/1040?q=old&mode=manga&idx=40');samples=[]
    page.evaluate("""window.retainedShell=document.querySelector('.item[data-pid="1040"]')""")
    for i in range(1040,1101):
     if i>1040:
      page.keyboard.press('ArrowLeft');page.wait_for_function("i=>document.title==='#'+i",arg=i)
     page.wait_for_timeout(120)
     if i%5==0:
      item=page.locator(f'.item[data-pid="{i}"]');box=item.bounding_box();vp=page.viewport_size
      left=max(0,box['x']);right=min(vp['width'],box['x']+box['width'])
      x=int(left+(right-left)*.65);y=int(box['y']+min(box['height'],vp['height']-box['y'])*.5)
      pixel=Image.open(io.BytesIO(page.screenshot())).convert('RGB').getpixel((x,y))
      samples.append({'page':i,'pixel':pixel,'visible_width':right-left,'items':page.locator('.stream .item').count(),'mounted':page.locator('.stream .item[data-mounted="true"]').count(),'images':page.locator('.stream .manga-page').count(),'full':page.locator('.stream .manga-full').count()})
    retained=page.evaluate("""window.retainedShell.isConnected && window.retainedShell===document.querySelector('.item[data-pid="1040"]')""")
    page.keyboard.press('ArrowRight');page.wait_for_function("document.title==='#1099' && !!document.querySelector('.item.focused .manga-full')")
    return dict(retained_shell=retained,pass_=retained and all(s['visible_width']>100 and all(c>30 for c in s['pixel']) and s['mounted']<=13 and s['images']<=13 and s['full']<=13 for s in samples) and samples[-1]['items']>samples[0]['items'],samples=samples)
   test('repeated_five_page_windows',repeated_window_turns)
   def distant_unknown_geometry():
    setup();FIXTURE['missing'].add(1020);conditions['image_ids']={'/fixture/thumb-1020.svg'}
    go('/post/1040?q=old&mode=manga&idx=40')
    item=page.locator('.item[data-pid="1020"]')
    waiting=item.get_attribute('data-mounted')=='true'
    release_images();page.wait_for_function("document.querySelector('.item[data-pid=\"1020\"]').dataset.mounted==='false'")
    before=item.bounding_box()['width']
    page.set_viewport_size({'width':1100,'height':700});page.wait_for_timeout(300)
    page.wait_for_function("document.querySelector('.item[data-pid=\"1020\"]').dataset.mounted==='false'")
    resized=item.bounding_box()['width'];stream_height=page.locator('.stream').bounding_box()['height']
    for _ in range(20):page.keyboard.press('ArrowRight');page.wait_for_timeout(100)
    page.wait_for_function("document.title==='#1020' && !!document.querySelector('.item.focused .manga-full')")
    after=item.bounding_box()['width']
    page.set_viewport_size({'width':1300,'height':800})
    return dict(pass_=waiting and before>400 and abs(resized/stream_height-2/3)<.01 and abs(after-resized)<1,waiting_for_intrinsic_size=waiting,before=before,resized=resized,after_remount=after)
   test('distant_unknown_geometry_and_resize',distant_unknown_geometry)
   def distant_video_resize():
    setup();FIXTURE['videos'].add(1020);FIXTURE['missing'].add(1020);conditions['image_ids']={'/fixture/manga-video.mp4'}
    go('/post/1040?q=old&mode=manga&idx=40')
    item=page.locator('.item[data-pid="1020"]');waiting=item.get_attribute('data-mounted')=='true'
    release_images();page.wait_for_function("document.querySelector('.item[data-pid=\"1020\"]').dataset.mounted==='false'")
    before=item.bounding_box()['width']
    page.set_viewport_size({'width':1100,'height':700});page.wait_for_timeout(300)
    page.wait_for_function("document.querySelector('.item[data-pid=\"1020\"]').dataset.mounted==='false'")
    resized=item.bounding_box()['width']
    for _ in range(20):page.keyboard.press('ArrowRight');page.wait_for_timeout(100)
    page.wait_for_function("document.title==='#1020' && document.querySelector('.item.focused video').videoWidth>0")
    after=item.bounding_box()['width'];videos=page.locator('.stream video').count()
    page.set_viewport_size({'width':1300,'height':800})
    return dict(pass_=waiting and before>100 and resized>100 and abs(after-resized)<1 and videos==1,waiting_for_metadata=waiting,before=before,resized=resized,after_remount=after,videos=videos)
   test('distant_video_geometry_and_resize',distant_video_resize)
   def focused():return int(page.title().lstrip('#'))
   def position():return page.locator('.stream').evaluate('el=>el.scrollLeft')
   def trace_start():
    page.evaluate("window.motion=[];window.tracing=true;function sample(){if(!window.tracing)return;window.motion.push(document.querySelector('.stream').scrollLeft);requestAnimationFrame(sample)};requestAnimationFrame(sample)")
   def trace_stop():return page.evaluate('window.tracing=false;window.motion')
   def wheel(direction,axis):
    setup(direction);page.mouse.move(650,400);sign=-1 if direction=='rtl' else 1;before=position();trace_start()
    # A sustained wheel must keep moving through multiple pages, not discard
    # all but its first event. Test the physical horizontal axis as well.
    for _ in range(20):
     page.mouse.wheel(sign*120 if axis=='x' else 0,120 if axis=='y' else 0);page.wait_for_timeout(50)
    page.wait_for_timeout(700);after=position();trace=trace_stop();page.wait_for_timeout(700);idle=position()
    steps=[(b-a)*sign for a,b in zip(trace,trace[1:])]
    return dict(pass_=abs((after-before)*sign-2400)<=3 and all(-1<=d<=164 for d in steps) and len(set(trace))>20 and abs(idle-after)<=1,before=before,after=after,idle=idle,min_step=min(steps),max_step=max(steps),distinct_frames=len(set(trace)))
   for direction in ['rtl','ltr']:
    for axis in ['x','y']:test('continuous_'+axis+'_'+direction,lambda d=direction,a=axis:wheel(d,a))
   def small_wheel(direction,axis):
    setup(direction);page.mouse.move(650,400);sign=-1 if direction=='rtl' else 1;before=position();seen=[]
    # Small moves remain where the user leaves them, including after idle.
    for delta in [1,3,20,-1,-3,-20]:
     page.mouse.wheel(sign*delta if axis=='x' else 0,delta if axis=='y' else 0);page.wait_for_timeout(700);seen.append(round((position()-before)*sign))
    return dict(pass_=seen==[1,4,24,23,20,0],seen=seen)
   for direction in ['rtl','ltr']:
    for axis in ['x','y']:test('small_slow_'+axis+'_'+direction,lambda d=direction,a=axis:small_wheel(d,a))
   def drag(direction):
    setup(direction);el=page.locator('.stream.horizontal');before=el.evaluate('el=>el.scrollLeft');sign=1 if direction=='rtl' else -1;distance=page.locator('.item[data-pid="1002"]').bounding_box()['width']
    page.mouse.move(650,400);page.mouse.down();page.mouse.move(650+sign*distance,400,steps=15);during=el.evaluate('el=>el.scrollLeft');page.mouse.up();page.wait_for_timeout(1100)
    return dict(pass_=abs((during-before)+sign*distance)<3 and focused()==1003,before=before,during=during,focused=focused())
   for direction in ['rtl','ltr']:test('drag_'+direction,lambda d=direction:drag(d))
   def pinch():
    setup();page.mouse.move(650,400);page.keyboard.down('Control');page.mouse.wheel(0,100);page.keyboard.up('Control');page.wait_for_timeout(900)
    return dict(pass_=focused()==1002,focused=focused())
   test('ctrl_wheel_no_paging',pinch)
   def touch(direction):
    setup(direction);sign=1 if direction=='rtl' else -1;distance=page.locator('.item[data-pid="1002"]').bounding_box()['width'];cdp=ctx.new_cdp_session(page)
    cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':650,'y':400}]})
    for n in range(1,13):
     cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':650+sign*n*distance/12,'y':400}]});page.wait_for_timeout(45)
    cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]});page.wait_for_timeout(1300);cdp.detach()
    return dict(pass_=focused()==1003,focused=focused())
   for direction in ['rtl','ltr']:test('touch_'+direction,lambda d=direction:touch(d))
   def horizontal(direction):
    setup(direction);distance=page.locator('.item[data-pid="1002"]').bounding_box()['width'];page.mouse.move(650,400);page.mouse.wheel(-distance if direction=='rtl' else distance,0);page.wait_for_timeout(1000)
    return dict(pass_=focused()==1003,focused=focused())
   for direction in ['rtl','ltr']:test('horizontal_trackpad_'+direction,lambda d=direction:horizontal(d))
   def mixed():
    setup();page.mouse.move(650,400);page.mouse.wheel(0,100);page.wait_for_timeout(80)
    distance=page.locator('.item[data-pid="1003"]').bounding_box()['width'];page.mouse.down();page.mouse.move(650+distance,400,steps=8);page.mouse.up();page.wait_for_timeout(900)
    return dict(pass_=focused()==1003,focused=focused())
   test('wheel_then_immediate_drag',mixed)
   def reverse():
    setup();page.mouse.move(650,400);page.mouse.wheel(0,400);page.wait_for_timeout(30);trace_start();page.mouse.wheel(0,-80);page.wait_for_timeout(600);trace=trace_stop()
    # Direction reversal discards residual forward animation immediately.
    steps=[b-a for a,b in zip(trace[2:],trace[3:])]
    return dict(pass_=bool(steps) and min(steps)>=-1,min_step=min(steps),max_step=max(steps))
   test('reverse_cancels_pending_motion',reverse)
   def keyboard_after_wheel():
    setup();page.mouse.move(650,400);page.mouse.wheel(0,100);page.wait_for_timeout(30);page.keyboard.press('ArrowLeft');page.wait_for_timeout(900)
    return dict(pass_=focused()==1003 and page.locator('.stream').evaluate('el=>el.style.scrollSnapType')=='',focused=focused())
   test('keyboard_restores_page_snapping',keyboard_after_wheel)
   def click_after_wheel():
    setup();page.mouse.move(650,400);page.mouse.wheel(0,120);page.wait_for_timeout(600);before=position()
    # A non-navigation click must not turn persistent wheel pan into a snap.
    page.locator('.sub-bar .bi').click();page.wait_for_timeout(700);after=position()
    return dict(pass_=abs(after-before)<=1 and page.locator('.stream').evaluate('el=>el.style.scrollSnapType')=='none',before=before,after=after)
   test('non_drag_click_preserves_wheel_position',click_after_wheel)
   def boundary():
    setup();go('/post/1000?q=old&mode=manga&idx=0');page.mouse.move(650,400);page.mouse.wheel(0,-100);page.wait_for_timeout(900)
    return dict(pass_=focused()==1000,focused=focused())
   test('wheel_first_page_boundary',boundary)
   def vertical():
    setup(scroll='v');el=page.locator('.stream');before=el.evaluate('el=>el.scrollTop');page.mouse.move(650,400);page.mouse.wheel(0,150);page.wait_for_timeout(500);after=el.evaluate('el=>el.scrollTop')
    return dict(pass_=after>before,before=before,after=after)
   test('vertical_native_scroll',vertical)
   def blur_retains_position():
    setup();page.mouse.move(650,400);page.mouse.wheel(0,80);page.wait_for_timeout(600);before=position()
    page.evaluate("window.dispatchEvent(new Event('blur'))");page.wait_for_timeout(700)
    page.evaluate("window.dispatchEvent(new Event('focus'))");after=position()
    return dict(pass_=abs(after-before)<1 and page.locator('.stream').evaluate('el=>el.style.scrollSnapType')=='none',before=before,after=after,input='Playwright wheel followed by DOM focus lifecycle events')
   test('blur_retains_partial_pan',blur_retains_position)
   def queued_navigation_takeover(direction):
    setup(direction);before=position();sign=-1 if direction=='rtl' else 1
    # Same task makes the otherwise narrow key-frame / wheel ordering reproducible.
    page.evaluate("""key=>{
      document.body.dispatchEvent(new KeyboardEvent('keydown',{key,bubbles:true}));
      document.querySelector('.stream').dispatchEvent(new WheelEvent('wheel',{deltaY:80,bubbles:true,cancelable:true}));
    }""",'ArrowLeft' if direction=='rtl' else 'ArrowRight')
    page.wait_for_timeout(750);after=position()
    return dict(pass_=abs((after-before)*sign-80)<3,before=before,after=after,input='DOM KeyboardEvent then WheelEvent in one task')
   for direction in ['rtl','ltr']:test('queued_navigation_takeover_'+direction,lambda d=direction:queued_navigation_takeover(d))
   def long_drag(direction,edge):
    setup(direction);go('/post/1070?q=old&mode=manga&idx=70')
    initial=page.locator('.stream .item').evaluate_all('es=>es.map(e=>Number(e.dataset.pid))')
    conditions['hold_posts']=True
    forward='ArrowLeft' if direction=='rtl' else 'ArrowRight'
    key=forward if edge=='append' else ('ArrowRight' if direction=='rtl' else 'ArrowLeft')
    for _ in range(26):
     page.keyboard.press(key);page.wait_for_timeout(80)
     if held_posts:break
    assert held_posts,'real sentinel did not request the next window'
    requested=[parse_qs(urlparse(r.request.url).query) for r in held_posts]
    page.mouse.move(650,400);page.mouse.down();page.mouse.move(680,400,steps=3)
    page.evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    pid=focused();item=page.locator(f'.item[data-pid="{pid}"]');before=item.bounding_box()['x']
    release_posts();page.wait_for_timeout(200)
    after_load=item.bounding_box()['x'];loaded=page.locator('.stream .item').evaluate_all('es=>es.map(e=>Number(e.dataset.pid))')
    page.mouse.move(700,400);page.evaluate('new Promise(resolve=>requestAnimationFrame(resolve))');after_move=item.bounding_box()['x'];page.mouse.up();page.wait_for_timeout(500)
    changed=set(initial).issubset(loaded) and (max(loaded)>max(initial) if edge=='append' else min(loaded)<min(initial))
    return dict(pass_=changed and abs(after_load-before)<3 and abs(after_move-after_load-20)<3,initial=[min(initial),max(initial)],loaded=[min(loaded),max(loaded)],requests=requested,page=pid,before=before,after_load=after_load,after_move=after_move)
   for direction in ['rtl','ltr']:
    for edge in ['append','prepend']:test('drag_'+edge+'_trim_'+direction,lambda d=direction,e=edge:long_drag(d,e))
   def delayed_image(direction,missing):
    setup(direction);conditions['image_ids']={'/fixture/1071.svg','/fixture/thumb-1071.svg'}
    FIXTURE['missing']={1071} if missing else set()
    go('/post/1070?q=old&mode=manga&idx=70')
    assert held_images,'image request was not actually delayed'
    page.mouse.move(650,400);page.mouse.wheel(0,80);page.wait_for_timeout(100)
    page.mouse.down();page.mouse.move(680,400,steps=3)
    page.evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
    pid=focused();item=page.locator(f'.item[data-pid="{pid}"]');before=item.bounding_box()['x']
    release_images();page.wait_for_timeout(120)
    after_load=item.bounding_box()['x'];page.mouse.move(700,400);page.evaluate('new Promise(resolve=>requestAnimationFrame(resolve))');after_move=item.bounding_box()['x'];page.mouse.up()
    page.keyboard.press('ArrowLeft' if direction=='rtl' else 'ArrowRight');page.wait_for_timeout(750)
    final=focused();img=page.locator('.item[data-pid="1071"] .manga-page')
    decoded=img.count()>0 and img.evaluate('el=>el.complete && el.naturalWidth===window.fixtureNaturalWidth')
    return dict(pass_=decoded and abs(after_load-before)<3 and abs(after_move-after_load-20)<3 and final==pid+1,dimensions='missing on delayed page' if missing else 'known',delayed_page=1071,page=pid,before=before,after_load=after_load,after_move=after_move,final=final,decoded=decoded)
   for direction in ['rtl','ltr']:
    for missing in [False,True]:test('delayed_image_'+('missing_' if missing else 'known_')+direction,lambda d=direction,m=missing:delayed_image(d,m))
   def pending_correction(direction,zoom=False):
    setup(direction);conditions['image_ids']={'/fixture/1071.svg','/fixture/thumb-1071.svg'};FIXTURE['missing']={1071}
    go('/post/1070?q=old&mode=manga&idx=70');assert held_images
    page.keyboard.press('ArrowLeft' if direction=='rtl' else 'ArrowRight')
    # Let alignment run and arm its 200/500ms correction callbacks, but do not
    # wait through either deadline before the manual takeover and layout change.
    page.wait_for_timeout(40);assert focused()==1071
    img=page.locator('.item[data-pid="1071"] .manga-page');width_before=img.bounding_box()['width']
    before=position();page.mouse.move(650,400)
    if zoom:page.locator('.stream').evaluate("el=>el.dispatchEvent(new WheelEvent('wheel',{deltaY:20,ctrlKey:true,bubbles:true,cancelable:true}))")
    else:page.mouse.wheel(0,20)
    page.wait_for_timeout(60)
    release_images();page.wait_for_function("document.querySelector('.item[data-pid=\"1071\"] img').naturalWidth===window.fixtureNaturalWidth")
    width_after=img.bounding_box()['width'];page.wait_for_timeout(220);after=position();page.wait_for_timeout(650);idle=position()
    sign=-1 if direction=='rtl' else 1
    return dict(pass_=abs(width_after-width_before)>8 and abs((after-before)*sign-(0 if zoom else 20))<3 and abs(idle-after)<1,ctrl_wheel=zoom,width_before=width_before,width_after=width_after,before=before,after=after,idle=idle,input=('DOM ctrl-wheel event' if zoom else 'Playwright key then wheel')+' before correction deadlines; held layout-changing image')
   for direction in ['rtl','ltr']:test('pending_correction_takeover_'+direction,lambda d=direction:pending_correction(d))
   test('ctrl_wheel_cancels_pending_correction',lambda:pending_correction('rtl',True))
   def teardown():
    setup();page.mouse.move(650,400);page.mouse.wheel(0,800)
    # Keep the old element and instrument only writes after Svelte removes it.
    page.evaluate("""()=>{const el=document.querySelector('.stream');window.oldStream=el;window.detachedWrites=0;const original=el.scrollTo.bind(el);el.scrollTo=(...args)=>{if(!el.isConnected)window.detachedWrites++;original(...args)}}""")
    page.locator('.sub-bar .dir-btn').first.click();page.wait_for_timeout(200)
    vertical=page.locator('.stream').evaluate('el=>!el.classList.contains("horizontal") && el.style.scrollSnapType===""')
    # Client navigation preserves the document so detached callbacks remain observable.
    page.locator('a.logo').click()
    page.wait_for_function('window.oldStream && !window.oldStream.isConnected');page.wait_for_timeout(700)
    writes=page.evaluate('window.detachedWrites')
    return dict(pass_=vertical and writes==0,vertical=vertical,detached_writes=writes)
   test('mode_change_and_teardown',teardown)
   browser.close()
 except Exception as exc:
  runner_errors.append({'kind':'runner_error','detail':str(exc)[:600]})
 finally:
  vite.terminate();vite.wait(timeout=10);api.shutdown();log.close()
 failure_details=runner_errors+[{'kind':'page_error','detail':e} for e in errors[:15]]+[{'kind':'forbidden_request','detail':v} for v in violations]
 raise SystemExit(write_evidence(root=ROOT,cases=results,errors=failure_details,browser_version=browser_version,case_filter=os.environ.get('KURA_MANGA_CASES')))
if __name__=='__main__':run()
