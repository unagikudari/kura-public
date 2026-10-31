#!/usr/bin/env python3
"""Synthetic GET-only browser regression probe; no real data, keys, or mutations.

Run from repository root with a Python environment containing Playwright:
  python web/tests/mobile-regressions.py
Requires installed web/node_modules and Playwright Chromium.
Set KURA_UI_BROWSER_CHANNEL=chrome to use installed Google Chrome instead.
Uses loopback ports 5183 (isolated Vite) and 5184 (synthetic GET-only API).
Outputs evidence to /tmp/kura-mobile-regressions-results.json; exits nonzero on failure.
"""
import base64, hashlib, hmac, json, os, re, subprocess, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from playwright.sync_api import sync_playwright
HERE=Path(__file__).resolve()
DEFAULT_ROOT=HERE.parents[2] if HERE.parent.name=='tests' else Path.cwd()
ROOT=Path(os.environ.get('KURA_UI_WORKTREE',DEFAULT_ROOT)).resolve()
ORIGIN='http://127.0.0.1:5183'; SECRET='synthetic-browser-audit-session-only'
SVG='<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="#456"/></svg>'
IDENTITY_TAGS=[]
def post(i):
 tags=[dict(t) for t in IDENTITY_TAGS]
 if tags and i==1001:tags[0]={**tags[0],'names':['artist_next'],'localizedNames':{'ja':'次の作家'}}
 return dict(id=i,version=1,creationTime=None,lastEditTime=None,safety='unsafe' if IDENTITY_TAGS and i==1000 else 'safe',source=None,taggingSource=None,taggingCompletedAt=None,taggingTagsWritten=None,originalFilename=f'synthetic-{i}',type='document' if IDENTITY_TAGS and i==1001 else 'image',mimeType='image/svg+xml',fileSize=100,canvasWidth=80,canvasHeight=80,contentUrl=f'/fixture/{i}.svg',thumbnailUrl=f'/fixture/{i}.svg',flags=[],tags=tags,user=None,score=0,ownFavorite=False,tagCount=0,favoriteCount=0,commentCount=0,noteCount=0,pools=[])
def response(url):
 u=urlparse(url);p=u.path;q=parse_qs(u.query); query=q.get('query',[''])[0]
 if p=='/api/tag-categories': return [{'name':'default','color':'#aaccdd','default':True}]
 if p=='/api/posts':
  offset=int(q.get('offset',['0'])[0]);limit=int(q.get('limit',['40'])[0]); total=1 if query=='new' else 160; base=(9000 if query=='new' else 1000)+(10000 if q.get('sort')==['updated'] else 0)
  return dict(results=[post(base+i) for i in range(offset,min(total,offset+limit))],total=total,offset=offset,limit=limit,query=query)
 if p=='/api/pools':return dict(results=[],total=0,offset=0,limit=40,query=query)
 if p.endswith('/index'):return {'idx':0}
 if p.startswith('/api/post/'):return post(int(p.split('/')[3]))
 if p=='/api/tags':
  tags=[dict(id=1,version=1,names=[query+'-result' if query else 'fixture'],category='default',categoryColor='#aaccdd',usages=1,description=None,creationTime=None,lastEditTime=None)]
  return dict(results=tags,total=1,offset=0,limit=60,query=query) if 'category' in q or 'offset' in q else tags
 if p.endswith('/status'):return {'results':{}}
 return {}
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  body=json.dumps(response(self.path)).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
 def do_POST(self):self.send_error(405)
 do_PUT=do_PATCH=do_DELETE=do_POST

def run():
 api=ThreadingHTTPServer(('127.0.0.1',5184),Handler);threading.Thread(target=api.serve_forever,daemon=True).start()
 env=dict(os.environ,INTERNAL_API_URL='http://127.0.0.1:5184',PUBLIC_API_URL='http://127.0.0.1:5184',KURA_SESSION_SECRET=SECRET)
 subprocess.run(['node','scripts/gen-build-info.cjs'],cwd=ROOT/'web',env=env,check=True)
 log=open('/tmp/kura-mobile-regressions-vite.log','w')
 vite=subprocess.Popen(['node','--input-type=module','-e',"import {createServer} from 'vite'; const s=await createServer({server:{host:'127.0.0.1',port:5183,strictPort:true,fs:{allow:ALLOW_PATHS}}});await s.listen();".replace("ALLOW_PATHS",json.dumps([str(ROOT),str((ROOT/"web/node_modules").resolve().parent)]))],cwd=ROOT/'web',env=env,stdout=log,stderr=log)
 results={};pending=[];counts={};mode={'hold_old':False,'hold_tags':False}; violations=[]
 try:
  import urllib.request
  for _ in range(100):
   if vite.poll() is not None:raise RuntimeError('Vite exited; see /tmp/kura-mobile-regressions-vite.log')
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
    if req.method not in ('GET','HEAD'):
     violations.append(req.method+' '+u.path);route.abort();return
    if u.hostname!='127.0.0.1':violations.append('external '+u.hostname);route.abort();return
    if u.path.startswith('/api/'):
     counts[u.path]=counts.get(u.path,0)+1;q=parse_qs(u.query)
     if mode['hold_old'] and u.path=='/api/posts' and q.get('query')==['old'] and q.get('offset')==['40']:
      pending.append(route);return
     if mode['hold_tags'] and u.path=='/api/tags' and q.get('query')==['older']:
      pending.append(route);return
     route.fulfill(json=response(req.url),headers={'Access-Control-Allow-Origin':'*'});return
    if u.path.startswith('/fixture/') or req.resource_type in ('image','media'):
     route.fulfill(body=SVG,content_type='image/svg+xml');return
    route.continue_()
   ctx.route('**/*',intercept);page=ctx.new_page();page.set_default_timeout(7000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   def go(path):page.goto(ORIGIN+path,wait_until='domcontentloaded');page.wait_for_timeout(900)
   def test(name,fn):
    if os.environ.get('KURA_MOBILE_CASES') and name not in os.environ['KURA_MOBILE_CASES'].split(','):return
    try:results[name]=fn()
    except Exception as e:results[name]={'error':str(e)[:400]}
   def mobile_tags():
    out=[]
    for width in (320,390):
     page.set_viewport_size({'width':width,'height':844});go('/tags');page.wait_for_timeout(1000)
     metrics=page.locator('iframe.gallery').evaluate('(el)=>({right:el.getBoundingClientRect().right,width:el.getBoundingClientRect().width,height:el.getBoundingClientRect().height,viewport:innerWidth})')
     toggle=page.locator('.filters-toggle');exists=toggle.count()>0
     if exists:
      toggle.click();page.wait_for_timeout(100);collapsed=page.locator('iframe.gallery').bounding_box()['height'];toggle.click();page.wait_for_timeout(100);expanded=page.locator('iframe.gallery').bounding_box()['height']
     else:collapsed=expanded=0
     out.append(dict(width=width,metrics=metrics,collapse_control=exists,collapsed_height=collapsed,expanded_height=expanded))
    page.screenshot(path='/tmp/kura-mobile-tags-new.png',full_page=True)
    return dict(pass_=all(x['metrics']['right']<=x['width'] and x['metrics']['width']>=x['width']-2 and x['metrics']['height']>=200 and x['collapsed_height']>x['expanded_height'] for x in out),cases=out)
   test('mobile_tags_bounds_and_collapse',mobile_tags)
   def short_tags():
    cases=[]
    for width,height in [(390,320),(360,420)]:
     page.set_viewport_size({'width':width,'height':height});go('/tags')
     body=page.locator('.filter-body');body.evaluate('(el)=>el.scrollTop=el.scrollHeight');page.wait_for_timeout(100)
     pill=page.locator('.tc-pill').filter(has_text='fixture').first
     bounds=pill.bounding_box();panel=body.bounding_box();gallery=page.locator('iframe.gallery').bounding_box()
     visible=bounds['y']>=panel['y'] and bounds['y']+bounds['height']<=panel['y']+panel['height']+1
     pill.click();selected=parse_qs(urlparse(page.locator('iframe.gallery').get_attribute('src')).query).get('q')
     cases.append(dict(width=width,height=height,tag_reachable=visible,selected=selected,panel_bottom=panel['y']+panel['height'],gallery_top=gallery['y'],pass_=visible and selected==['fixture'] and panel['y']+panel['height']<=gallery['y']+1))
     page.locator('button.clear').click()
    return dict(pass_=all(c['pass_'] for c in cases),cases=cases)
   test('short_tag_panel_scrolls_without_overlap',short_tags)
   def tag_modes():
    go('/tags');page.locator('.tc-pill').filter(has_text='fixture').first.click();page.wait_for_timeout(100)
    inc=parse_qs(urlparse(page.locator('iframe.gallery').get_attribute('src')).query).get('q')
    mode_btn=page.locator('.selection-mode button').filter(has_text='除外')
    if mode_btn.count():mode_btn.click();page.locator('.tc-pill').filter(has_text='fixture').first.click()
    page.wait_for_timeout(100);exc=parse_qs(urlparse(page.locator('iframe.gallery').get_attribute('src')).query).get('q')
    return dict(pass_=inc==['fixture'] and exc==['-fixture'],include_query=inc,exclude_query=exc)
   test('touch_include_exclude',tag_modes)
   def sidebar_stale():
    mode['hold_tags']=True;inp=page.locator('input.tag-search');inp.fill('older');page.wait_for_timeout(400);held=len(pending);inp.fill('newer');page.wait_for_timeout(450)
    for r in pending[:]:r.fulfill(json=response(r.request.url),headers={'Access-Control-Allow-Origin':'*'});pending.remove(r)
    page.wait_for_timeout(100);after=page.locator('.side-scroll').inner_text()
    inp.fill('older');page.wait_for_timeout(350);held_clear=len(pending);inp.fill('')
    for r in pending[:]:r.fulfill(json=response(r.request.url),headers={'Access-Control-Allow-Origin':'*'});pending.remove(r)
    page.wait_for_timeout(100);cleared=page.locator('.side-scroll').inner_text();mode['hold_tags']=False
    return dict(pass_=held>0 and held_clear>0 and 'newer-result' in after and 'older-result' not in after and 'older-result' not in cleared,held=held,held_clear=held_clear,after=after,cleared=cleared)
   test('sidebar_latest_query_and_clear',sidebar_stale)
   def suggestion_keyboard():
    inp=page.locator('header input[type=search]');inp.fill('keyboard');page.wait_for_timeout(350);inp.press('Tab');page.wait_for_timeout(350);option=page.locator('.tsi-sug').first;focused=option.evaluate('(el)=>el===document.activeElement');option.press('Enter');page.wait_for_timeout(100)
    picked=focused and 'keyboard-result' in inp.input_value()
    inp.fill('leave');page.wait_for_timeout(350);inp.press('Tab');page.wait_for_timeout(300);page.keyboard.press('Tab');page.wait_for_timeout(300);closed=page.locator('.tsi-sug').count()==0
    return dict(pass_=picked and closed,actual=inp.input_value(),candidate_kept_focus=focused,closes_after_leaving=closed)
   test('suggestion_keyboard_activation',suggestion_keyboard)
   def filter_tokens():
    cases=[]
    for query,action,expected,checked_type,checked_fav in [
     ('type:image cat','video','cat type:video','画像のみ',False),
     ('-type:image cat','video','cat type:video',None,False),
     ('-type:image cat','all','cat',None,False),
     ('source:type:image cat','video','source:type:image cat type:video','すべて',False),
     ('fav:true cat','fav','cat','すべて',True),
     ('-fav:true cat','fav','cat fav:true','すべて',False),
     ('fav:false cat','fav','cat fav:true','すべて',False),
     ('-fav:false cat','fav','cat','すべて',True),
     ('source:fav:true cat','fav','source:fav:true cat fav:true','すべて',False),
    ]:
     from urllib.parse import quote
     go('/?q='+quote(query));page.locator('.filter-btn').click()
     radios=page.locator('.menu-filter input[type=radio]:checked').evaluate_all('(els)=>els.map(e=>e.parentElement.textContent.trim())')
     favorite=page.locator('.menu-filter input[type=checkbox]').is_checked()
     if action=='fav':page.locator('.menu-filter input[type=checkbox]').click()
     else:page.locator('.menu-filter label').filter(has_text='動画のみ' if action=='video' else 'すべて').click()
     page.wait_for_function('(q)=>new URLSearchParams(location.search).get("q")===q',arg=expected)
     actual=parse_qs(urlparse(page.url).query).get('q',[''])[0]
     cases.append(dict(input=query,after=actual,radios=radios,favorite=favorite,pass_=actual==expected and radios==([checked_type] if checked_type else []) and favorite==checked_fav))
    return dict(pass_=all(c['pass_'] for c in cases),cases=cases)
   test('filter_tokens_preserve_negation_and_other_tags',filter_tokens)
   def login():
    cases=[]
    for width in (320,390):
     page.set_viewport_size({'width':width,'height':844});go('/login');bounds=page.locator('input').evaluate_all('(els)=>els.map(e=>({left:e.getBoundingClientRect().left,right:e.getBoundingClientRect().right,labels:e.labels.length}))')
     cases.append(dict(width=width,scroll=page.evaluate('document.documentElement.scrollWidth'),inputs=bounds))
    page.screenshot(path='/tmp/kura-mobile-login-new.png',full_page=True)
    return dict(pass_=all(x['scroll']<=x['width'] and all(i['left']>=0 and i['right']<=x['width'] and i['labels']>0 for i in x['inputs']) for x in cases),cases=cases)
   test('mobile_login_bounds_labels',login)
   def landscape_login():
    page.set_viewport_size({'width':390,'height':320});go('/tags')
    page.evaluate("let a=document.createElement('a');a.href='/login';a.id='audit-login-link';a.textContent='audit login';document.body.append(a)");page.locator('#audit-login-link').click();page.wait_for_timeout(500)
    top=page.locator('.login-panel, .login-box').bounding_box()['y'];wrap=page.locator('.login-wrap');wrap.evaluate('(el)=>el.scrollTop=el.scrollHeight');page.wait_for_timeout(100)
    link=page.locator('a[href*="account/reset"]').bounding_box()
    return dict(pass_=top>=0 and link['y']>=0 and link['y']+link['height']<=320,panel_initial_top=top,reset_link_after_scroll=link)
   test('landscape_login_from_tags_scrollable',landscape_login)

   def identity_display_settings():
    global IDENTITY_TAGS
    IDENTITY_TAGS=[
     dict(id=1,names=['artist_one','artist_alias'],category='artist',usages=4,localizedNames={'ja':'作家いち'}),
     dict(id=2,names=['artist_two'],category='artist',usages=4),
     *[dict(id=10+i,names=[name,'alias_is_not_another_character'] if i==0 else [name],category='character',usages=4,localizedNames={'ja':label}) for i,(name,label) in enumerate([('alice','アリス'),('bob','ボブ'),('carol','キャロル'),('dave','デイヴ')])],
     dict(id=20,names=['series_one'],category='copyright',usages=4,localizedNames={'ja':'作品いち'}),
     dict(id=21,names=['series_two'],category='copyright',usages=4),
    ]
    page.set_viewport_size({'width':390,'height':844});go('/?q=')
    page.evaluate("localStorage.setItem('kura_cols','2')");page.reload();page.wait_for_timeout(800)
    assert page.locator('.thumb-identities').count()==0
    def open_identity_sidebar(target=page):
     target.locator('.lb-backdrop').wait_for()
     assert target.locator('.lb-tag-sidebar').count()==0
     assert not target.locator('.viewer-identities').is_visible()
     target.get_by_role('button',name='タグ',exact=True).click()
     target.locator('.lb-tag-sidebar').wait_for(state='visible')
     return target.locator('.lb-tag-sidebar .identity-summary')
    go('/post/1000?q=&idx=0')
    summary=open_identity_sidebar()
    assert 'アリス・ボブ' in summary.inner_text() and 'ほか2件' in summary.inner_text(),summary.inner_text()
    assert 'alias_is_not_another_character' not in summary.inner_text()
    page.screenshot(path='/tmp/kura-identity-mobile-sidebar.png')
    url=page.url;summary.click();dialog=page.locator('dialog.identity-dialog');dialog.wait_for(state='visible')
    assert dialog.locator('section[aria-label="キャラクター"] li').count()==4
    assert 'キャロル' in dialog.inner_text() and 'デイヴ' in dialog.inner_text()
    page.screenshot(path='/tmp/kura-identity-dialog.png')
    assert 'alias_is_not_another_character' not in dialog.inner_text()
    assert page.url==url and page.locator('.lb-backdrop').count()==1
    page.keyboard.press('Escape');dialog.wait_for(state='detached')
    assert summary.evaluate('(e)=>document.activeElement===e')
    go('/settings')
    page.get_by_label('キャラクター',exact=True).uncheck()
    page.get_by_label('カテゴリごとの表示件数',exact=True).select_option('3')
    page.get_by_role('link',name='← 画像一覧',exact=True).click();page.wait_for_url('**/?q=');page.wait_for_timeout(500)
    go('/post/1000?q=&idx=0');open_identity_sidebar()
    assert page.locator('.lb-tag-sidebar .identity-summary [data-category="character"]').count()==0
    page.reload();page.wait_for_timeout(700);open_identity_sidebar()
    assert page.locator('.lb-tag-sidebar .identity-summary [data-category="character"]').count()==0
    assert page.locator('.lb-tag-sidebar .identity-summary [data-category="artist"]').count()>0
    go('/settings');assert page.get_by_label('カテゴリごとの表示件数',exact=True).input_value()=='3'
    # SSR isolation: a second browser receives default character display, not the first browser's setting.
    fresh=browser.new_context(viewport={'width':390,'height':844});fresh.add_cookies([dict(name='kura_session',value=payload+'.'+sig,url=ORIGIN)])
    fresh.route('**/*',intercept)
    fp=fresh.new_page();fp.set_default_timeout(7000);fp.goto(ORIGIN+'/post/1000?q=&idx=0',wait_until='domcontentloaded');fp.wait_for_timeout(700);open_identity_sidebar(fp);fp.locator('.lb-tag-sidebar .identity-summary [data-category="character"]').wait_for(state='attached');fresh.close()
    go('/settings');page.get_by_role('button',name='初期設定に戻す').click()
    assert page.get_by_label('サムネイル',exact=True).count()==0
    go('/?q=')
    assert page.locator('.thumb-identities .identity-summary').count()==0
    go('/post/1000?q=&idx=0');open_identity_sidebar()
    assert 'アリス・ボブ' in page.locator('.lb-tag-sidebar .identity-summary').inner_text()
    page.screenshot(path='/tmp/kura-identity-single.png')
    page.locator('.lb-tag-sidebar .identity-summary').click();page.locator('dialog.identity-dialog').wait_for(state='visible');url=page.url
    page.keyboard.press('ArrowRight');assert page.url==url
    page.keyboard.press('Escape');page.locator('dialog.identity-dialog').wait_for(state='detached');assert page.locator('.lb-backdrop').count()==1
    page.get_by_role('button',name='次',exact=True).click();page.wait_for_function("document.querySelector('.lb-tag-sidebar .identity-summary')?.textContent.includes('次の作家')")
    page.evaluate("localStorage.setItem('kura_scroll_dir','h')")
    go('/post/1000?q=&idx=0&mode=manga');page.locator('.manga-identities .identity-summary').wait_for()
    page.screenshot(path='/tmp/kura-identity-manga.png')
    page.locator('.manga-identities .identity-summary').click();page.locator('dialog.identity-dialog').wait_for(state='visible');url=page.url
    page.locator('dialog.identity-dialog').evaluate("e=>{e.tabIndex=-1;e.focus()}")
    page.keyboard.press(' ');page.keyboard.press('ArrowRight');assert page.url==url
    before=page.locator('.stream').evaluate('(e)=>e.scrollLeft')
    box=page.locator('dialog.identity-dialog li').first.bounding_box();x=box['x']+box['width']/2;y=box['y']+box['height']/2
    page.mouse.move(x,y);page.mouse.wheel(0,250);page.mouse.down();page.mouse.move(x-65,y,steps=6);page.mouse.up();page.wait_for_timeout(400)
    assert abs(page.locator('.stream').evaluate('(e)=>e.scrollLeft')-before)<2 and page.url==url
    page.keyboard.press('Escape');page.locator('dialog.identity-dialog').wait_for(state='detached');assert page.locator('.stream').count()==1
    page.evaluate('document.activeElement.blur()');page.keyboard.press('ArrowLeft');page.wait_for_function("document.querySelector('.manga-identities')?.textContent.includes('次の作家')")
    go('/settings');page.get_by_label('単画像・マンガビュー',exact=True).uncheck()
    for path in ('/post/1000?q=&idx=0','/post/1000?q=&idx=0&mode=manga'):
     go(path)
     if 'mode=manga' not in path:
      page.get_by_role('button',name='タグ',exact=True).click();page.locator('.lb-tag-sidebar').wait_for()
     assert page.locator('.lb-tag-sidebar .identity-summary,.viewer-identities .identity-summary,.manga-identities .identity-summary').count()==0
    page.set_viewport_size({'width':320,'height':740});go('/settings')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path='/tmp/kura-identity-settings-320.png')
    page.get_by_role('button',name='初期設定に戻す').click();go('/?q=&bulk=1')
    assert page.locator('.thumb-identities .identity-summary').count()==0
    go('/?q=');page.evaluate("localStorage.setItem('kura_cols','5');localStorage.setItem('kura_bulk_mode','0')");page.reload();page.wait_for_timeout(700)
    assert page.locator('.thumb-identities .identity-summary').count()==0
    cells=page.locator('.thumb-cell').evaluate_all('(els)=>els.map(e=>{const a=e.getBoundingClientRect(),b=e.querySelector(".thumb").getBoundingClientRect();return Math.abs(a.width-a.height)<1&&Math.abs(a.width-b.width)<1&&Math.abs(a.height-b.height)<1})')
    assert cells and all(cells),cells
    IDENTITY_TAGS=[]
    return dict(pass_=True,characters=4,aliases_counted=False,persistence='navigation+reload',ssr_isolation=True,viewers='single+manga',dialog_input_ownership=True,square_cells=True)
   test('identity_display_settings',identity_display_settings)

   results['page_errors']=errors[:15];results['forbidden_requests']=violations;browser.close()
 finally:
  vite.terminate();vite.wait(timeout=10);api.shutdown();log.close()
 results['source_root']=str(ROOT);print(json.dumps(results,ensure_ascii=False,indent=2));Path('/tmp/kura-mobile-regressions-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
 failed=bool(results.get('page_errors') or results.get('forbidden_requests')) or any(isinstance(v,dict) and not v.get('pass_',False) for v in results.values())
 raise SystemExit(1 if failed else 0)
if __name__=='__main__':run()
