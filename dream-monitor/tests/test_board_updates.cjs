const {test} = require('node:test');
const assert = require('node:assert/strict');
const {Gate} = require('../ui/board-updates.js');
const now=1900000000;
const item={aid:'A',name:'Activity',quota:50,eligibility:'eligible',can_join:false,
  registration_phase:'soon',sign_start:now+30,sign_end:now+500,eligibility_checked_at:now};
function state(items=[{...item}], extra={}) {return {me:{uid:'userA'},items,last_sync:now,busy:false,enriching:false,...extra};}
test('first complete snapshot displays automatically, partial bootstrap waits',()=>{
  const g=new Gate(); assert.equal(g.receive(state([], {busy:true}),now),false);
  assert.equal(g.current,null); assert.equal(g.receive(state(),now),true);
  assert.equal(g.current.items.length,1); assert.equal(g.pending,null);
});
test('background changes are staged without mutating the displayed snapshot',()=>{
  const g=new Gate(); g.receive(state(),now);
  const original=g.current; g.receive(state([{...item,quota:20}]),now+1);
  assert.equal(g.current,original); assert.equal(g.current.items[0].quota,50);
  assert.equal(g.pending.items[0].quota,20); assert.equal(g.apply(),true);
  assert.equal(g.current.items[0].quota,20); assert.equal(g.pending,null);
});
test('only the newest completed update is applied',()=>{
  const g=new Gate(); g.receive(state(),now);
  g.receive(state([{...item,quota:40}]),now+1);
  g.receive(state([{...item,quota:30}]),now+2);
  g.receive(state([{...item,quota:10}],{busy:true}),now+3);
  g.apply(); assert.equal(g.current.items[0].quota,30);
});
test('timestamp, task and clock updates do not cause catalogue notifications',()=>{
  const g=new Gate(); g.receive(state(),now);
  g.receive(state([{...item,eligibility_checked_at:now+1}],{last_sync:now+1,auto:{x:{status:'success'}},clock:{offset:2}}),now+1);
  assert.equal(g.pending,null);
});
test('server list reordering alone does not produce a notice',()=>{
  const g=new Gate(), b={...item,aid:'B'};
  g.receive(state([item,b]),now); g.receive(state([b,item]),now+1);
  assert.equal(g.pending,null);
});
test('begin/end boundaries stage a change but preserve displayed eligibility time',()=>{
  const g=new Gate(); g.receive(state(),now);
  g.receive(state(),now+31); assert.ok(g.pending); assert.equal(g.current.now,now);
  g.apply(); assert.equal(g.current.now,now+31);
  g.receive(state(),now+501); assert.ok(g.pending);
});
test('updates that revert to the displayed data clear the notice',()=>{
  const g=new Gate(); g.receive(state(),now);
  g.receive(state([{...item,quota:2}]),now+1); assert.ok(g.pending);
  g.receive(state(),now+2); assert.equal(g.pending,null);
});
test('account switches clear both old snapshot and pending data',()=>{
  const g=new Gate(); g.receive(state(),now);
  g.receive(state([{...item,quota:2}]),now+1);
  g.receive(state([],{me:{uid:'userB'},busy:true}),now+2);
  assert.equal(g.current,null); assert.equal(g.pending,null);
  g.receive(state([],{me:{uid:'userB'}}),now+3);
  assert.deepEqual(g.current.items,[]);
});
test('empty completed catalogue remains a valid pending update',()=>{
  const g=new Gate(); g.receive(state(),now); g.receive(state([]),now+1);
  assert.equal(g.current.items.length,1); assert.equal(g.pending.items.length,0);
  g.apply(); assert.equal(g.current.items.length,0);
});
test('live detail mutations never alias the displayed items',()=>{
  const g=new Gate(), s=state(); g.receive(s,now);
  s.items[0].name='new detail'; assert.equal(g.current.items[0].name,'Activity');
  g.receive(s,now+1); s.items[0].quota=0;
  assert.equal(g.pending.items[0].quota,50);
});
