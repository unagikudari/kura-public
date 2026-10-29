"""Ordinary folder drops, persistent results and selective retries. Synthetic writes only."""
import importlib.util
from pathlib import Path
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('compact-tag-filter.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
def drop(page,names,folder=False):
 page.evaluate('''({names,folder})=>{
  const files=names.map(name=>new File(['synthetic'],name,{type:name.endsWith('.txt')?'text/plain':''}));
  const leaf=file=>({name:file.name,isFile:true,isDirectory:false,file:resolve=>resolve(file)});
  const root={name:'Book',isFile:false,isDirectory:true,createReader:()=>{let read=false;return {readEntries:resolve=>{resolve(read?[]:files.map(leaf));read=true;}}}};
  const event=new DragEvent('drop',{bubbles:true,cancelable:true});
  Object.defineProperty(event,'dataTransfer',{value:{types:['Files'],files,items:folder?[{kind:'file',webkitGetAsEntry:()=>root,getAsFile:()=>null}]:[]}});
  window.dispatchEvent(event);
 }''',{'names':names,'folder':folder})
def exercise(context,base,width,phone,evidence):
 page=context.new_page();calls=[];recovered={'value':False};held=[];reloads=[]
 page.on('request',lambda r:reloads.append(r.url) if r.method=='GET' and '/posts?localize=true' in r.url else None)
 def upload(route):
  body=route.request.post_data_buffer or b''
  name=next(n for n in ['1.jpg','2.png','3.gif','4.webm','10.jpg','hold.jpg','extra.jpg','later.jpg'] if f'filename="{n}"'.encode() in body)
  calls.append(name)
  if name=='hold.jpg':held.append(route);return
  if name=='2.png':return route.fulfill(status=409,json={'detail':'すでに登録済みです (checksum重複)'})
  if name=='3.gif' and not recovered['value']:return route.fulfill(status=400,json={'detail':'synthetic unsupported media'})
  if name=='4.webm' and calls.count(name)==1:return route.fulfill(status=503,json={'detail':'synthetic unavailable'})
  route.fulfill(json={'id':1000})
 page.route('**/m/posts?*',upload)
 page.goto(base+'/?q=',wait_until='networkidle')
 page.evaluate("window.dispatchEvent(new DragEvent('dragstart'))");drop(page,['extra.jpg']);assert not calls
 drop(page,['10.jpg','2.png','1.jpg','3.gif','4.webm','notes.txt'],True)
 panel=page.get_by_role('region',name='ファイル取り込み')
 expect(panel.get_by_text('取り込みを中断しています',exact=True)).to_be_visible()
 assert calls==['1.jpg','2.png','3.gif','4.webm'],calls
 for text in ['追加 1','登録済み 1','対象外 1']:expect(panel.locator('.counts')).to_contain_text(text)
 # the list behind the panel reloads its data once something was added
 page.wait_for_timeout(300);assert reloads,'no list reload after an import that added a post'
 panel.get_by_role('button',name='失敗したファイル').click();expect(panel.locator('li').first).to_contain_text('Book/3.gif')
 page.wait_for_timeout(6100);expect(panel).to_be_visible()
 page.evaluate("()=>{const a=document.createElement('a');a.href='/?q=other';document.body.append(a);a.click();a.remove()}")
 page.wait_for_url('**/?q=other');expect(panel).to_be_visible();assert len(calls)==4
 panel.get_by_role('button',name='未完了分を続ける',exact=True).click()
 expect(panel.get_by_role('button',name='失敗分だけ再試行',exact=True)).to_be_visible()
 assert calls==['1.jpg','2.png','3.gif','4.webm','3.gif','4.webm','10.jpg'],calls
 recovered['value']=True;panel.get_by_role('button',name='失敗分だけ再試行',exact=True).click()
 expect(panel.get_by_text('取り込み完了',exact=True)).to_be_visible()
 assert calls.count('1.jpg')==calls.count('2.png')==1 and calls.count('3.gif')==3
 expect(panel.locator('.counts')).to_contain_text('追加 4');expect(panel.locator('.counts')).to_contain_text('登録済み 1')
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 if evidence:page.screenshot(path=str(evidence/f'drop-import-{width}.png'))
 panel.get_by_role('button',name='取り込み結果を閉じる').click();expect(panel).to_have_count(0)
 drop(page,['hold.jpg','later.jpg']);expect(panel).to_be_visible();page.wait_for_timeout(100)
 assert len(held)==1
 drop(page,['extra.jpg']);expect(panel.get_by_role('alert')).to_be_visible();assert 'extra.jpg' not in calls
 panel.get_by_role('button',name='一時停止',exact=True).click();held.pop().fulfill(json={'id':1000})
 expect(panel.get_by_text('取り込みを中断しています',exact=True)).to_be_visible();assert 'later.jpg' not in calls
 assert page.evaluate("()=>{const e=new Event('beforeunload',{cancelable:true});window.dispatchEvent(e);return e.defaultPrevented}")
 panel.get_by_role('button',name='未完了分を続ける',exact=True).click();expect(panel.get_by_text('取り込み完了',exact=True)).to_be_visible();assert calls.count('later.jpg')==1
 panel.get_by_role('button',name='取り込み結果を閉じる').click()
 # the reload happens where the reader is: a scrolled list keeps its position,
 # and a deep-linked one (idx/scroll_to) keeps loading as it is scrolled
 for address,scroll in (('/?q=',2500),('/?q=&idx=120&scroll_to=1120',None)):
  page.goto(base+address,wait_until='networkidle')
  vs=page.locator('.vscroll');expect(vs).to_have_attribute('aria-busy','false')
  if scroll:vs.evaluate(f'e=>e.scrollTop={scroll}');page.wait_for_timeout(400)
  at=vs.evaluate('e=>e.scrollTop');before=len(reloads)
  drop(page,['later.jpg']);expect(panel.get_by_text('取り込み完了',exact=True)).to_be_visible()
  page.wait_for_timeout(800);assert len(reloads)>before,address
  expect(vs).to_have_attribute('aria-busy','false')
  assert abs(vs.evaluate('e=>e.scrollTop')-at)<2,(address,at,vs.evaluate('e=>e.scrollTop'))
  vs.evaluate('e=>e.scrollTop=e.scrollHeight');page.wait_for_timeout(800)
  assert page.locator('[id^=post-]').count()>0,address
  panel.get_by_role('button',name='取り込み結果を閉じる').click()
 page.close();return dict(width=width,list_reloads_after_add=True,folder=True,natural_order=True,duplicate_not_failure=True,selective_retry=True,persistent_on_navigation=True,concurrent_drop_guard=True)
f.exercise=exercise
if __name__=='__main__':f.main()
