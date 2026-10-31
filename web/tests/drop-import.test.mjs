import assert from 'node:assert/strict';
import test from 'node:test';
import { planDropImport, duplicateUpload } from '../src/lib/dropImport.ts';
import { messagesFor } from '../src/lib/i18n/index.ts';
const m=messagesFor('ja');
const input=path=>({file:new File(['content'],path.split('/').at(-1)),relativePath:path});
test('folder media retain paths and natural order, including MIME-less WebM and MP4',()=>{
 const result=planDropImport(['Book/10.jpg','Book/2.jpg','Book/3.WEBM','Book/4.mp4','Book/readme.txt'].map(input),m);
 assert.deepEqual(result.files.map(i=>i.relativePath),['Book/2.jpg','Book/3.WEBM','Book/4.mp4','Book/10.jpg']);
 assert.equal(result.skipped,1);assert.throws(()=>planDropImport(Array(10001).fill(input('x.jpg')),m),/10,000/);
});
test('only the exact checksum duplicate refusal is counted as already registered',()=>{
 assert.equal(duplicateUpload({status:409,message:'すでに登録済みです (checksum重複)'}),true);
 assert.equal(duplicateUpload({status:409,error:'post_already_exists',message:'post_already_exists — すでに登録済みです (checksum重複)。同じ画像は取り込み済みです。'}),true);
 assert.equal(duplicateUpload({status:409,error:'version_conflict',message:'version_conflict — Reload the post'}),false);
 for(const error of [null,new Error('fetch failed'),{status:409,message:'Version conflict'},{status:500,message:'すでに登録済みです (checksum重複)'}]) assert.equal(duplicateUpload(error),false);
});
