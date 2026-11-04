import test from 'node:test';
import assert from 'node:assert/strict';
import { thumbnailLoader, THUMBNAIL_RETRY_EVENT, THUMBNAIL_STATE_EVENT } from '../src/lib/thumbnailLoader.ts';

function fixture(t) {
  const saved = Object.fromEntries(['window','document','navigator','IntersectionObserver'].map(k => [k,Object.getOwnPropertyDescriptor(globalThis,k)]));
  let intersection;
  const window = new EventTarget();
  for (const [key,value] of Object.entries({window,document:{baseURI:'https://kura.test/'},navigator:{onLine:true},
    IntersectionObserver:class { constructor(cb){intersection=cb;} observe(){} unobserve(){} disconnect(){} }})) {
    Object.defineProperty(globalThis,key,{configurable:true,value});
  }
  t.mock.timers.enable({apis:['setTimeout','Date']});
  class Image extends EventTarget {
    dataset={};complete=false;naturalWidth=0;isConnected=true;requests=[];
    set src(value){this.requests.push(value);this.complete=false;this.naturalWidth=0;}
  }
  const states=[];window.addEventListener(THUMBNAIL_STATE_EVENT,e=>states.push(e.detail.state));
  const actions=[];
  // Another thumbnail on the same page, in view.
  const add=(source='/thumb?v=1')=>{
    const image=new Image();const action=thumbnailLoader(image,source);actions.push(action);
    intersection([{target:image,isIntersecting:true}]);
    return {image,action};
  };
  const {image,action}=add();
  t.after(()=>{for(const a of actions)a.destroy();for(const [key,value] of Object.entries(saved)){if(value)Object.defineProperty(globalThis,key,value);else delete globalThis[key];}});
  const hide=image=>intersection([{target:image,isIntersecting:false}]);
  const show=image=>intersection([{target:image,isIntersecting:true}]);
  return {image,action,window,states,add,hide,show};
}

test('last stalled attempt reaches failure instead of an eternal blank, then manual retry succeeds',t=>{
  const {image,states}=fixture(t);
  t.mock.timers.tick(15000);t.mock.timers.tick(15000);t.mock.timers.tick(15000);
  assert.equal(image.requests.length,2);
  assert.equal(image.dataset.thumbnailState,'failed');
  image.dispatchEvent(new Event(THUMBNAIL_RETRY_EVENT));
  assert.equal(image.requests.length,3);assert.equal(new Set(image.requests).size,3);
  image.complete=true;image.naturalWidth=100;image.dispatchEvent(new Event('load'));
  assert.equal(image.dataset.thumbnailState,'loaded');assert.ok(states.includes('failed'));
  t.mock.timers.tick(60000);assert.equal(image.requests.length,3);
});
test('a tile that failed by stalling keeps trying slowly while in view, and stops out of view',t=>{
  const {image,hide}=fixture(t);
  t.mock.timers.tick(3000);t.mock.timers.tick(8000);t.mock.timers.tick(15000);
  assert.equal(image.dataset.thumbnailState,'failed');assert.equal(image.requests.length,2);
  t.mock.timers.tick(29999);assert.equal(image.requests.length,2);
  t.mock.timers.tick(1);assert.equal(image.requests.length,3);assert.equal(image.dataset.thumbnailState,'retrying');
  // The slow retry starts a fresh round: 8 s, then 15 s, then failed again.
  t.mock.timers.tick(8000);assert.equal(image.requests.length,4);
  t.mock.timers.tick(15000);assert.equal(image.dataset.thumbnailState,'failed');
  hide(image);t.mock.timers.tick(120000);assert.equal(image.requests.length,4);
});
test('any thumbnail that loads retries the stalled ones at once (the link is back)',t=>{
  const {image,add}=fixture(t);
  t.mock.timers.tick(3000);assert.equal(image.requests.length,1);   // stalled once, retrying
  const other=add('/thumb?v=2').image;
  other.complete=true;other.naturalWidth=100;other.dispatchEvent(new Event('load'));
  assert.equal(image.requests.length,2);assert.equal(image.dataset.thumbnailState,'retrying');
  // One announcement per 2 s: a page of loads is not a storm of retries.
  const third=add('/thumb?v=3').image;
  third.complete=true;third.naturalWidth=100;third.dispatchEvent(new Event('load'));
  assert.equal(image.requests.length,2);
});
test('a tile that failed with an error is neither slow-retried nor woken by a recovery',t=>{
  const {image,add}=fixture(t);
  for(const delay of [500,1500]){image.dispatchEvent(new Event('error'));t.mock.timers.tick(delay);}
  image.dispatchEvent(new Event('error'));
  assert.equal(image.dataset.thumbnailState,'failed');assert.equal(image.requests.length,2);
  t.mock.timers.tick(5000);
  const other=add('/thumb?v=2').image;
  other.complete=true;other.naturalWidth=100;other.dispatchEvent(new Event('load'));
  t.mock.timers.tick(120000);assert.equal(image.requests.length,2);
});
test('a first attempt that stalls is retried after 3 s, not 15 s, and later attempts wait longer',t=>{
  const {image}=fixture(t);
  t.mock.timers.tick(2999);assert.equal(image.requests.length,0);
  t.mock.timers.tick(1);assert.equal(image.requests.length,1);assert.equal(image.dataset.thumbnailState,'retrying');
  t.mock.timers.tick(7999);assert.equal(image.requests.length,1);
  t.mock.timers.tick(1);assert.equal(image.requests.length,2);
  t.mock.timers.tick(14999);assert.notEqual(image.dataset.thumbnailState,'failed');
  t.mock.timers.tick(1);assert.equal(image.dataset.thumbnailState,'failed');
});
test('offline time does not consume retries and reconnection restarts without scrolling',t=>{
  const {image,window}=fixture(t);
  navigator.onLine=false;window.dispatchEvent(new Event('offline'));
  assert.equal(image.dataset.thumbnailState,'offline');
  t.mock.timers.tick(120000);assert.equal(image.requests.length,0);
  navigator.onLine=true;window.dispatchEvent(new Event('online'));
  assert.equal(image.requests.length,1);
  image.complete=true;image.naturalWidth=100;image.dispatchEvent(new Event('load'));
  assert.equal(image.dataset.thumbnailState,'loaded');
});
test('HTTP failures stop after two automatic retries and destroyed nodes release timers',t=>{
  const {image,action}=fixture(t);
  for(const delay of [500,1500]){image.dispatchEvent(new Event('error'));t.mock.timers.tick(delay);}
  image.dispatchEvent(new Event('error'));
  assert.equal(image.requests.length,2);assert.equal(image.dataset.thumbnailState,'failed');
  action.destroy();t.mock.timers.tick(60000);
  assert.equal(image.requests.length,2);
});
test('a recovery restarts a stalled tile once, then leaves the fresh request its watchdog (codex 1.308.2 P1)',t=>{
  const {image,add}=fixture(t);
  t.mock.timers.tick(3000);                       // stalled: first retry
  for(let i=0;i<4;i++){                           // a healthy load every 2 s, at 3, 5, 7, 9 s
    const other=add('/healthy'+i).image;
    other.complete=true;other.naturalWidth=100;other.dispatchEvent(new Event('load'));
    if(i<3)t.mock.timers.tick(2000);
  }
  assert.equal(image.requests.length,2);          // one recovery restart, at 3 s, and no more
  t.mock.timers.tick(1999);assert.equal(image.requests.length,2);
  t.mock.timers.tick(1);assert.equal(image.requests.length,3);   // its own 8 s watchdog, at 11 s
});
test('the retry after an error is not a stall, so a recovery does not replace it (codex 1.308.2 P2)',t=>{
  const {image,add}=fixture(t);
  image.dispatchEvent(new Event('error'));
  t.mock.timers.tick(500);
  assert.equal(image.requests.length,1);
  const other=add('/healthy').image;
  other.complete=true;other.naturalWidth=100;other.dispatchEvent(new Event('load'));
  assert.equal(image.requests.length,1);
});
test('an image already held when it scrolls into view does not announce a recovery',t=>{
  const {image,add,hide,show}=fixture(t);
  t.mock.timers.tick(3000);assert.equal(image.requests.length,1);   // stalled, retrying
  const cached=add('/cached').image;hide(cached);
  cached.complete=true;cached.naturalWidth=100;
  t.mock.timers.tick(2500);
  show(cached);
  assert.equal(cached.dataset.thumbnailState,'loaded');
  assert.equal(image.requests.length,1);
});
