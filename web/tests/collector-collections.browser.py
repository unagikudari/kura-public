"""Bounded work selection, retry, selection preservation and stale page responses."""
import importlib.util
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('collector-restore.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)


def exercise(context,base,width,evidence):
    page=context.new_page();errors=[];reads=[];plans=[];failed=False;status_failed=False;hold=False;held=[]
    works=[{'id':i,'title':f'作品 {i}'} for i in range(201,0,-1)]
    page.on('pageerror',lambda e:errors.append(str(e)))
    def route(req):
        nonlocal failed,status_failed
        parsed=urlparse(req.request.url);path=parsed.path.split('/m/collector-next/')[1];q=parse_qs(parsed.query)
        if req.request.method=='POST':
            if path=='collections':return req.fulfill(status=201,json={'id':202,'title':'新作'})
            assert path=='imports'
            plans.append(req.request.post_data_json)
            return req.fulfill(status=503,json={'detail':{'fix':'テスト用の保存待機'}})
        reads.append((path,q))
        if path=='status':
            if not status_failed:
                status_failed=True
                return req.fulfill(status=503,json={'detail':{'fix':'ライブラリを初期化できません'}})
            return req.fulfill(json={'safe_mode':False})
        if path=='collections':
            assert q.get('page')==['true']
            after=int(q['after'][0]) if 'after' in q else None
            if after==102 and not failed:
                failed=True;return req.fulfill(status=503,json={'detail':{'fix':'作品一覧を取得できません'}})
            rows=[c for c in works if after is None or c['id']<after]
            result={'items':rows[:100],'next_cursor':rows[99]['id'] if len(rows)>100 else None}
            if hold:held.append((req,result));return
            return req.fulfill(json=result)
        return req.fulfill(json={'items':[],'next_cursor':None})
    page.route('**/m/collector-next/**',route)
    page.goto(base+'/lab/collector',wait_until='networkidle')
    select=page.get_by_role('combobox').first
    controls=page.get_by_role('region',name='ライブラリと取り込み先')
    expect(page.get_by_role('alert')).to_contain_text('初期化できません')
    page.get_by_role('button',name='一覧を再読み込み').click()
    expect(select.locator('option')).to_have_count(101)
    assert sum(path=='status' for path,q in reads)==2
    select.select_option('150')
    more=controls.get_by_role('button',name='ほかの作品を表示')
    more.click();expect(controls.get_by_role('alert')).to_contain_text('取得できません')
    expect(select).to_have_value('150');expect(select.locator('option')).to_have_count(101)
    more.click();expect(select.locator('option')).to_have_count(201)
    more.click();expect(select.locator('option')).to_have_count(202)
    expect(more).to_have_count(0)
    select.select_option('1')
    expect(page.get_by_role('link',name='作品をZIPで書き出す')).to_have_attribute('href','/m/collector-next/collections/1/export')
    expect(page.get_by_text('取り込み先：作品 1',exact=True)).to_be_visible()
    controls.get_by_role('button',name='作品一覧を更新').click()
    expect(controls.get_by_role('button',name='作品一覧を更新')).to_be_enabled()
    expect(select).to_have_value('1');expect(select.locator('option')).to_have_count(202)
    assert any(path=='collections/1/entries' for path,q in reads)
    # A late pre-creation page must not erase the just-created destination.
    hold=True
    controls.get_by_role('button',name='ほかの作品を表示').click()
    page.wait_for_timeout(100);assert held
    page.get_by_role('textbox',name='新しい作品名').fill('新作')
    controls.get_by_role('button',name='作品を作る',exact=True).click()
    expect(select).to_have_value('202')
    hold=False
    for req,result in held:req.fulfill(json=result)
    page.wait_for_timeout(100)
    expect(select).to_have_value('202');expect(select.locator('option')).to_have_count(203)
    # Import planning uses the old destination's ID, not its position in loaded options.
    select.select_option('1')
    page.locator('input[type=file]:not([webkitdirectory])').first.set_input_files({'name':'page.png','mimeType':'image/png','buffer':b'fixture'})
    expect(page.get_by_role('alert').last).to_contain_text('保存待機')
    assert plans and plans[0]['collection']==1
    assert not errors,errors
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    if evidence:page.screenshot(path=str(evidence/f'collector-collections-{width}.png'),full_page=True)
    page.close();return {'width':width,'initial_status_retry':True,'old_work_reachable':True,'retry':True,'selection_preserved':True,'stale_response_ignored':True,'import_destination':True}


f.exercise=exercise
if __name__=='__main__':f.main()
