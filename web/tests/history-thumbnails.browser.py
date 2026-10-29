"""Back restores visible rows while authoritative reads are delayed (GET only)."""
import importlib.util
from pathlib import Path
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('history_fixture',Path(__file__).with_name('rapid-pool-navigation.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
g=f.g


def exercise(context,base,width,mobile,evidence):
    page=context.new_page();page.on('pageerror',lambda e:g.ERRORS.append(str(e)))
    page.goto(base+'/?q=',wait_until='networkidle');f.r.visible_loaded(page)
    page.locator('.vscroll').evaluate('e=>e.scrollTop=e.scrollHeight*.55')
    f.r.visible_loaded(page)
    anchor=page.locator('.vscroll').evaluate('e=>e.scrollTop')
    target=page.evaluate('''()=>{const s=document.querySelector('.vscroll'),r=s.getBoundingClientRect();return [...s.querySelectorAll('button.thumb')].find(e=>{const b=e.getBoundingClientRect();return b.top>=r.top&&b.bottom<=r.bottom})?.id}''')
    assert target
    # Do not delay navigation/SSR itself. Delay only the subsequent client reads.
    page.evaluate('''()=>{const original=window.fetch;window.__slowReads=0;window.fetch=async(...args)=>{
      const response=await original(...args);
      if(/\\/api\\/(pools|posts)\\?/.test(String(args[0]))){window.__slowReads++;await new Promise(r=>setTimeout(r,2500));window.__slowReads--;}
      return response;
    }}''')
    page.evaluate("id=>{const a=document.createElement('a');a.href='/post/'+id;document.body.append(a);a.click();a.remove()}",target.removeprefix('post-'))
    page.wait_for_url('**/post/*')
    page.go_back(wait_until='domcontentloaded')
    page.wait_for_url('**/?q=')
    page.wait_for_function('''top=>{const s=document.querySelector('.vscroll');if(!s||Math.abs(s.scrollTop-top)>2)return false;
      const r=s.getBoundingClientRect(),tiles=[...s.querySelectorAll('button.thumb')].filter(e=>{const b=e.getBoundingClientRect();return b.bottom>r.top+1&&b.top<r.bottom-1});
      return window.__slowReads>0&&tiles.length>0&&tiles.every(e=>{const i=e.querySelector('img');return i&&i.complete&&i.naturalWidth>0});}''',arg=anchor,timeout=1800)
    if evidence:page.screenshot(path=str(evidence/f'history-posts-{width}.png'))
    page.wait_for_function('window.__slowReads===0')
    page.get_by_role('navigation',name='メインナビゲーション').get_by_role('link',name='Pool',exact=True).click()
    page.wait_for_url('**/?mode=pools');f.r.visible_loaded(page)
    page.locator('.vscroll').evaluate('e=>e.scrollTop=e.scrollHeight*.55');f.r.visible_loaded(page)
    page.wait_for_timeout(150)
    pool_anchor=page.locator('.vscroll').evaluate('e=>e.scrollTop')
    pool_id=page.evaluate("()=>{const s=document.querySelector('.vscroll'),r=s.getBoundingClientRect();return [...s.querySelectorAll('.pool-card')].find(e=>{const b=e.getBoundingClientRect();return b.top>=r.top&&b.bottom<=r.bottom})?.dataset.historyId}")
    assert pool_id
    page.locator('.pool-card[data-history-id="'+pool_id+'"]').click();page.wait_for_url('**/?q=pool:*')
    page.go_back(wait_until='domcontentloaded');page.wait_for_url('**/?mode=pools')
    page.wait_for_function('''top=>{const s=document.querySelector('.vscroll');return s&&Math.abs(s.scrollTop-top)<2&&window.__slowReads>0&&s.querySelectorAll('.pool-card img').length>0}''',arg=pool_anchor,timeout=1800)
    if evidence:page.screenshot(path=str(evidence/f'history-pools-{width}.png'))
    f.r.visible_loaded(page)
    return dict(width=width,back_displays_cached_rows_before_revalidation=True,post_and_pool_positions_preserved=True)

g.exercise=exercise
if __name__=='__main__':g.main()
