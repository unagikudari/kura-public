import assert from 'node:assert/strict';
import test from 'node:test';
import { droppedFiles, planFiles, selectedFiles } from '../src/lib/collector/importFiles.ts';

const file = (name, type = '') => new File(['sample'], name, { type });
const entry = value => ({ name: value.name, isFile: true, isDirectory: false, file: resolve => resolve(value) });

test('natural ordering preserves nested paths and filters only unsupported files', () => {
  const items = ['10.jpg', '2.jpg', '1.jpg', 'notes.txt'].map(name => ({ file: file(name), relativePath: `本/第1章/${name}` }));
  const result = planFiles(items);
  assert.equal(result.skipped, 1);
  assert.deepEqual(result.files.map(i => i.relativePath), ['本/第1章/1.jpg', '本/第1章/2.jpg', '本/第1章/10.jpg']);
  assert.equal(planFiles([{ file: file('movie.WEBM') }]).files.length, 1);
});

test('folder picker relative paths and same-name files in distinct directories survive', () => {
  const a = file('a.jpg'), b = file('a.jpg');
  Object.defineProperty(a, 'webkitRelativePath', { value: 'Book/chapter1/a.jpg' });
  Object.defineProperty(b, 'webkitRelativePath', { value: 'Book/chapter2/a.jpg' });
  assert.deepEqual(planFiles(selectedFiles([a, b])).files.map(i => i.relativePath), ['Book/chapter1/a.jpg', 'Book/chapter2/a.jpg']);
  assert.throws(() => planFiles(selectedFiles([a, a])), /重複/);
  assert.throws(() => planFiles(Array(10001).fill({ file: a })), /10,000/);
});

test('drop drains every directory-reader batch and captures handles synchronously', async () => {
  const children = Array.from({ length: 205 }, (_, i) => entry(file(`${i + 1}.jpg`)));
  let reads = 0, active = true;
  const root = { name: 'Book', isFile: false, isDirectory: true, createReader() {
    let index = 0;
    return { readEntries(resolve) { reads++; const batch = children.slice(index, index + 100); index += 100; resolve(batch); } };
  } };
  const transfer = { files: [], items: [{ kind: 'file', webkitGetAsEntry() { assert.ok(active); return root; }, getAsFile() { assert.ok(active); return null; } }] };
  let progress = 0;
  const work = droppedFiles(transfer, count => { progress = count; });
  active = false;
  const result = await work;
  assert.equal(result.length, 205); assert.equal(progress, 205); assert.equal(reads, 4);
  assert.equal(result.at(-1).relativePath, 'Book/205.jpg');
});

test('nested folders recurse and read errors reject the incomplete plan', async () => {
  const leaf = entry(file('2.jpg'));
  const directory = (name, children) => ({ name, isFile: false, isDirectory: true, createReader() {
    let read = false;
    return { readEntries(resolve) { resolve(read ? [] : children); read = true; } };
  } });
  const root = directory('Book', [directory('Part', [leaf])]);
  const transfer = value => ({ files: [], items: [{ kind: 'file', webkitGetAsEntry: () => value, getAsFile: () => null }] });
  assert.equal((await droppedFiles(transfer(root), () => {}))[0].relativePath, 'Book/Part/2.jpg');
  const broken = { name: 'Unreadable', isFile: false, isDirectory: true, createReader: () => ({
    readEntries(_resolve, reject) { reject(new DOMException('denied', 'NotReadableError')); },
  }) };
  await assert.rejects(droppedFiles(transfer(broken), () => {}), /最後まで読み取れません/);
});

test('ordinary file drops work when entry APIs are missing', async () => {
  const value = file('photo.png', 'image/png');
  const result = await droppedFiles({ files: [value], items: [] }, () => {});
  assert.equal(result[0].file, value);
  assert.equal(result[0].relativePath, undefined);
});
