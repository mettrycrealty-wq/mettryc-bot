const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function setup() {
  const records = new Map(); let now = 1000000;
  const folder = {
    getName: () => 'private',
    getFilesByName: name => { let used = false; return {hasNext: () => !used && records.has(name), next: () => {
      used = true; return {getBlob: () => ({getDataAsString: () => records.get(name)}), setContent: text => records.set(name,text)};
    }}; },
    createFile: (name,text) => records.set(name,text)
  };
  const ctx = vm.createContext({Date: {now: () => now}, MimeType: {PLAIN_TEXT:'text/plain'}});
  vm.runInContext(fs.readFileSync('apps_script/paty_memory/Code.gs','utf8'),ctx);
  const base={sender:'a'.repeat(64), owner:'b'.repeat(32), message:'c'.repeat(64)};
  return {call: (action, values={}) => ctx.operarMemoria({...base,action,...values},folder), records,
    advance: ms => now += ms, ctx};
}
test('restore after new execution, duplicate and idempotent commit', () => {
  const s=setup();
  assert.equal(s.call('acquire',{lease:360}).state,null);
  const commit={state:{ultimo_lote:['123']},ttl:60,duplicate_ttl:180};
  s.call('commit',commit); s.call('commit',commit);
  assert.equal(s.call('acquire',{lease:360}).duplicate,true);
  assert.equal(s.call('acquire',{owner:'d'.repeat(32),message:'e'.repeat(64),lease:360}).state.ultimo_lote[0],'123');
});
test('concurrent owner, stale writes, release and reset cannot overwrite active turn', () => {
  const s=setup(); s.call('acquire',{lease:1});
  assert.throws(()=>s.call('acquire',{owner:'d'.repeat(32),lease:360}));
  assert.throws(()=>s.call('reset'));
  s.advance(1001); s.call('acquire',{owner:'d'.repeat(32),lease:360});
  assert.throws(()=>s.call('commit',{state:{},ttl:60,duplicate_ttl:180}));
  s.call('release');
  assert.throws(()=>s.call('acquire',{lease:360}));
});
test('expired state is not restored and corruption fails closed',()=>{
  const s=setup(); s.call('acquire',{lease:360}); s.call('commit',{state:{x:1},ttl:1,duplicate_ttl:1});
  s.advance(1001); assert.equal(s.call('acquire',{lease:360}).state,null);
  for(const key of s.records.keys()) s.records.set(key,'broken');
  assert.throws(()=>s.call('acquire',{lease:360}));
});
test('unauthenticated request cannot access Drive',()=>{
  const s=setup(); let accessed=false;
  s.ctx.PropertiesService={getScriptProperties:()=>({getProperty:()=> 'secret'})};
  s.ctx.DriveApp={getFolderById:()=>{accessed=true; throw Error();}};
  s.ctx.ContentService={MimeType:{JSON:'json'},createTextOutput:text=>({setMimeType:()=>JSON.parse(text)})};
  assert.equal(s.ctx.doPost({postData:{contents:JSON.stringify({token:'wrong'})}}).ok,false);
  assert.equal(accessed,false);
});
