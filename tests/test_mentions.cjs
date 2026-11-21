const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const ctx=vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../server/static/mentions.js'),'utf8'),ctx);
const token=(text,start=text.length,end=start)=>JSON.parse(JSON.stringify(ctx.mentionRange(text,start,end)));
assert.deepEqual(token('@'),{start:0,end:1,query:''});
assert.deepEqual(token('请@小'),{start:1,end:3,query:'小'});
assert.deepEqual(token('note @claude1，'),null);
assert.equal(token('mail@example'),null);
assert.equal(token('abc+tag@example'),null);
assert.equal(token('@@manager'),null);
assert.equal(token('@小龙',1,3),null);
assert.deepEqual(token('请 @manager 看一下',6),{start:2,end:10,query:'man'});
assert.deepEqual(token('请 @小龙\n@'),{start:6,end:7,query:''});
const members=[
 {name:'小龙',kind:'agent',harness:'codex'},
 {name:'manager',kind:'agent',harness:'codex',session_type:'manager'},
 {name:'Claude1',kind:'agent',harness:'claude',paused:1},
 {name:'retired',kind:'agent',revoked:1},
 {name:'deleted',kind:'agent',deleted_at:123},
 {name:'bridge',kind:'bridge'},
 {name:'tester',kind:'user'}
];
const candidates=q=>JSON.parse(JSON.stringify(ctx.mentionCandidates(members,q)));
assert.deepEqual(candidates('').map(c=>c.name),['all','小龙','manager','Claude1','tester']);
assert.deepEqual(candidates('CL').map(c=>c.name),['Claude1']);
assert.match(candidates('CL')[0].detail,/已关闭/);
assert.deepEqual(candidates('小').map(c=>c.name),['小龙']);
assert.deepEqual(candidates('所有').map(c=>c.name),['all']);
assert.deepEqual(candidates('nobody'),[]);
console.log('Mention completion: 16 boundary and filtering checks passed');
