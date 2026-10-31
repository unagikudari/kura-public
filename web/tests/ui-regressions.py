#!/usr/bin/env python3
"""Synthetic GET-only browser regression probe; no real data, keys, or mutations.

Run from repository root with a Python environment containing Playwright:
  python web/tests/ui-regressions.py
Requires installed web/node_modules and Playwright Chromium.
Set KURA_UI_BROWSER_CHANNEL=chrome to use installed Google Chrome instead.
Uses loopback ports 5179 (isolated Vite) and 5180 (synthetic GET-only API).
Outputs evidence to /tmp/kura-ui-regressions-results.json; exits nonzero on failure.
"""
import base64, hashlib, hmac, json, os, re, subprocess, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from playwright.sync_api import sync_playwright
HERE=Path(__file__).resolve()
DEFAULT_ROOT=HERE.parents[2] if HERE.parent.name=='tests' else Path.cwd()
ROOT=Path(os.environ.get('KURA_UI_WORKTREE',DEFAULT_ROOT)).resolve()
ORIGIN='http://127.0.0.1:5179'; SECRET='synthetic-browser-audit-session-only'
FIXTURE={'pool_shift':0,'rich':False,'large':False}
SVG='<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="#456"/></svg>'
def post(i):
 row=dict(id=i,version=1,creationTime=None,lastEditTime=None,safety='safe',source=None,taggingSource=None,taggingCompletedAt=None,taggingTagsWritten=None,originalFilename=f'synthetic-{i}',type='image',mimeType='image/svg+xml',fileSize=100,canvasWidth=80,canvasHeight=80,contentUrl=f'/fixture/{i}.svg',thumbnailUrl=f'/fixture/{i}.svg',flags=[],tags=[],user=None,score=0,ownFavorite=False,tagCount=0,favoriteCount=0,commentCount=0,noteCount=0,pools=[])
 if FIXTURE['rich'] and i==1000:row.update(canvasWidth=None,canvasHeight=None,tags=[dict(names=['fixture-artist'],category='artist',usages=1,pinned=None)],pools=[dict(id=8515,names=['Synthetic pool'],postCount=160)],source='https://example.test/source',taggingSource='fixture-tagger',taggingCompletedAt='2026-09-01T00:00:00Z',taggingTagsWritten=['fixture-artist'])
 if FIXTURE['large']:row.update(canvasWidth=1600,canvasHeight=1200)
 return row
def response(url):
 u=urlparse(url);p=u.path;q=parse_qs(u.query); query=q.get('query',[''])[0]
 if p=='/api/tag-categories': return []
 if p=='/api/posts':
  offset=int(q.get('offset',['0'])[0]);limit=int(q.get('limit',['40'])[0]); total=1 if query=='new' else 160; base=(9000 if query=='new' else 1000)+(10000 if q.get('sort')==['updated'] else 0)
  return dict(results=[post(base+i) for i in range(offset,min(total,offset+limit))],total=total,offset=offset,limit=limit,query=query)
 if p=='/api/pools':
  offset=int(q.get('offset',['0'])[0]);limit=int(q.get('limit',['40'])[0]);total=600 if query.startswith('history') else 0
  base=8000 if query=='history2' else 5000
  return dict(results=[dict(id=base+i-FIXTURE['pool_shift'],name=f'Pool {base+i-FIXTURE["pool_shift"]}',postCount=160,thumbnailUrl=f'/fixture/{base+i}.svg',sourceKinds=[]) for i in range(offset,min(total,offset+limit))],total=total,offset=offset,limit=limit,query=query)
 if p.startswith(('/api/pool/','/api/pools/')):return dict(id=int(p.split('/')[3]),name='Synthetic pool',postCount=160,posts=[])
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
 api=ThreadingHTTPServer(('127.0.0.1',5180),Handler);threading.Thread(target=api.serve_forever,daemon=True).start()
 env=dict(os.environ,INTERNAL_API_URL='http://127.0.0.1:5180',PUBLIC_API_URL='',KURA_SESSION_SECRET=SECRET)
 subprocess.run(['node','scripts/gen-build-info.cjs'],cwd=ROOT/'web',env=env,check=True)
 log=open('/tmp/kura-ui-regressions-vite.log','w')
 vite=subprocess.Popen(['node','--input-type=module','-e',"import {createServer} from 'vite'; const s=await createServer({server:{host:'127.0.0.1',port:5179,strictPort:true,fs:{allow:ALLOW_PATHS}}});await s.listen();".replace("ALLOW_PATHS",json.dumps([str(ROOT),str((ROOT/"web/node_modules").resolve().parent)]))],cwd=ROOT/'web',env=env,stdout=log,stderr=log)
 results={};pending=[];counts={};mode={'hold_old':False,'hold_tags':False,'hold_annotation':False,'hold_pools':False,'mutation':None,'hold_pair':False}; violations=[]
 try:
  import urllib.request
  for _ in range(100):
   if vite.poll() is not None:raise RuntimeError('Vite exited; see /tmp/kura-ui-regressions-vite.log')
   try:urllib.request.urlopen(ORIGIN+'/login',timeout=1);break
   except Exception:time.sleep(.2)
  with sync_playwright() as p:
   browser=p.chromium.launch(channel=os.environ.get('KURA_UI_BROWSER_CHANNEL') or None,args=['--no-sandbox','--disable-gpu','--disable-dev-shm-usage'])
   ctx=browser.new_context(viewport={'width':1400,'height':1000})
   enc=lambda b:base64.urlsafe_b64encode(b).rstrip(b'=').decode()
   payload=enc(json.dumps({'u':'synthetic','t':'synthetic','e':'','exp':int(time.time())+3600}).encode());sig=enc(hmac.new(SECRET.encode(),payload.encode(),hashlib.sha256).digest())
   ctx.add_cookies([dict(name='kura_session',value=payload+'.'+sig,url=ORIGIN)])
   def intercept(route):
    req=route.request;u=urlparse(req.url)
    if u.hostname=='127.0.0.1' and mode['mutation']==(req.method,u.path):pending.append(route);return
    if req.method not in ('GET','HEAD'):
     violations.append(req.method+' '+u.path);route.abort();return
    if u.hostname!='127.0.0.1':violations.append('external '+u.hostname);route.abort();return
    if u.path.startswith('/api/'):
     counts[u.path]=counts.get(u.path,0)+1;q=parse_qs(u.query)
     if mode['hold_pair'] and u.path=='/api/post/1000/censor-pair':pending.append(route);return
     if mode['hold_pools'] and u.path=='/api/pools':pending.append(route);return
     if mode['hold_annotation'] and u.path=='/api/post/1000/censor-annotations':
      pending.append(route);return
     if mode['hold_old'] and u.path=='/api/posts' and q.get('query')==['old'] and q.get('offset')==['40']:
      pending.append(route);return
     if mode['hold_tags'] and u.path=='/api/tags' and q.get('query')==['older']:
      pending.append(route);return
     route.fulfill(json=response(req.url));return
    if u.path.startswith('/fixture/') or req.resource_type in ('image','media'):
     route.fulfill(body=SVG.replace('width="80" height="80"','width="1600" height="1200"') if FIXTURE['large'] else SVG,content_type='image/svg+xml');return
    route.continue_()
   ctx.route('**/*',intercept);page=ctx.new_page();page.set_default_timeout(7000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   def go(path):page.goto(ORIGIN+path,wait_until='domcontentloaded');page.wait_for_timeout(900)
   def test(name,fn):
    if os.environ.get('KURA_UI_CASES') and name not in os.environ['KURA_UI_CASES'].split(','):return
    try:results[name]=fn()
    except Exception as e:results[name]={'pass_':False,'error':str(e)[:600]}
    finally:
     mode['hold_pools']=False;mode['hold_pair']=False;mode['mutation']=None;FIXTURE['pool_shift']=0;FIXTURE['rich']=False;FIXTURE['large']=False
     for r in pending[:]:
      try:r.fulfill(json=response(r.request.url))
      except Exception:pass
      pending.remove(r)
    print(json.dumps({'case':name,**results[name]}),flush=True)
   def empty():
    counts.clear();go('/?mode=pools&q=none');page.wait_for_timeout(1000);a=counts.get('/api/pools',0);page.wait_for_timeout(800);b=counts.get('/api/pools',0)
    return dict(pass_=0<b<=2,requests_at_1900ms=a,requests_at_2700ms=b)
   test('empty_pool_bounded',empty)
   def cols():
    page.evaluate("localStorage.setItem('kura_cols','2')");go('/?q=new')
    got=page.evaluate("localStorage.getItem('kura_cols')");return dict(pass_=got=='2',actual=got)
   test('two_columns_persist',cols)
   def stale():
    go('/?mode=pools&q=none');mode['hold_tags']=True;inp=page.locator('header input[type=search]');inp.fill('older');page.wait_for_timeout(350)
    held=len(pending);inp.fill('newer');page.wait_for_timeout(400);before=page.locator('.tsi-sug').all_text_contents()
    for r in pending[:]:r.fulfill(json=response(r.request.url));pending.remove(r)
    page.wait_for_timeout(200);after=page.locator('.tsi-sug').all_text_contents();mode['hold_tags']=False
    return dict(pass_=held>0 and bool(before) and bool(after) and all('older' not in x for x in after),held_requests=held,before=before,after=after)
   test('autocomplete_latest_only',stale)
   def ime():
    inp=page.locator('header input[type=search]');inp.fill('compose');page.wait_for_timeout(350);inp.press('ArrowDown');before=inp.input_value()
    inp.evaluate("el=>el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',isComposing:true,bubbles:true,cancelable:true}))")
    after=inp.input_value();return dict(pass_=before==after,before=before,after=after)
   test('ime_enter_does_not_commit',ime)
   def oldquery():
    page.evaluate("localStorage.setItem('kura_cols','10')");mode['hold_old']=True;go('/?q=old');page.wait_for_timeout(800)
    held=len(pending);inp=page.locator('header input[type=search]');inp.fill('new');inp.press('Enter');page.wait_for_timeout(600)
    page.evaluate("window.auditTotals=[];new MutationObserver(()=>window.auditTotals.push(document.querySelector('.total-count')?.textContent)).observe(document.querySelector('.total-count'),{childList:true,subtree:true,characterData:true})")
    for r in pending[:]:
     try:r.fulfill(json=response(r.request.url))
     except Exception:pass
     pending.remove(r)
    mode['hold_old']=False;page.wait_for_timeout(400)
    page.locator('.vscroll').evaluate('(el)=>el.scrollTop=1200');page.wait_for_timeout(300)
    ids=page.locator('main img').evaluate_all('(els)=>els.map(e=>e.alt)');total=page.locator('.total-count').all_text_contents()
    observed=page.evaluate('window.auditTotals');normalize=lambda values:[re.sub(r'[^0-9]', '', x or '') for x in values];return dict(pass_=held>0 and '160' not in normalize(observed) and normalize(total)==['1'] and not any(x.startswith('#10') for x in ids),held_requests=held,total=total,transient_totals=observed,ids=ids[:10])
   test('old_query_response_rejected',oldquery)
   def mobile():
    page.set_viewport_size({'width':390,'height':844});go('/post/1000?q=old');page.locator('button.gear-btn').click();page.locator('.menu-cols button').filter(has_text='3').click();page.wait_for_timeout(500)
    return dict(pass_=urlparse(page.url).path=='/',actual_path=urlparse(page.url).path)
   test('mobile_viewer_column_returns_grid',mobile)
   def status():
    counts.clear();go('/post/1000?q=old&mode=manga');page.wait_for_timeout(600)
    first={k:counts.get(k,0) for k in ['/api/censor/status','/api/variant/status']};page.wait_for_timeout(800)
    last={k:counts.get(k,0) for k in first}
    return dict(pass_=all(0<v<=4 for v in last.values()) and first==last,first=first,last=last)
   test('viewer_empty_status_bounded',status)
   def sort_updated():
    page.get_by_role('button',name='絞り込み',exact=True).click()
    page.locator('header .menu-sort').get_by_role('button',name=re.compile('更新で並び替え')).click()
    page.get_by_role('button',name='絞り込み',exact=True).click()
   def updated_selected():
    page.get_by_role('button',name='絞り込み',exact=True).click()
    selected=page.locator('header .menu-sort button[aria-pressed="true"]').filter(has_text='更新').count()==1
    page.get_by_role('button',name='絞り込み',exact=True).click()
    return selected
   def sorting():
    page.set_viewport_size({'width':1400,'height':1000});go('/?q=old');sort_updated();page.wait_for_timeout(500)
    inp=page.locator('header input[type=search]');inp.fill('new');inp.press('Enter');page.wait_for_timeout(700)
    ids=page.locator('main img').evaluate_all('(els)=>els.map(e=>e.alt)')
    return dict(pass_=ids==['#19000'],ids=ids)
   test('custom_sort_survives_search_consistently',sorting)
   def annotation():
    # A censor-annotation response for post 1000 is held until AFTER the
    # lightbox has advanced to 1001. The late response must not paint 1000's
    # rectangles onto 1001, and moving to 1001 must issue its own request
    # rather than being suppressed by the outstanding one.
    go('/?q=old');page.evaluate("localStorage.setItem('kura_show_regions','1')");mode['hold_annotation']=True;counts.clear();go('/?q=old&lb=1000');page.wait_for_timeout(500)
    held=len(pending);before=page.locator('.lb-media').get_attribute('alt');page.keyboard.press('ArrowRight');page.wait_for_timeout(150)
    after=page.locator('.lb-media').get_attribute('alt')
    for r in pending[:]:
     r.fulfill(json={'results':[{'id':111,'shapeType':'rect','censorType':'mosaic','geometry':{'x':.1,'y':.1,'w':.25,'h':.25}}]});pending.remove(r)
    page.wait_for_timeout(350);rects=page.locator('.lb-img-wrap svg.cr rect').count();new_requests=counts.get('/api/post/1001/censor-annotations',0);mode['hold_annotation']=False
    return dict(pass_=held>0 and before=='#1000' and after=='#1001' and rects==0 and new_requests>0,held=held,before=before,after=after,old_rects_on_new_post=rects,new_post_annotation_requests=new_requests)
   test('censor_regions_stale_response',annotation)
   def reverse_completion():
    # Post ids come back. A -> B -> A issues a SECOND request for A while the
    # first is still in flight, so "is this response for the post on screen?"
    # is true for BOTH of them. Answer the newest first and the stale one last:
    # the stale one must not repaint a view it no longer owns.
    page.evaluate("localStorage.setItem('kura_show_regions','1')");mode['hold_annotation']=True;counts.clear()
    go('/?q=old&lb=1000');page.wait_for_timeout(300)
    page.keyboard.press('ArrowRight');page.wait_for_timeout(250);page.keyboard.press('ArrowLeft');page.wait_for_timeout(400)
    held=[r for r in pending if '/post/1000/censor-annotations' in r.request.url]
    body=lambda x:{'results':[{'id':111,'shapeType':'rect','censorType':'mosaic','geometry':{'x':x,'y':.1,'w':.25,'h':.25}}]}
    if len(held)!=2:
     mode['hold_annotation']=False
     return dict(pass_=False,error='expected two in-flight requests for post 1000',held=len(held),paths=[urlparse(r.request.url).path for r in pending])
    held[1].fulfill(json=body(.4));pending.remove(held[1]);page.wait_for_timeout(250);newx=page.locator('.lb-img-wrap svg.cr rect').get_attribute('x')
    held[0].fulfill(json=body(.1));pending.remove(held[0]);page.wait_for_timeout(250);afterx=page.locator('.lb-img-wrap svg.cr rect').get_attribute('x')
    display=page.locator('.lb-media').get_attribute('alt');reqs=counts.get('/api/post/1000/censor-annotations',0)
    for r in pending[:]:
     try:r.fulfill(json={'results':[]})
     except Exception:pass
     pending.remove(r)
    mode['hold_annotation']=False
    # x = geometry.x * canvasWidth(80): newest .4 -> 32, stale .1 -> 8.
    return dict(pass_=display=='#1000' and newx=='32' and afterx=='32' and reqs==2,display=display,new_response_x=newx,after_old_response_x=afterx,post_1000_requests=reqs)
   test('censor_regions_reverse_completion',reverse_completion)
   def visible_anchor():
    return page.locator('.vscroll').evaluate("""el=>{
      const top=el.getBoundingClientRect().top;
      const first=[...el.querySelectorAll('[data-history-index]')].find(x=>x.getBoundingClientRect().bottom>top);
      return first?{id:Number(first.dataset.historyId),offset:first.getBoundingClientRect().top-top,scroll:el.scrollTop}:null;
    }""")
   def pool_history(mobile=False,delayed=False,changed=False,takeover=False,key=None):
    page.set_viewport_size({'width':390,'height':844} if mobile else {'width':1400,'height':1000})
    go('/login');page.evaluate("value=>localStorage.setItem('kura_cols',value)",'2' if mobile else '5')
    go('/?mode=pools&q=history');page.wait_for_selector('.pool-card')
    page.locator('.vscroll').evaluate('el=>el.scrollTop=10437');page.wait_for_timeout(600)
    before=visible_anchor();assert before and before['id']>5100,before
    # Click a fully visible card, so Playwright need not scroll it into view.
    href=page.locator('.vscroll').evaluate("""el=>[...el.querySelectorAll('.pool-card')].find(x=>{const r=x.getBoundingClientRect(),v=el.getBoundingClientRect();return r.top>=v.top&&r.bottom<=v.bottom})?.getAttribute('href')""")
    link=page.locator(f'.pool-card[href="{href}"]');target=link.get_attribute('target');assert target in (None,'_self'),target
    link.click();page.wait_for_url('**/?q=pool:*');page.wait_for_timeout(400)
    mode['hold_pools']=delayed
    if changed:FIXTURE['pool_shift']=5
    if delayed:
     with page.expect_request(lambda r:urlparse(r.url).path=='/api/pools',timeout=7000):page.go_back()
    else:page.go_back()
    page.wait_for_url('**/?mode=pools&q=history');page.wait_for_timeout(300)
    held=len(pending);during=page.locator('.vscroll').evaluate('el=>el.scrollTop')
    manual_position=None
    if takeover:
     if key:
      page.locator('.vscroll').evaluate('el=>{el.tabIndex=0;el.focus({preventScroll:true})}');page.keyboard.press(key)
     else:page.mouse.move(700,500);page.mouse.wheel(0,320)
     page.wait_for_timeout(200);manual_position=page.locator('.vscroll').evaluate('el=>el.scrollTop')
    mode['hold_pools']=False
    for route in pending[:]:route.fulfill(json=response(route.request.url));pending.remove(route)
    page.wait_for_selector('.pool-card' if takeover else f'.pool-card[data-history-id="{before["id"]}"]');page.wait_for_timeout(650);after=visible_anchor()
    page.go_forward();page.wait_for_url('**/?q=pool:*');page.wait_for_timeout(150)
    if key == 'PageDown':
     # Force the second Back's rows to arrive after the old 750ms sample.
     page.evaluate("""() => {
       const original=window.fetch;
       window.fetch=async (...args)=>{
         const response=await original(...args);
         if(String(args[0]).includes('/api/pools')) await new Promise(resolve=>setTimeout(resolve,1000));
         return response;
       };
     }""")
    page.go_back();page.wait_for_url('**/?mode=pools&q=history');page.wait_for_timeout(750)
    # Navigation completion does not mean async virtual rows have loaded.
    # Bound readiness separately; the exact ID/offset assertions below still
    # reject a wrong restored position after the rows are actually visible.
    page.wait_for_function("""() => {
      const el=document.querySelector('.vscroll');
      return el && [...el.querySelectorAll('.pool-card[data-history-index]')]
        .some(x=>x.getBoundingClientRect().bottom>el.getBoundingClientRect().top);
    }""",timeout=7000)
    again=visible_anchor()
    passed=after and again and before['id']==after['id']==again['id'] and abs(before['offset']-after['offset'])<3 and abs(before['offset']-again['offset'])<3 and (not delayed or held>0)
    if takeover:passed=bool(after and again and held>0 and manual_position>during and abs(after['scroll']-manual_position)<1 and after==again)
    return dict(pass_=bool(passed),before=before,after=after,again=again,held_requests=held,scroll_while_loading=during,manual_position=manual_position,return_fetch_delay_ms=1000 if key=='PageDown' else 0,pages=len(ctx.pages),input='Playwright same-tab link and browser Back/Forward',viewport='mobile emulation' if mobile else 'desktop')
   test('pool_history_desktop',lambda:pool_history())
   test('pool_history_mobile_delayed',lambda:pool_history(True,True))
   test('pool_history_inserted_item',lambda:pool_history(changed=True))
   test('pool_history_manual_takeover',lambda:pool_history(delayed=True,takeover=True))
   test('pool_history_keyboard_takeover',lambda:pool_history(delayed=True,takeover=True,key='ArrowDown'))
   test('pool_history_page_key_takeover',lambda:pool_history(delayed=True,takeover=True,key='PageDown'))
   def repeated_history():
    page.set_viewport_size({'width':1400,'height':1000});go('/login');page.evaluate("localStorage.setItem('kura_cols','5')")
    go('/?mode=pools&q=history');page.locator('.vscroll').evaluate('el=>el.scrollTop=10437');page.wait_for_timeout(500);first=visible_anchor()
    def search(q):
     inp=page.locator('header input[type=search]');inp.fill(q);inp.press('Enter');page.wait_for_function('q=>new URL(location.href).searchParams.get("q")===q',arg=q);page.wait_for_timeout(600)
    search('history2');newtop=page.locator('.vscroll').evaluate('el=>el.scrollTop');page.locator('.vscroll').evaluate('el=>el.scrollTop=2237');page.wait_for_timeout(400);second=visible_anchor()
    search('history');fresh=page.locator('.vscroll').evaluate('el=>el.scrollTop');page.locator('.vscroll').evaluate('el=>el.scrollTop=1237');page.wait_for_timeout(300)
    page.go_back();page.wait_for_timeout(700);back_second=visible_anchor();page.go_back();page.wait_for_timeout(700);back_first=visible_anchor()
    return dict(pass_=newtop==0 and fresh==0 and first==back_first and second==back_second,first=first,second=second,back_first=back_first,back_second=back_second,new_query_top=newtop,repeated_url_top=fresh)
   test('pool_history_separate_entries',repeated_history)
   def sorted_history():
    page.set_viewport_size({'width':1400,'height':1000});go('/login');page.evaluate("localStorage.setItem('kura_cols','5');localStorage.setItem('kura_open_mode','single')")
    go('/?q=old');sort_updated();page.wait_for_timeout(500)
    outcomes=[]
    for offset in [0,6000]:
     page.locator('.vscroll').evaluate('(el,y)=>el.scrollTop=y',offset);page.wait_for_timeout(500);before=visible_anchor()
     button=page.locator('.vscroll').evaluate("""el=>[...el.querySelectorAll('button.thumb[id]')].find(x=>{const r=x.getBoundingClientRect(),v=el.getBoundingClientRect();return r.top>=v.top&&r.bottom<=v.bottom})?.id""")
     page.locator('#'+button).click();page.wait_for_url('**/post/**');page.wait_for_timeout(200);page.go_back();page.wait_for_timeout(800);after=visible_anchor()
     ids=page.locator('.vscroll [data-history-id]').evaluate_all('es=>es.map(e=>Number(e.dataset.historyId))')
     outcomes.append(dict(before=before,after=after,only_sorted=bool(ids) and min(ids)>=11000))
    return dict(pass_=all(o['before']==o['after'] and o['only_sorted'] for o in outcomes),positions=outcomes)
   test('sorted_grid_history',sorted_history)
   go('/login');page.evaluate("localStorage.setItem('kura_cols','5')");go('/?q=old')
   page.screenshot(path='/tmp/kura-ui-synthetic-desktop.png',full_page=True)
   page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(300)
   page.screenshot(path='/tmp/kura-ui-synthetic-mobile.png',full_page=True)
   def unified_viewer():
    go('/login');page.evaluate("localStorage.setItem('kura_open_mode','lightbox');localStorage.setItem('kura_cols','5')")
    go('/?q=old');page.locator('.vscroll').evaluate('el=>el.scrollTop=3000');page.wait_for_timeout(400)
    before=visible_anchor()
    button=page.locator('.vscroll').evaluate("""el=>[...el.querySelectorAll('button.thumb[id]')].find(x=>{const r=x.getBoundingClientRect(),v=el.getBoundingClientRect();return r.top>=v.top&&r.bottom<=v.bottom})?.id""")
    page.locator('#'+button).click();page.wait_for_selector('.lb-backdrop');page.wait_for_url('**/post/**')
    page.keyboard.press('ArrowRight');page.wait_for_timeout(350);shown=page.locator('.lb-id').inner_text();url=page.url
    page.get_by_role('button',name='詳細',exact=True).click();page.wait_for_selector('.image-details');details=page.locator('.image-details').inner_text();panel=page.locator('.image-details').bounding_box();toolbar=page.locator('.lb-top').bounding_box();assert panel['y']>=toolbar['y']+toolbar['height'];page.screenshot(path='/tmp/kura-unified-details-mobile.png')
    page.keyboard.press('Escape');page.wait_for_timeout(350);after=visible_anchor();closed=page.locator('.lb-backdrop').count()==0
    page.go_forward();page.wait_for_selector('.lb-backdrop');forward=page.locator('.lb-id').inner_text()
    page.reload();page.wait_for_selector('.lb-backdrop');direct=page.locator('.lb-id').inner_text()
    page.keyboard.press('ArrowRight');page.wait_for_timeout(600);page.keyboard.press('Escape');page.wait_for_selector('.vscroll')
    return dict(pass_=before==after and closed and shown==forward==direct and shown in details and '/post/' in url and page.locator('.lb-backdrop').count()==0,before=before,after=after,shown=shown,forward=forward,direct=direct,closed=closed)
   test('unified_viewer_overlay_direct_history',unified_viewer)
   def direct_sorted_close():
    page.set_viewport_size({'width':1400,'height':1000});go('/post/11105?q=old&idx=105&sort=updated&dir=asc');page.wait_for_selector('.lb-id')
    shown=page.locator('.lb-id').inner_text();page.keyboard.press('Escape');page.wait_for_selector('#post-11105')
    ids=page.locator('.vscroll [data-history-id]').evaluate_all('es=>es.map(e=>Number(e.dataset.historyId))')
    return dict(pass_=shown=='#11105' and bool(ids) and all(i>=11000 for i in ids) and updated_selected(),shown=shown,ids=ids,url=page.url)
   test('unified_direct_sorted_close',direct_sorted_close)
   def mutation_identity(kind):
    page.set_viewport_size({'width':1400,'height':1000});go('/?q=old&lb=1000');page.wait_for_selector('.lb-id')
    mode['mutation']=('POST','/m/post/1000/favorite') if kind=='favorite' else ('PATCH','/m/post/1000')
    page.locator('.lb-fav').first.click() if kind=='favorite' else page.locator('.lb-save').click()
    page.wait_for_timeout(200);held=list(pending);assert len(held)==1,len(held)
    page.keyboard.press('ArrowRight');page.wait_for_timeout(200)
    updated=post(1000);updated.update(ownFavorite=True,version=2,tags=[dict(names=['updated-a'],category='default',usages=1)])
    held[0].fulfill(json=updated);pending.remove(held[0]);mode['mutation']=None;page.wait_for_timeout(200)
    shown=page.locator('.lb-id').inner_text();tags=page.locator('.lb-tagbar').inner_text()
    page.keyboard.press('ArrowLeft');page.wait_for_timeout(200);back=page.locator('.lb-id').inner_text()
    return dict(pass_=shown=='#1001' and 'updated-a' not in tags and back=='#1000',shown_after_response=shown,back=back)
   test('unified_favorite_response_identity',lambda:mutation_identity('favorite'))
   test('unified_tag_response_identity',lambda:mutation_identity('tags'))
   def pair_response_order():
    page.set_viewport_size({'width':1400,'height':1000});go('/login');page.evaluate("localStorage.setItem('kura_show_pair','1')")
    mode['hold_pair']=True
    with page.expect_request(lambda r:urlparse(r.url).path=='/api/post/1000/censor-pair'):
     go('/?q=old&lb=1000')
    page.wait_for_function("document.querySelector('.lb-id')?.textContent==='#1000'")
    assert len(pending)==1,dict(stage='initial_pair_request',held=len(pending),url=page.url)
    page.keyboard.press('ArrowRight');page.wait_for_function("document.querySelector('.lb-id')?.textContent==='#1001'")
    with page.expect_request(lambda r:urlparse(r.url).path=='/api/post/1000/censor-pair'):
     page.keyboard.press('ArrowLeft')
    page.wait_for_function("document.querySelector('.lb-id')?.textContent==='#1000'")
    held=list(pending);assert len(held)==2,dict(stage='return_pair_request',held=len(held),url=page.url)
    held[1].fulfill(json=dict(role='censored',partner=post(1098),confirmed=True));pending.remove(held[1]);page.wait_for_selector('.lb-pair-cell.unc img')
    newest=page.locator('.lb-pair-cell.unc img').get_attribute('alt')
    held[0].fulfill(json=dict(role='censored',partner=post(1099),confirmed=True));pending.remove(held[0]);page.wait_for_timeout(200)
    after=page.locator('.lb-pair-cell.unc img').get_attribute('alt')
    page.get_by_role('button',name='原寸',exact=True).click();page.wait_for_selector('.lb-img-wrap');assert page.locator('.lb-pair').count()==0
    page.keyboard.press('p');page.wait_for_selector('.lb-pair');assert page.get_by_role('button',name='原寸',exact=True).get_attribute('aria-pressed')=='false'
    return dict(pass_=newest==after=='#1098',newest=newest,after_old_response=after,pair_zoom_transition=True)
   test('unified_pair_response_order',pair_response_order)
   def retained_tools():
    page.set_viewport_size({'width':390,'height':844});FIXTURE['rich']=True;go('/login');page.evaluate("localStorage.setItem('kura_verify_overlay','0');localStorage.setItem('kura_verify_target','fixture-artist');localStorage.setItem('kura_show_pair','0')")
    go('/post/1000?q=pool:8515&idx=0');page.wait_for_selector('.pool-title-name');page.wait_for_selector('.viewer-target.has')
    page.keyboard.press('t');page.wait_for_selector('.vto');assert 'fixture-artist' in page.locator('.vto').inner_text()
    page.get_by_role('button',name='原寸',exact=True).click();page.wait_for_function("document.querySelector('.lb-img-wrap')?.getBoundingClientRect().width===80")
    page.get_by_role('button',name='詳細',exact=True).click();details=page.locator('.image-details');assert 'fixture-tagger' in details.inner_text();assert details.locator('a[href="https://example.test/source"]').count()==1;page.wait_for_selector('.related-images a')
    page.get_by_role('button',name='詳細',exact=True).click()
    page.locator('.lb-tagbar button[aria-label="remove"]').click();assert 'fixture-artist' not in page.locator('.vto').inner_text()
    page.keyboard.press('Escape');go('/?q=old&lb=1000');page.wait_for_selector('.lb-tagbar .tc-name')
    mode['mutation']=('PUT','/m/post/1000/tag-pin');page.locator('.lb-tagbar button[aria-label="lock"]').click();page.wait_for_timeout(150);assert len(pending)==1
    r=pending.pop();assert r.request.post_data_json['state']=='present';r.fulfill(json=dict(post_id=1000,tag='fixture-artist',pinned='present'));page.wait_for_selector('.lb-tagbar .tc-pinned')
    page.keyboard.press('ArrowRight');page.wait_for_timeout(150);page.keyboard.press('ArrowLeft');page.wait_for_selector('.lb-tagbar .tc-pinned')
    page.locator('.lb-tagbar button[aria-label="lock"]').click();page.wait_for_timeout(150);assert len(pending)==1
    r=pending.pop();assert r.request.post_data_json['state'] is None;r.fulfill(json=dict(post_id=1000,tag='fixture-artist',pinned=None));mode['mutation']=None
    page.locator('.lb-tagbar button.tc-name').click();page.wait_for_url('**/?q=fixture-artist')
    return dict(pass_=True,pool_toolbar=True,pending_overlay=True,natural_size=80,pin_roundtrip=True,tag_filter=page.url)
   test('unified_retained_single_tools',retained_tools)
   def input_ownership():
    page.set_viewport_size({'width':390,'height':844});FIXTURE['rich']=True;FIXTURE['large']=True
    go('/login');page.evaluate("localStorage.setItem('kura_show_pair','0');localStorage.setItem('kura_verify_overlay','0')");go('/?q=old&lb=1000');page.wait_for_selector('.lb-id')
    for label,selector in [('詳細','.image-details'),('タグ','.vto')]:
     button=page.get_by_role('button',name=label,exact=True);button.focus();page.keyboard.press('Space');page.wait_for_selector(selector);assert page.locator('.lb-id').inner_text()=='#1000'
     page.keyboard.press('Space');page.wait_for_selector(selector,state='hidden')
    page.get_by_role('button',name='原寸',exact=True).focus();page.keyboard.press('Space');page.wait_for_selector('.lb-backdrop.zoomed')
    cdp=ctx.new_cdp_session(page)
    def swipe(x,y):
     cdp.send('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(x=x,y=y)]))
     for n in range(1,9):
      cdp.send('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(x=x-n*20,y=y)]));page.wait_for_timeout(35)
     cdp.send('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]));page.wait_for_timeout(300)
    stage=page.locator('.lb-stage');box=stage.bounding_box();swipe(300,box['y']+100)
    pan=stage.evaluate('el=>({left:el.scrollLeft,width:el.scrollWidth,client:el.clientWidth})');assert pan['left']>0 and page.locator('.lb-id').inner_text()=='#1000',pan
    page.get_by_role('button',name='全体',exact=True).click();page.get_by_role('button',name='詳細',exact=True).click();page.wait_for_selector('.related-images a');strip=page.locator('.related-images');box=strip.bounding_box();swipe(box['x']+box['width']-20,box['y']+30)
    strip_left=strip.evaluate('el=>el.scrollLeft');assert strip_left>0 and page.locator('.lb-id').inner_text()=='#1000',strip_left
    page.get_by_role('button',name='詳細',exact=True).click();box=stage.bounding_box();swipe(300,box['y']+100);page.wait_for_function("document.querySelector('.lb-id')?.textContent==='#1001'");cdp.detach()
    return dict(pass_=True,native_space_controls=True,original_pan=pan,related_strip_scroll=strip_left,fit_swipe_next=True,input='Chromium CDP touchStart/move/end')
   test('unified_input_ownership',input_ownership)
   def viewer_safe_area_controls():
    # Simulate nonzero safe-area values in the actual stylesheet declarations;
    # a desktop browser normally reports zero. This is not physical-device evidence.
    cdp=ctx.new_cdp_session(page)
    cdp.send('Emulation.setTouchEmulationEnabled',{'enabled':True,'maxTouchPoints':1})
    cases=[]
    try:
     for width,height,inset in [(844,390,44),(390,844,0),(740,320,44)]:
      page.set_viewport_size({'width':width,'height':height});FIXTURE['rich']=True
      go('/?q=old&lb=1000');page.wait_for_selector('.lb-close')
      changed=page.evaluate("""inset=>{
       let n=0;const walk=rules=>{for(const r of rules){
        if(r.style && r.style.cssText.includes('env(safe-area-inset-right)')){
         r.style.cssText=r.style.cssText.replaceAll('env(safe-area-inset-right)',inset+'px');n++;
        }
        if(r.cssRules)walk(r.cssRules);
       }};for(const sheet of document.styleSheets)walk(sheet.cssRules);return n;
      }""",inset)
      assert changed>=3, 'safe-area declarations were not exercised'
      assert page.evaluate("matchMedia('(pointer: coarse)').matches"), 'touch emulation inactive'
      controls=page.locator('.lb-top').evaluate("""el=>{
       const close=el.querySelector('.lb-close'),c=close.getBoundingClientRect();
       return [...el.querySelectorAll('.lb-fav,.lb-detail,.lb-close')].map(e=>{
        const r=e.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
        return {label:e.textContent,x:r.x,y:r.y,w:r.width,h:r.height,hit:e===hit||e.contains(hit),
         overlap:e!==close&&r.left<c.right&&r.right>c.left&&r.top<c.bottom&&r.bottom>c.top};
       });
      }""")
      assert all(not c['overlap'] and c['hit'] and c['x']>=0 and c['x']+c['w']<=width-inset and c['h']>=44 for c in controls),controls
      page.locator('.lb-close').click();page.wait_for_selector('.lb-backdrop',state='hidden')
      cases.append(dict(width=width,height=height,simulated_right_inset=inset,controls=controls))
    finally:
     cdp.send('Emulation.setTouchEmulationEnabled',{'enabled':False});cdp.detach()
    return dict(pass_=True,cases=cases,physical_device='not_performed')
   test('viewer_safe_area_controls',viewer_safe_area_controls)

   results['page_errors']=errors[:15];results['forbidden_requests']=violations;browser.close()
 finally:
  vite.terminate();vite.wait(timeout=10);api.shutdown();log.close()
 results['source_root']=str(ROOT);print(json.dumps(results,ensure_ascii=False,indent=2));Path('/tmp/kura-ui-regressions-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
 failed=bool(results.get('page_errors') or results.get('forbidden_requests')) or any(isinstance(v,dict) and not v.get('pass_',False) for v in results.values())
 raise SystemExit(1 if failed else 0)
if __name__=='__main__':run()
