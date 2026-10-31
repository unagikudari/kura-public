import assert from 'node:assert/strict';
import test from 'node:test';
import { claimPlayback } from '../src/lib/collector/videoPlayback.ts';

test('switching playback releases previous decoder; stale cleanup does not release current owner', () => {
  let stoppedA = 0, stoppedB = 0, stoppedC = 0;
  const releaseA = claimPlayback(() => { stoppedA++; });
  const releaseB = claimPlayback(() => { stoppedB++; releaseA(); });
  assert.equal(stoppedA, 1);
  releaseA();
  const releaseC = claimPlayback(() => { stoppedC++; });
  assert.equal(stoppedB, 1);
  releaseB();
  releaseC();
  const releaseD = claimPlayback(() => {});
  assert.equal(stoppedC, 0);
  releaseD();
});
