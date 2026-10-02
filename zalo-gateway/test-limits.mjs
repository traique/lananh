import test from 'node:test';
import assert from 'node:assert/strict';
import {BoundedSerialQueue, readLimitedBody} from './dist/limits.js';
test('queue bounds waiting events, preserves order, and recovers after failures', async()=>{
 const q=new BoundedSerialQueue(2), order=[];
 let release;
 const gate=new Promise(resolve=>{release=resolve});
 const first=q.add(async()=>{await gate;order.push(1);throw new Error('first failed')});
 const second=q.add(async()=>{order.push(2)});
 await assert.rejects(q.add(async()=>{order.push(3)}),/overloaded/);
 release();await assert.rejects(first,/first failed/);await second;
 await q.add(async()=>{order.push(4)});assert.deepEqual(order,[1,2,4]);
});
test('streamed image limit cancels download even without Content-Length',async()=>{
 let cancelled=false;
 const stream=new ReadableStream({start(controller){controller.enqueue(new Uint8Array(3));controller.enqueue(new Uint8Array(4));},cancel(){cancelled=true}});
 await assert.rejects(readLimitedBody(new Response(stream),5),/exceeds/);
 assert.equal(cancelled,true);
});
test('streamed image chunks assemble without truncation',async()=>{
 const stream=new ReadableStream({start(controller){controller.enqueue(new Uint8Array([1,2]));controller.enqueue(new Uint8Array([3]));controller.close()}});
 assert.deepEqual(await readLimitedBody(new Response(stream),3),new Uint8Array([1,2,3]));
});
