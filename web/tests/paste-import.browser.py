"""Chrome native image clipboard plus paste guards; all uploads are synthetic."""
import importlib.util
import re
from pathlib import Path
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('compact-tag-filter.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)


def pasted(page,name='copied.png',kind='image/png',target='body'):
 return page.evaluate('''({name,kind,target})=>{
  const transfer=new DataTransfer();transfer.items.add(new File(['synthetic'],name,{type:kind}));
  const event=new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:transfer});
  document.querySelector(target).dispatchEvent(event);return event.defaultPrevented;
 }''',dict(name=name,kind=kind,target=target))


def exercise(context,base,width,phone,evidence):
 context.grant_permissions(['clipboard-read','clipboard-write'])
 page=context.new_page();calls=[];held=[];errors=[]
 page.on('pageerror',lambda e:errors.append(str(e)))
 def upload(route):
  body=route.request.post_data_buffer or b''
  name=re.search(rb'filename="([^"]+)"',body).group(1).decode();calls.append(name)
  if name=='held.png':held.append(route);return
  if len(calls)==2:return route.fulfill(status=409,json={'detail':'すでに登録済みです (checksum重複)'})
  route.fulfill(json={'id':1000})
 page.route('**/m/posts?*',upload)
 page.goto(base+'/?q=',wait_until='networkidle')
 # The browser creates a real image File from a native Ctrl+V paste, rather
 # than relying only on synthetic ClipboardEvent fixtures.
 page.evaluate('''async()=>{
  const c=document.createElement('canvas');c.width=4;c.height=4;
  c.getContext('2d').fillRect(0,0,4,4);
  const blob=await new Promise(r=>c.toBlob(r,'image/png'));
  await navigator.clipboard.write([new ClipboardItem({'image/png':blob})]);
  document.activeElement?.blur();
 }''')
 page.keyboard.press('Control+v')
 panel=page.get_by_role('region',name='ファイル取り込み')
 expect(panel.get_by_text('取り込み完了',exact=True)).to_be_visible()
 expect(panel.locator('.counts')).to_contain_text('追加 1');assert len(calls)==1
 expect(panel.get_by_role('link',name='追加した画像を開く ↗')).to_have_attribute('href','/post/1000')
 panel.get_by_role('button',name='取り込み結果を閉じる').click()
 page.keyboard.press('Control+v')
 expect(panel.get_by_text('取り込み完了',exact=True)).to_be_visible()
 expect(panel.locator('.counts')).to_contain_text('登録済み 1');assert len(calls)==2
 panel.get_by_role('button',name='取り込み結果を閉じる').click()
 # Input/editor paste is untouched, even if the clipboard contains an image.
 assert not pasted(page,target='header input[type="search"]')
 page.evaluate('''()=>{const note=document.createElement('div');note.id='paste-note';note.contentEditable='true';document.body.append(note)}''')
 assert not pasted(page,target='#paste-note')
 assert not pasted(page,name='clipboard.webm',kind='video/webm')
 page.locator('header input[type="search"]').fill('')
 page.evaluate("navigator.clipboard.writeText('normal text')")
 page.locator('header input[type="search"]').focus();page.keyboard.press('Control+v')
 expect(page.locator('header input[type="search"]')).to_have_value('normal text')
 assert len(calls)==2
 # Existing import ownership rejects a second image while one is pending.
 assert pasted(page,name='held.png');expect(panel).to_be_visible()
 assert len(held)==1
 assert pasted(page,name='extra.png');expect(panel.get_by_role('alert')).to_be_visible()
 assert 'extra.png' not in calls
 held.pop().fulfill(json={'id':1000})
 expect(panel.get_by_text('取り込み完了',exact=True)).to_be_visible()
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 assert not errors,errors
 if evidence:page.screenshot(path=str(evidence/f'paste-import-{width}.png'))
 panel.get_by_role('button',name='取り込み結果を閉じる').click();page.close()
 return dict(width=width,native_image_paste=True,duplicate_safe=True,text_fields_unchanged=True,text_paste_unchanged=True,no_video_paste=True,concurrent_import_guard=True,page_errors=errors)

f.exercise=exercise
if __name__=='__main__':f.main()
