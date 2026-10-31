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
  const image=new Image();
  const states=[];window.addEventListener(THUMBNAIL_STATE_EVENT,e=>states.push(e.detail.state));
  const action=thumbnailLoader(image,'/thumb?v=1');
  intersection([{target:image,isIntersecting:true}]);
  t.after(()=>{action.destroy();for(const [key,value] of Object.entries(saved)){if(value)Object.defineProperty(globalThis,key,value);else delete globalThis[key];}});
  return {image,action,window,states};
}

test('last stalled attempt reaches failure instead of an eternal blank, then manual retry succeeds',t=>{
  const {image,states}=fixture(t);
  t.mock.timers.tick(15000);t.mock.timers.tick(15000);t.mock.timers.tick(15000);
  assert.equal(image.requests.length,2);
  assert.equal(image.dataset.thumbnailState,'failed');
  t.mock.timers.tick(60000);assert.equal(image.requests.length,2);
  image.dispatchEvent(new Event(THUMBNAIL_RETRY_EVENT));
  assert.equal(image.requests.length,3);assert.equal(new Set(image.requests).size,3);
  image.complete=true;image.naturalWidth=100;image.dispatchEvent(new Event('load'));
  assert.equal(image.dataset.thumbnailState,'loaded');assert.ok(states.includes('failed'));
  t.mock.timers.tick(60000);assert.equal(image.requests.length,3);
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
