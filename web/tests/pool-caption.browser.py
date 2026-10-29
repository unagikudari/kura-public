"""Pool names remain readable and clicking their pixels opens the same pool.

Uses actual compiled CSS, synthetic GET-only API/media and normal browser Back.
Run after npm run build; CHROMIUM and EVIDENCE_DIR follow sibling fixtures.
"""
import importlib.util
from pathlib import Path
import re
from urllib.parse import parse_qs,urlparse
from playwright.sync_api import expect

spec=importlib.util.spec_from_file_location('pool_fixture',Path(__file__).with_name('compact-pool-header.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)


def exercise(context,base,width,phone,evidence):
    page=context.new_page();page.on('pageerror',lambda e:f.f.ERRORS.append(str(e)))
    page.goto(base+'/?mode=pools',wait_until='networkidle')
    expect(page.locator('.pool-card')).to_have_count(4)
    observed=[]
    for pid in (33,34):
        card=page.locator(f'.pool-card[data-history-id="{pid}"]')
        caption=card.locator('.pool-name');overlay=card.locator('.pool-overlay')
        caption.scroll_into_view_if_needed();expect(caption).to_be_visible()
        expect(caption).to_have_text(f.NAMES[pid]);expect(card).to_have_attribute('title',f.NAMES[pid])
        data=card.evaluate('''e=>{const r=x=>{let b=x.getBoundingClientRect();return {x:b.x,y:b.y,w:b.width,h:b.height}},o=e.querySelector('.pool-overlay'),n=e.querySelector('.pool-name'),i=e.querySelector('img'),os=getComputedStyle(o),ns=getComputedStyle(n);return {card:r(e),image:r(i),overlay:r(o),caption:r(n),overlayPointer:os.pointerEvents,namePointer:ns.pointerEvents,gradient:os.backgroundImage,color:ns.color,shadow:ns.textShadow,lineHeight:parseFloat(ns.lineHeight),clamp:ns.webkitLineClamp,position:os.position,objectFit:getComputedStyle(i).objectFit}}''')
        assert data['overlayPointer']==data['namePointer']=='none',data
        assert data['position']=='absolute' and data['clamp']=='2',data
        alphas=[float(x) for x in re.findall(r'rgba\(0, 0, 0, ([0-9.]+)\)',data['gradient'])]
        assert 'linear-gradient' in data['gradient'] and 0 in alphas and any(.5<a<1 for a in alphas),data
        assert min(map(int,re.findall(r'\d+',data['color'])[:3]))>=200 and data['shadow']!='none',data
        assert 0<data['caption']['h']<=data['lineHeight']*2+1,data
        assert abs(data['card']['w']-data['card']['h'])<1,data
        assert abs(data['image']['w']-data['card']['w'])<1 and abs(data['image']['h']-data['card']['h'])<1,data
        assert data['objectFit']=='cover',data
        assert data['overlay']['h']<data['card']['h']/2,data
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        # A geometric hit test and real mouse click prove the overlay does not
        # own clicks, including at the actual rendered title text coordinates.
        box=caption.bounding_box();x=box['x']+min(16,box['width']/2);y=box['y']+min(8,box['height']/2)
        hit=page.evaluate('p=>document.elementFromPoint(p.x,p.y)?.closest(".pool-card")?.dataset.historyId',{ 'x':x,'y':y })
        assert hit==str(pid),(hit,pid,data)
        before_scroll=page.locator('.vscroll').evaluate('(e)=>e.scrollTop')
        if evidence:page.screenshot(path=str(evidence/f'pool-caption-{width}-{pid}.png'))
        page.mouse.click(x,y)
        page.wait_for_function('pid=>new URL(location.href).searchParams.get("q")===`pool:${pid}`',arg=pid)
        expect(page.locator('.pool-title-name')).to_have_text(f.NAMES[pid])
        page.go_back(wait_until='networkidle')
        expect(card).to_be_visible()
        assert parse_qs(urlparse(page.url).query).get('mode')==['pools']
        page.wait_for_function('n=>Math.abs(document.querySelector(".vscroll").scrollTop-n)<2',arg=before_scroll)
        after=card.bounding_box()
        assert abs(after['width']-data['card']['w'])<1 and abs(after['height']-data['card']['h'])<1,(after,data)
        observed.append(dict(pool_id=pid,geometry=data,caption_hit_opens_same_pool=True,back_restores_scroll=True))
    page.close();return dict(width=width,phone=phone,pools=observed)


f.exercise=exercise
if __name__=='__main__':f.main()
