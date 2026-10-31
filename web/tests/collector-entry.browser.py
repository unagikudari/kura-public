"""Collector entry UI on the built app; uploads go to page-local fixtures only."""
import importlib.util,json,re
from urllib.parse import urlparse,parse_qs
from pathlib import Path
from playwright.sync_api import expect
# The fixture server, session and viewport loop come from the core collector test, so this
# runs without any optional extension.
spec=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('collector-restore.browser.py'))
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)

def exercise(context,base,width,evidence):
 page=context.new_page();errors=[];uploads=[];fail=True;plans={};items={}
 page.on('pageerror',lambda e:errors.append(str(e)))
 def route(req):
  nonlocal fail
  parsed=urlparse(req.request.url);path=parsed.path.split('/m/collector-next/')[1]
  def summary(key):
   rows=items[key];registered=sum(i['state']=='registered' for i in rows);rejected=sum(i['state']=='rejected' for i in rows)
   return dict(request_key=key,item_count=len(rows),registered_count=registered,rejected_count=rejected,remaining_count=len(rows)-registered-rejected,state='awaiting_files' if registered+rejected<len(rows) else 'completed',created_at='2026-10-18T00:00:00Z')
  if req.request.method=='GET':
   if path=='imports':value={'items':[summary(key) for key in reversed(plans)],'next_cursor':None}
   elif path.startswith('imports/'):
    key=path.split('/')[1];value={'items':items[key],'next_cursor':None} if path.endswith('/items') else summary(key)
   else:value={'safe_mode':False} if path=='status' else [] if path=='collections' else {'items':[],'next_cursor':None}
   return req.fulfill(json=value)
  if path=='imports':
   plan=req.request.post_data_json;key=plan['request_key'];plans[key]=plan
   if key not in items:items[key]=[dict(i,position=n+1,state='pending',collection_id=None,error_message=None) for n,i in enumerate(plan['items'])]
   return req.fulfill(status=201,json=summary(key))
  assert path=='upload' and req.request.method=='POST',path
  body=req.request.post_data_buffer.decode(errors='replace');key=re.search(r'name="request_key"\r\n\r\n([^\r]+)',body).group(1);uploads.append(key)
  op=re.search(r'name="operation_key"\r\n\r\n([^\r]+)',body).group(1)
  item=next(i for i in items[op] if i['request_key']==key)
  if 'bad.png' in body:
   item.update(state='rejected',error_message='入力画像を確認してください')
   return req.fulfill(status=400,json={'detail':{'message':'入力画像を確認してください','retryable':False}})
  if 'retry.png' in body and fail:
   fail=False;return req.fulfill(status=503,json={'detail':{'message':'保存先を確認してください','retryable':True}})
  item['state']='registered'
  return req.fulfill(json={'id':1})
 page.route('**/m/collector-next/**',route)
 def choose(files):
  # The page ignores a selection while it is still busy (its picker button is disabled
  # then), so hand the hidden input files only once the button is enabled again.
  expect(page.locator('button.drop')).to_be_enabled();page.locator('input[type=file]').first.set_input_files(files)
 page.goto(base+'/lab/collector',wait_until='networkidle')
 expect(page.get_by_role('heading',name='集めた画像を、自分のライブラリへ。')).to_be_visible()
 expect(page.get_by_text('まだメディアがありません',exact=True)).to_be_visible()
 assert not uploads and page.locator('.import-help').get_attribute('open') is None
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 if evidence:page.screenshot(path=str(evidence/f'collector-entry-{width}.png'))
 choose([{'name':'bad.png','mimeType':'image/png','buffer':b'fixture'},{'name':'good.png','mimeType':'image/png','buffer':b'fixture'}])
 progress=page.get_by_role('region',name='取り込み状況')
 expect(progress.get_by_text('1件登録 · 1件要確認 / 2件',exact=True)).to_be_visible()
 expect(page.get_by_role('progressbar',name='取り込み処理済み件数')).to_have_attribute('value','2')
 expect(page.get_by_role('list',name='取り込めなかったファイル')).to_contain_text('bad.png')
 choose([{'name':'first.png','mimeType':'image/png','buffer':b'fixture'},{'name':'retry.png','mimeType':'image/png','buffer':b'fixture'}])
 expect(progress.get_by_text('取り込みが停止しています',exact=True)).to_be_visible()
 expect(page.get_by_role('progressbar')).to_have_attribute('value','1')
 first_key=uploads[-1]
 original_count=len(uploads)
 page.reload(wait_until='networkidle')
 expect(page.get_by_role('region',name='取り込み履歴')).to_contain_text('未完了1件')
 page.get_by_role('button',name='再開する',exact=True).click()
 expect(page.get_by_text('保存済みの取り込みを再開します。',exact=False)).to_be_visible()
 choose({'name':'retry.png','mimeType':'image/png','buffer':b'changed'})
 expect(page.get_by_role('alert')).to_contain_text('内容が保存済みの計画と異なります')
 assert len(uploads)==original_count
 choose([{'name':'first.png','mimeType':'image/png','buffer':b'fixture'},{'name':'retry.png','mimeType':'image/png','buffer':b'fixture'}])
 expect(progress.get_by_text('2件登録 / 2件',exact=True)).to_be_visible()
 assert len(uploads)==original_count+1
 page.get_by_role('button',name='要確認の内訳',exact=True).click()
 expect(page.locator('.import-history details')).to_contain_text('bad.png')
 assert uploads[-1]==first_key
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth') and not errors,errors
 if evidence:page.screenshot(path=str(evidence/f'collector-result-{width}.png'))
 page.close();return {'width':width,'entry':True,'partial_success':True,'retry_same_receipt':True,'reload_resume_skips_registered':True,'changed_bytes_rejected':True,'no_overflow':True}
fixture.exercise=exercise
if __name__=='__main__':fixture.main()
