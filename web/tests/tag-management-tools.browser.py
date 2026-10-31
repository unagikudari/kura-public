"""Management workflow: text-only browsing, cleanup filters and exact edit entry."""
import importlib.util
import time
from pathlib import Path
from urllib.parse import parse_qs,urlparse,unquote
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('tabs_fixture',Path(__file__).with_name('tag-management-tabs.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
original=f.response
READS=[]
for i,row in enumerate(f.ROWS):
    row.update(version=1,creationTime=None,lastEditTime=None,localizedNames={'ja':'表示名_'+str(i)},usages=0 if i%3==0 else 10)
    if i%2:row['names'].append('alias_'+str(i))


f.ROWS[12]['names'][0] = 'artist/tag_0'  # IDs make slash-containing names editable, too.


def response(url):
    parsed=urlparse(url);q=parse_qs(parsed.query)
    if parsed.path=='/api/tags' and int(q.get('limit',['200'])[0])>50:
        READS.append(q)
        cat=q.get('category',[''])[0];query=q.get('query',[''])[0];cleanup=q.get('cleanup',['all'])[0]
        rows=[r for r in f.ROWS if (not q.get('tag_id') or r['id']==int(q['tag_id'][0])) and (not cat or r['category']==cat) and any(query in n for n in r['names'])
              and (cleanup!='unused' or r['usages']==0) and (cleanup!='no_alias' or len(r['names'])==1)]
        offset=int(q.get('offset',['0'])[0]);limit=int(q.get('limit',['200'])[0])
        return dict(results=rows[offset:offset+limit],total=len(rows),offset=offset,limit=limit,query=query)
    if parsed.path.startswith('/api/tag/') and '/' not in parsed.path[len('/api/tag/'):]:
        return next(r for r in f.ROWS if unquote(parsed.path[len('/api/tag/'):]) in r['names'])
    return original(url)


def exercise(context,base,width,mobile,evidence):
    READS.clear()
    page=context.new_page();page.on('pageerror',lambda e:f.g.ERRORS.append(str(e)))
    page.goto(base+'/tags/manage?q=tag_&cat=artist&view=list',wait_until='networkidle')
    expect(page.locator('.card')).to_have_count(12)
    expect(page.locator('.card img')).to_have_count(0)
    page.get_by_label('整理対象',exact=True).select_option('unused')
    page.wait_for_url('**/tags/manage?q=tag_&cat=artist&cleanup=unused&view=list')
    expect(page.locator('.card')).to_have_count(4)
    page.get_by_label('整理対象',exact=True).select_option('no_alias')
    page.wait_for_url('**/tags/manage?q=tag_&cat=artist&cleanup=no_alias&view=list')
    expect(page.locator('.card')).to_have_count(6)
    assert all(q.get('thumbnails')==['false'] for q in READS)
    if evidence:page.screenshot(path=str(evidence/f'tag-management-compact-{width}.png'))
    selected=page.locator('.card').first
    name=selected.locator('.name').inner_text()
    assert selected.get_by_role('link',name='画像を見る',exact=True).get_attribute('href').startswith('/?q=')
    pool_link=selected.get_by_role('link',name='Pool一覧',exact=True)
    pool_query=parse_qs(urlparse(pool_link.get_attribute('href')).query)
    assert pool_query=={'mode':['pools'],'q':['tag:'+name]}
    pool_link.click();page.wait_for_url('**/?mode=pools&q=**');page.wait_for_load_state('networkidle')
    expect(page.locator('header input[type=search]')).to_have_value('tag:'+name)
    page.go_back(wait_until='networkidle')
    expect(page.get_by_label('タグ一覧の表示',exact=True)).to_have_value('list')
    expect(page.get_by_label('整理対象',exact=True)).to_have_value('no_alias')
    # The general editor retains the full current search/category/cleanup context.
    page.get_by_role('navigation',name='タグ管理の画面').get_by_role('link',name='編集',exact=True).click()
    page.wait_for_url('**/tags/edit?**')
    expect(page.get_by_label('編集するタグを検索',exact=True)).to_have_value('tag_')
    expect(page.locator('.cat-chip.active')).to_have_text('artist')
    expect(page.get_by_label('編集画面の整理対象',exact=True)).to_have_value('no_alias')
    back=page.get_by_role('link',name='タグ一覧へ戻る →',exact=True)
    back.click();page.wait_for_url('**/tags/manage?**')
    expect(page.get_by_label('タグ一覧の表示',exact=True)).to_have_value('list')
    page.get_by_role('link',name=name+' を編集',exact=True).click()
    page.wait_for_url('**/tags/edit?**tagId=**')
    expect(page.locator('.panel .name-row').first.locator('input')).to_have_value(name)
    expect(page.get_by_label('言語コード',exact=True)).to_have_value('ja')
    expect(page.get_by_label('言語別表示名',exact=True)).to_have_value(next(r['localizedNames']['ja'] for r in f.ROWS if r['names'][0]==name))
    assert page.locator('.tag-row').count()==1
    assert page.get_by_role('navigation',name='編集するタグのカテゴリ').bounding_box()['height']<52
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    if evidence:page.screenshot(path=str(evidence/f'tag-editor-entry-{width}.png'))
    page.get_by_label('編集するタグを検索',exact=True).fill('artist_tag_2')
    page.wait_for_url('**/tags/edit?q=artist_tag_2&cat=artist&cleanup=no_alias&from=**')
    expect(page.locator('.tag-name')).to_have_text('artist_tag_2')
    expect(page.locator('.panel')).to_have_count(0)
    page.go_back(wait_until='networkidle')
    expect(page.locator('.panel .name-row').first.locator('input')).to_have_value(name)
    page.get_by_role('link',name='タグ一覧へ戻る →',exact=True).click()
    page.wait_for_url('**/tags/manage?**')
    expect(page.get_by_label('整理対象',exact=True)).to_have_value('no_alias')
    expect(page.get_by_label('タグ一覧の表示',exact=True)).to_have_value('list')
    page.get_by_label('タグ一覧の表示',exact=True).select_option('cards')
    page.wait_for_url('**/tags/manage?q=tag_&cat=artist&cleanup=no_alias')
    expect(page.locator('.card')).to_have_count(6)
    expect(page.locator('.card .card-actions').first.get_by_role('link',name='Pool一覧',exact=True)).to_be_visible()
    expect(page.locator('.thumb')).to_have_count(0)  # no-image cards have no empty box
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    if evidence:page.screenshot(path=str(evidence/f'tag-management-tools-{width}.png'))
    return dict(width=width,cleanup_filters=True,compact_no_thumbnail_reads=True,exact_editor=True,return_context=True,aliases_and_localizations_preserved=True)


f.response=response;f.g.response=response;f.exercise=exercise
if __name__=='__main__':f.main()
