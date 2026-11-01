import test from 'node:test';
import assert from 'node:assert/strict';
import { repaintOnResume } from '../src/lib/repaintOnResume.ts';

test('returning to the page redraws the element for two frames, leaving no style behind', t => {
  const saved = Object.fromEntries(['document','addEventListener','removeEventListener','requestAnimationFrame','cancelAnimationFrame'].map(k => [k,Object.getOwnPropertyDescriptor(globalThis,k)]));
  const doc = Object.assign(new EventTarget(), { visibilityState: 'hidden' });
  const win = new EventTarget();
  let frames = [];
  for (const [key,value] of Object.entries({ document: doc, addEventListener: win.addEventListener.bind(win),
    removeEventListener: win.removeEventListener.bind(win), requestAnimationFrame: cb => frames.push(cb), cancelAnimationFrame: id => { frames[id - 1] = () => {}; } }))
    Object.defineProperty(globalThis,key,{configurable:true,value});
  t.after(() => { for (const [key,value] of Object.entries(saved)) { if (value) Object.defineProperty(globalThis,key,value); else delete globalThis[key]; } });
  const node = { style: { opacity: '' } };
  const action = repaintOnResume(node);
  const flush = () => { const run = frames; frames = []; run.forEach(cb => cb()); };

  doc.dispatchEvent(new Event('visibilitychange'));            // going to the background: nothing
  assert.equal(node.style.opacity, '');
  doc.visibilityState = 'visible';
  doc.dispatchEvent(new Event('visibilitychange'));
  assert.equal(node.style.opacity, '0.999');
  flush(); assert.equal(node.style.opacity, '0.999');          // still set for the first frame
  flush(); assert.equal(node.style.opacity, '');
  win.dispatchEvent(new Event('pageshow'));                    // back/forward cache restore too
  assert.equal(node.style.opacity, '0.999'); flush(); flush();

  // An opacity someone else set comes back, and leaving mid-redraw does not strand 0.999.
  node.style.opacity = '0.5';
  doc.dispatchEvent(new Event('visibilitychange'));
  assert.equal(node.style.opacity, '0.999'); flush(); flush();
  assert.equal(node.style.opacity, '0.5');
  doc.dispatchEvent(new Event('visibilitychange'));
  action.destroy();
  assert.equal(node.style.opacity, '0.5');
  flush(); flush();
  doc.dispatchEvent(new Event('visibilitychange'));
  assert.equal(node.style.opacity, '0.5');
});
