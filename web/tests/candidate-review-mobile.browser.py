"""Built markup with synthetic tall PNG; no archive media or live judgment writes."""
import importlib.util,json,hashlib,struct,zlib
from pathlib import Path
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('compact-tag-filter.browser.py'));f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
def png(w=400,h=2200):
 def chunk(t,b):return struct.pack('>I',len(b))+t+b+struct.pack('>I',zlib.crc32(t+b)&0xffffffff)
 rows=b''.join(b'\0'+bytes((40+(y//150%3)*65,100,170))*w for y in range(h))
 return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b'')
PNG=png();PNG2=png(600,900);SHA=hashlib.sha256(PNG).hexdigest();SHA2=hashlib.sha256(PNG2).hexdigest()
def exercise(context,base,width,phone,evidence):
 p=context.new_page();states={};commands=[];errors=[];ref='a'*64;fail=False
 p.on('pageerror',lambda e:errors.append(str(e)));p.route('**/m/client-timing',lambda r:r.fulfill(status=204))
 def route(r):
  nonlocal fail
  parts=r.request.url.split('/tagger-review/')[1].split('?')[0].split('/')
  if r.request.method=='POST':
   data=r.request.post_data_json;commands.append(data);pid=parts[2]
   if fail:fail=False;return r.fulfill(status=503,json={})
   states[pid]=data['decision'];return r.fulfill(json={'outcome':'succeeded','event_id':len(commands)})
  if len(parts)==1:return r.fulfill(json=dict(queue_sha256=ref,tag='synthetic_detail',purpose='candidate_evaluation',post_ids=[101,102],decisions=states,remaining=2-len(states),quality_granted=False))
  pid=int(parts[2]);return r.fulfill(json=dict(context=dict(queue_sha256=ref,post_id=pid,tag_present=True,tag_id=1,pin={'state':'present'},display_sha256=SHA if pid==101 else SHA2),context_sha256='b'*64,previous_judgment=dict(decision=states[str(pid)],actor_name='Synthetic reviewer',received_at='2026-11-16T00:00:00Z') if str(pid) in states else None,content_url=f'/api/post/{pid}/content'))
 p.route('**/m/tagger-review/**',route);p.route('**/m/post/*/content',lambda r:r.fulfill(body=PNG if '/101/' in r.request.url else PNG2,content_type='image/png'))
 p.goto(base+'/tagger-review/'+ref,wait_until='networkidle')
 buttons=p.locator('.actions button');expect(buttons).to_have_count(3);expect(buttons.first).to_be_enabled()
 expect(p.locator('.review-notes')).not_to_have_attribute('open','')
 # No real archive imagery: intentionally tall colored chart, hash checked by actual UI.
 image=p.locator('.image-stage img');expect(image).to_be_visible();assert image.evaluate('e=>e.naturalWidth===400&&e.naturalHeight===2200')
 bar=p.locator('.decision-bar');barbox=bar.bounding_box();viewport=p.viewport_size
 if width<=600:
  assert barbox['y']>=0 and barbox['y']+barbox['height']<=viewport['height']+.5,barbox
  ib=image.bounding_box();assert ib['y']<barbox['y'] and ib['y']+ib['height']<=barbox['y']+1,(ib,barbox)
 for b in buttons.all():
  box=b.bounding_box();assert box['height']>=44 and box['width']>=80,box
  assert b.evaluate("e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}")
 assert p.evaluate('document.documentElement.scrollWidth<=innerWidth')
 if width==844:
  p.mouse.move(width/2,130);p.mouse.wheel(0,250);p.wait_for_timeout(150)
 if evidence:p.screenshot(path=str(evidence/f'initial-{width}.png'))
 # Caveats remain reachable via keyboard and may be collapsed without changing the answer.
 summary=p.locator('.review-notes summary');summary.focus();p.keyboard.press('Enter');expect(p.get_by_text('現在のタグが見えるため盲検評価ではありません。',exact=False)).to_be_visible()
 expect(p.get_by_text('学習から自動除外されるわけではありません。',exact=False)).to_be_visible()
 p.keyboard.press('Enter');assert not commands
 uncertain=p.get_by_role('button',name='判別できない — 今回は未確定');uncertain.focus();assert uncertain.evaluate('e=>e===document.activeElement');assert uncertain.evaluate('e=>getComputedStyle(e).outlineStyle')!='none'
 fail=True;p.mouse.move(width/2,100);p.mouse.wheel(0,2000);p.wait_for_timeout(150)
 p.keyboard.press('Enter');expect(p.get_by_role('alert')).to_be_visible();alert=p.get_by_role('alert').bounding_box();assert alert['y']>=0 and alert['y']+alert['height']<=viewport['height'],alert
 if evidence:p.screenshot(path=str(evidence/f'save-error-{width}.png'))
 failed=commands[-1];uncertain.click();assert commands[-1]==failed
 expect(p.get_by_text('今回の回答 1 / 2',exact=False)).to_be_visible();expect(uncertain).to_have_attribute('aria-pressed','true')
 assert commands[-1]['decision']=='uncertain' and set(commands[-1])=={'request_id','decision','expected_context_sha256'}
 expect(p.locator('#review-response-status')).to_be_focused()
 focused=p.locator('#review-response-status').bounding_box();assert focused['y']>=0 and focused['y']+focused['height']<=viewport['height'],focused
 if evidence:p.screenshot(path=str(evidence/f'uncertain-{width}.png'))
 p.get_by_role('button',name='次へ',exact=True).click();expect(buttons.first).to_be_enabled()
 assert image.evaluate('e=>e.naturalWidth===600&&e.naturalHeight===900')
 if evidence:p.screenshot(path=str(evidence/f'regular-{width}.png'))
 buttons.first.click()
 expect(p.get_by_text('すべて回答しました。',exact=False)).to_be_visible();expect(p.get_by_text('判別できない 1 件',exact=False)).to_be_visible()
 assert states=={'101':'uncertain','102':'present'}
 p.reload(wait_until='networkidle');expect(p.get_by_text('今回の回答 2 / 2',exact=False)).to_be_visible()
 # Last nonbar content can scroll above fixed controls, not permanently covered.
 previous=p.locator('.previous');p.mouse.move(width/2,100);p.mouse.wheel(0,2000);p.wait_for_timeout(150);box=previous.bounding_box();bb=bar.bounding_box()
 if width<=600:assert box['y']+box['height']<=bb['y']+1,(box,bb)
 if evidence:p.screenshot(path=str(evidence/f'completed-{width}.png'))
 assert not errors,errors;p.close();return dict(width=width,tall_image=[400,2200],choices_visible=True,uncertain_persistent=True,keyboard_focus=True,completed_not_quality=True,horizontal_overflow=False,live_writes=0)
f.exercise=exercise
if __name__=='__main__':f.main()
