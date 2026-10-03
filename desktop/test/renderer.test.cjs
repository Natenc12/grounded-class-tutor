const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.value = ''; this.disabled = false; this.hidden = false; this.textContent = ''; this.className = ''; this.attributes = {}; this.classList = {toggle: (name, value) => {const set = new Set(this.className.split(' ').filter(Boolean)); if (value) set.add(name); else set.delete(name); this.className = [...set].join(' ');}}; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  setAttribute(k,v) {this.attributes[k] = v;}
  fire(type) { return this.listeners[type]?.({target:this}); }
}
const root = require('path').join(__dirname, '../renderer/');
const html = fs.readFileSync(root + 'index.html', 'utf8');
const source = fs.readFileSync(root + 'app.js', 'utf8');
const nodes = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(m => [m[1],new Element()]));
let emit, pendingAskResolve, payload, initResolve;
let current = {session: {status:'disconnected',sharing:false}, models:[]};
const gct = {onState(fn) {emit=fn; return ()=>{};}, getState:()=>new Promise(r=>initResolve=r), async signIn(...args){assert.equal(args.length,0,'sign-in must use no-argument IPC');return current;}, async cancelSignIn(){return current;}, async signOut(){return current;}, async chooseFile(){return current;}, async useSample(){return current;}, async cancelAsk(){current={...current,busy:null,result:{state:'CANCELLED'}};emit(current);return current;}, async openUsage(){return current;}, ask(p){payload=p;current={...current,busy:'ask',result:undefined};emit(current);return new Promise(r=>pendingAskResolve=r);}};
vm.runInNewContext(source, {document:{getElementById:id=>nodes[id],createElement:tag=>new Element(tag)},window:{gct,addEventListener(){}},console,Promise,Set,Map,JSON});
const flush=()=>new Promise(r=>setImmediate(r));
const update=(changes)=>{current={...current,...changes};emit(structuredClone(current));};
(async()=>{
  await flush();
  nodes['sign-in'].fire('click');await flush();
  assert.equal(nodes.ask.disabled,true,'disconnected must not ask');
  update({session:{status:'connected',sharing:true,identity:{name:'Student <img>'}},models:[{slug:'m1',display_name:'Model one'},{slug:'m2',display_name:'Model two'}],document:{filename:'<img src=x>.pdf',page_count:6,pages:Array.from({length:6},(_,i)=>({page_or_slide:i+1,text:'Source <script> '+i}))}});
  assert.equal(nodes['account-identity'].textContent,'Student <img>');
  assert.equal(nodes['document-name'].textContent,'<img src=x>.pdf');
  assert.equal(nodes['selection-count'].textContent,'5 of 5 selected');
  const rows=nodes['page-options'].children.slice(1);
  assert.equal(rows[5].children[0].disabled,true,'sixth page must be disabled');
  rows[0].children[0].checked=false;rows[0].children[0].fire('change');
  assert.equal(rows[5].children[0].disabled,false,'sixth unlocked when selection below five');
  rows[5].children[0].checked=true;rows[5].children[0].fire('change');
  nodes.question.value=' What is the idea? ';nodes.question.fire('input');
  assert.equal(nodes.ask.disabled,false);
  nodes.model.value='m2';nodes.model.fire('change');
  update({notice:'Connected'});
  assert.equal(nodes.model.value,'m2','valid selection survives fresh state');
  update({models:[{slug:'m1',display_name:'Model one'}]});
  assert.equal(nodes.model.value,'m1','removed model cannot remain selected');
  update({busy:'document'});
  for(const id of ['choose-file','use-sample','ask','sign-out','model','question'])assert.equal(nodes[id].disabled,true,id+' locked while parsing');
  assert.equal(nodes['ask-help'].textContent,'Reading document…');
  update({busy:null,session:{status:'connected',sharing:false}});
  assert.equal(nodes.ask.disabled,true,'sharing must be explicitly enabled');
  assert.equal(nodes['sign-in'].hidden,false,'connected identity must still be able to authorize plan use');
  update({session:{status:'connecting',sharing:false},busy:'signin'});
  assert.equal(nodes['cancel-sign-in'].hidden,false);
  assert.equal(nodes['sign-in'].disabled,true);
  update({session:{status:'connected',sharing:true},busy:null});
  nodes.ask.fire('click');await flush();
  assert.equal(payload.question,'What is the idea?');
  assert.deepEqual([...payload.pages],[2,3,4,5,6]);
  assert.equal(payload.model,'m1');
  assert.equal(nodes['answer-progress'].hidden,false);
  assert.equal(nodes['cancel-ask'].hidden,false);
  const grounded={state:'GROUNDED',answer_prose:'Safe <img onerror=x> answer.',citations:[{label:'[1]',file:'Example.pdf',page_or_slide:2,chunk_id:'x'}],coverage:{complete:true,gaps:[]},integrity:{ok:true,reasons:[]}};
  update({busy:null,result:grounded});pendingAskResolve(current);await flush();
  assert.equal(nodes['answer-content'].hidden,false);
  assert.equal(nodes['answer-badge'].textContent,'Grounded answer');
  assert.equal(nodes['answer-prose'].children[0].textContent,'Safe <img onerror=x> answer.');
  assert.equal(nodes.citations.children.length,1);
  nodes.question.value='Changed question';nodes.question.fire('input');
  assert.equal(nodes['answer-content'].hidden,true);
  update({notice:'Unrelated update'});
  assert.equal(nodes['answer-content'].hidden,true,'old answer stays hidden across cloned state');
  update({result:{...grounded,state:'PARTIAL',coverage:{complete:false,gaps:['Missing detail']}}});
  assert.equal(nodes['answer-badge'].textContent,'Partial support');assert.equal(nodes['gaps-section'].hidden,false);
  update({result:{...grounded,state:'INTEGRITY_FLAGGED',integrity:{ok:false,reasons:['Check source']}}});
  assert.equal(nodes['answer-badge'].textContent,'Needs review');assert.match(nodes['answer-alert'].textContent,/Check source/);
  update({result:{...grounded,state:'REFUSAL',answer_prose:'Insufficient material',coverage:{complete:false,gaps:[]}}});
  assert.equal(nodes['answer-badge'].textContent,'Not supported');
  update({result:{state:'ERROR',error:{message:'Please reconnect'}}});
  assert.equal(nodes['answer-badge'].textContent,'Couldn’t complete');assert.equal(nodes['answer-prose'].children.length,0);
  update({result:undefined});assert.equal(nodes['answer-content'].hidden,true);assert.equal(nodes['answer-empty'].hidden,false);
  initResolve({session:{status:'disconnected',sharing:false},models:[]});await flush();
  assert.equal(nodes['account-status-text'].textContent,'Connected','late initial snapshot must not overwrite streamed state');
  assert(!source.includes('innerHTML'));
  assert(html.includes("connect-src 'none'"));
  assert(html.includes("default-src 'none'"));
  console.log('PASS: renderer account, busy, five-page bound, model refresh, safe text, stale-result, answer states, and initial-state race checks');
})().catch(err=>{console.error(err);process.exitCode=1});
