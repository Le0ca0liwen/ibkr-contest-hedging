const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const ids=new Set([...fs.readFileSync('hedge/web/index.html','utf8').matchAll(/id="([^"]+)"/g)].map(x=>x[1]));
const nodes=new Map([...ids].map(id=>[id,{content:'old-token',dataset:{},classList:{toggle(){}},addEventListener(){}}]));
const posts=[];
let reject=true,lastPath=null,lastBody=null;
const response=(status,text)=>({status,ok:status>=200&&status<300,text:async()=>text,json:async()=>JSON.parse(text)});
const context=vm.createContext({
  console,Intl,Date,JSON,Error,AbortSignal,setInterval(){},setTimeout(){},clearTimeout(){},
  DOMParser:class{parseFromString(){return{querySelector:()=>({content:'new-token'})};}},
  document:{querySelector:()=>({content:'old-token'}),querySelectorAll:()=>[],
    getElementById:id=>nodes.get(id)||null,addEventListener(){}},
  fetch:async(path,options)=>{
    if(path==='/api/state')return response(200,'{}');
    if(path==='/')return response(200,'fresh authorization page');
    posts.push(options.headers['X-Control-Token']);
    lastPath=path;lastBody=JSON.parse(options.body);
    if(options.headers['X-Control-Token']==='old-token')return response(403,'Local control token required');
    if(reject)return response(409,JSON.stringify({detail:'Mandatory holdings missing'}));
    return response(200,JSON.stringify({mode:'PAUSED'}));
  }
});
vm.runInContext(fs.readFileSync('hedge/web/app.js','utf8'),context);
vm.runInContext('refresh=async()=>{}',context);
(async()=>{
  await vm.runInContext('control("paper")',context);
  assert.deepEqual(posts,['old-token','new-token']);
  assert.equal(nodes.get('notice').textContent,'Mandatory holdings missing');
  assert.equal(nodes.get('notice').hidden,false);
  reject=false;
  await vm.runInContext('control("pause")',context);
  assert.match(nodes.get('notice').textContent,/Pause accepted/);
  assert.match(nodes.get('notice').textContent,/disarmed/);
  await vm.runInContext('control("shadow")',context);
  assert.match(nodes.get('notice').textContent,/Shadow mode accepted/);
  await vm.runInContext('renderRecoveryAvailability({mode:"shadow",promotion:{eligible:false,independent_test_windows:0,status:"Validation incomplete",blockers:["Missing scores <script>"]}})',context);
  assert.equal(nodes.get('recovery-enable').disabled,true);
  assert.match(nodes.get('recovery-availability').textContent,/0\/20/);
  assert.match(nodes.get('recovery-validation').innerHTML,/&lt;script&gt;/);
  await vm.runInContext('renderRecoveryAvailability({mode:"shadow",promotion:{eligible:true,independent_test_windows:20,status:"Validation passed",blockers:[]}})',context);
  assert.equal(nodes.get('recovery-enable').disabled,false);
  await vm.runInContext('renderRecoveryAvailability({mode:"paper",promotion:{eligible:true,independent_test_windows:20}})',context);
  assert.equal(nodes.get('recovery-enable').disabled,true);
  const timing=vm.runInContext('solverSummary({total_ms:2143,accepted_stage:"primary",ranking_complete:false})',context);
  assert.match(timing,/2\.14s/);
  assert.match(timing,/ranking incomplete/);
  assert.equal(vm.runInContext('solverSummary(undefined)',context),'');
  await vm.runInContext('renderContinuity({continuity:{automatic_resume_pending:true,wait:{reason:"Stale UAL book"}}})',context);
  assert.equal(nodes.get('continuity-status').hidden,false);
  assert.match(nodes.get('continuity-status').textContent,/resumes automatically/);
  await vm.runInContext('renderContinuity({continuity:{automatic_resume_pending:false,pause_reason:"Operator requested pause"}})',context);
  assert.match(nodes.get('continuity-status').textContent,/Automatic resume is disabled/);
  await vm.runInContext('renderBurner({mode:"paper",effective_mode:"paper",state:"RESIDUAL",reason:"<img src=x>",window:{start:"2026-10-22T15:40:00-04:00",stock_entry_cutoff:"2026-10-22T15:56:00-04:00"},gap:{median:12.5},today:{cycles:2,realized_cost:8.4},last_cycles:[{label:"<script>x</script>",entry_filled:10,exit_filled:4,residual:6,realized_loss:2,status:"RESIDUAL",reason:"<b>exit</b>",finished_at:"2026-10-22T15:58:00-04:00"}],symbols:["SPCX"],admissibility_source:"Instructor <note>"})',context);
  assert.match(nodes.get('burner-cycles').innerHTML,/&lt;script&gt;/);
  assert.doesNotMatch(nodes.get('burner-cycles').innerHTML,/<script>|<b>/);
  assert.match(nodes.get('burner-detail').innerHTML,/Instructor &lt;note&gt;/);
  assert.match(nodes.get('burner-status').textContent,/RESIDUAL/);
  assert.equal(nodes.get('burner-enable').disabled,true);
  await vm.runInContext('renderBurner({mode:"off",configured_mode:"off"})',context);
  assert.equal(nodes.get('burner-shadow').disabled,true);  // never offers a mode above the configuration
  assert.match(nodes.get('burner-detail').innerHTML,/Just-in-time not evaluated/);
  await vm.runInContext('control("autostart_paper")',context);
  assert.match(nodes.get('notice').textContent,/Automatic paper start turned on/);
  await vm.runInContext('renderBurner({})',context);
  const before=posts.length;
  await vm.runInContext('control("burner_off")',context);
  assert.equal(posts.length,before+1);
  assert.equal(lastPath,'/api/burner');
  assert.equal(lastBody.mode,'off');
  // Ask panel: AI and app text is escaped; the deterministic fallback is labelled as such.
  await vm.runInContext('renderAsk({answer:"<img src=x>",relied_on:["<b>"],source:"ai",note:"<i>n</i>",point_to:"Reconcile"})',context);
  assert.match(nodes.get('ask-answer').innerHTML,/&lt;img src=x&gt;/);
  assert.doesNotMatch(nodes.get('ask-answer').innerHTML,/<img|<i>/);
  assert.match(nodes.get('ask-answer').innerHTML,/\[AI · advice only\].*See: Reconcile/);
  assert.equal(nodes.get('ask-refs').textContent,'Based on: <b>');  // textContent: never parsed as HTML
  await vm.runInContext('renderAsk({answer:"Blocked by: IBKR",relied_on:[],source:"deterministic"})',context);
  assert.match(nodes.get('ask-answer').innerHTML,/\[From the readiness checklist\]<\/strong> Blocked by: IBKR/);
  assert.equal(nodes.get('ask-refs').textContent,'');
  await vm.runInContext('renderAI({ai_enabled:false,ai_status:{model:"gemini-3.8-flash"},ai_features:{qa:{status:"AI off"}},qa_history:[{at:"2026-10-22T14:00:00Z",question:"<script>q</script>",answer:"<b>a</b>",source:"deterministic"}]})',context);
  assert.match(nodes.get('ask-history').innerHTML,/&lt;script&gt;q/);
  assert.doesNotMatch(nodes.get('ask-history').innerHTML,/<script>|<b>/);
  assert.equal(nodes.get('ask-status').textContent,'AI off · checklist answers');
  assert.match(nodes.get('ai-features').textContent,/Questions: AI off · One Enable AI switch governs all AI features/);
  await vm.runInContext('askQuestion("Why is paper trading not on?")',context);
  assert.equal(lastPath,'/api/ask');
  assert.deepEqual(lastBody,{question:'Why is paper trading not on?'});
  assert.equal(nodes.get('ask-submit').disabled,false);
  // The API close is the official close for this contest: no "import the statement" prompt.
  assert.equal(vm.runInContext('provenanceText({previous_close_provenance:"api",api_close_official:true})',context),"Today's closing target · IBKR API close (official)");
  assert.match(vm.runInContext('provenanceText({previous_close_provenance:"api",api_close_official:false})',context),/import the IBKR statement NAV/);
  // Day-end report: numbers render without any AI text; labels, notes and AI text are escaped.
  await vm.runInContext(`renderDayReport({ai_enabled:false,ai_features:{day_report:{status:'Waiting'}},day_reports:[{date:'2026-10-22',previous_date:'2026-10-21',status:'partial',official:{nav:'1000100',target:'1000095.5',miss:'4.5',score:'4.5'},drivers:[{key:'delta',label:'<b>Stock</b>',amount:-12.5},{key:'gamma',label:'Gamma',amount:null},{key:'miss',label:'= Miss',amount:4.5}],by_symbol:{'<i>RKLB</i>':{delta:1,gamma:null,total:1}},anomalies:[{item:'<img src=x>',detail:'outside'}],notes:['n<script>'],ai:null,ai_note:'AI off: numbers only'}]})`,context);
  for(const id of ['day-report-drivers','day-report-symbols','day-report-notes','day-report-ai','day-report-date'])assert.doesNotMatch(nodes.get(id).innerHTML,/<b>|<i>|<img|<script>/);
  assert.match(nodes.get('day-report-drivers').innerHTML,/&lt;b&gt;Stock.*-\$12\.50/);
  assert.match(nodes.get('day-report-drivers').innerHTML,/Gamma<\/td><td>—/);
  assert.match(nodes.get('day-report-symbols').innerHTML,/&lt;i&gt;RKLB/);
  assert.match(nodes.get('day-report-notes').innerHTML,/&lt;img src=x&gt;.*n&lt;script&gt;/);
  assert.match(nodes.get('day-report-summary').textContent,/2026-10-22 .*score \$4\.50 · partial data/);
  assert.match(nodes.get('day-report-ai').innerHTML,/AI off: numbers only/);
  assert.equal(nodes.get('day-report-explain').disabled,true);
  await vm.runInContext(`renderDayReport({ai_enabled:true,ai_features:{day_report:{}},day_reports:[{date:'2026-10-22',official:{},drivers:[],by_symbol:{},ai_for_earlier:true,ai:{status:'ok',model:'m<x>',at:'2026-10-22T20:30:00Z',explanation:'<script>e</script>',anomalies:[{item:'<b>i</b>',why:'w',check:'c'}],suggestions:[{label:'Burner off',reason:'<img src=y>'}]}}]})`,context);
  assert.match(nodes.get('day-report-ai').innerHTML,/AI explanation · advice only.*earlier version/);
  assert.match(nodes.get('day-report-ai').innerHTML,/Suggested for tomorrow \(not applied\): Burner off — &lt;img src=y&gt;/);
  assert.doesNotMatch(nodes.get('day-report-ai').innerHTML,/<script>|<b>|<img|<x>/);
  assert.equal(nodes.get('day-report-explain').disabled,false);
  await vm.runInContext(`renderDayReport({ai_enabled:true,ai_features:{day_report:{}},day_reports:[{date:'2026-10-22',official:{},drivers:[],ai:{status:'failed',error:'Google AI request failed (HTTP 500)'}}]})`,context);
  assert.match(nodes.get('day-report-ai').innerHTML,/unavailable: Google AI request failed \(HTTP 500\)\. The numbers above are unaffected/);
  await vm.runInContext('control("day_report_compute")',context);
  assert.equal(lastPath,'/api/day-report');
  assert.deepEqual(lastBody,{date:null,action:'compute'});
  assert.match(nodes.get('notice').textContent,/Day-end report recomputed/);
  await vm.runInContext('control("day_report_explain")',context);
  assert.deepEqual(lastBody,{date:null,action:'explain'});
  console.log('Dashboard controls: expired token refresh, server rejection, pause, shadow, Ask panel and day-end report passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
