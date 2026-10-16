"use strict";
let state=null,pendingAction=null;
const $=id=>document.getElementById(id);
let token=document.querySelector('meta[name="control-token"]').content;
const money=value=>value===null||value===undefined?'—':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(Number(value));
const stamp=value=>!value?'—':new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',timeZoneName:'short'}).format(new Date(value.endsWith('Z')||/[+-]\d\d:\d\d$/.test(value)?value:value+'Z'));
const esc=value=>String(value??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const label=id=>state?.positions.find(p=>p.con_id===id)?.label||state?.contract_labels?.[id]||`conId ${id}`;
function rows(id,items,render,cols){$(id).innerHTML=items.length?items.map(render).join(''):`<tr><td colspan="${cols}" class="empty">No records yet</td></tr>`;}
function badge(id,text,good){$(id).textContent=text;$(id).className='badge '+(good?'good':'bad');}
function notice(text,error=true){$('notice').textContent=text;$('notice').hidden=false;$('notice').className='notice'+(error?'':' success');}
async function api(path,body){
  const send=()=>fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':token},body:JSON.stringify(body),signal:AbortSignal.timeout(30000)});
  let response=await send();
  if(response.status===403){
    // The server rejects invalid tokens before dispatching any control action.
    // Refresh once from the same-origin page; never retry uncertain submissions.
    const page=await fetch('/',{cache:'no-store',signal:AbortSignal.timeout(10000)});
    if(!page.ok)throw Error('Could not refresh dashboard authorization. Reload this page.');
    const fresh=new DOMParser().parseFromString(await page.text(),'text/html').querySelector('meta[name="control-token"]')?.content;
    if(!fresh)throw Error('Dashboard authorization unavailable. Reload this page.');
    token=fresh;
    response=await send();
  }
  const raw=await response.text();
  let result;try{result=JSON.parse(raw);}catch{throw Error(raw||`Dashboard request failed (HTTP ${response.status}).`);}
  if(!response.ok)throw Error(typeof result.detail==='string'?result.detail:JSON.stringify(result.detail||result));
  return result;
}
async function control(action){
  notice(`Sending ${action} request…`,false);
  try{if(action.startsWith('recovery_')){await api('/api/recovery',{mode:action.slice(9)});}else{await api('/api/control',{action});}notice(action==='pause'?'Pause accepted. Scheduled purchases are disarmed; filled holdings are retained.':action==='shadow'?'Shadow mode accepted. Scheduled purchases are disarmed; no orders will be submitted.':`${action} request accepted.`,false);await refresh();}
  catch(e){notice(e.name==='TimeoutError'?'Control request timed out. Check the current mode before retrying.':e.message);}
}
function renderRecoveryAvailability(rec){
  const validation=rec.promotion||{};
  const eligible=validation.eligible===true;
  const button=$('recovery-enable');
  button.disabled=!eligible||rec.mode==='paper';
  button.textContent=rec.mode==='paper'?'Paper recovery enabled':eligible?'Enable paper recovery':'Paper recovery unavailable';
  button.title=validation.status||'Recovery validation status unavailable';
  $('recovery-availability').textContent=`${validation.status||'Recovery validation status unavailable'} · ${validation.independent_test_windows??0}/20 required held-out contest windows. Shadow recovery submits no recovery orders; ordinary paper hedging is controlled separately.`;
  $('recovery-validation').innerHTML=eligible?'<p>Promotion evidence passed. Current quotes, signals and execution risk checks still apply.</p>':`<ul>${(validation.blockers||['Waiting for validation status']).map(reason=>`<li>${esc(reason)}</li>`).join('')}</ul>`;
}
function renderContinuity(s){
  const c=s.continuity||{}, el=$('continuity-status');
  el.hidden=!c.automatic_resume_pending&&!c.pause_reason;
  el.textContent=c.automatic_resume_pending?`Paper execution is waiting: ${c.wait?.reason||'data or broker state'}. It resumes automatically after reconciliation and all freshness/risk checks pass. Pause trading disables automatic resume.`:c.pause_reason?`Paused: ${c.pause_reason}. Automatic resume is disabled.`:'';
}
function solverSummary(m){
  if(!m||!Number.isFinite(Number(m.total_ms)))return '';
  const timing=(Number(m.total_ms)/1000).toFixed(2);
  const accepted=m.accepted_stage?` · Accepted ${esc(m.accepted_stage)}`:'';
  const ranking=m.accepted_stage?` · ${m.ranking_complete?'Ranking completed':'Certified target retained; ranking incomplete'}`:'';
  return `<p class="muted">Optimizer ${timing}s${accepted}${ranking}</p>`;
}
function render(s){state=s;$('contest-name').textContent=s.contest;$('demo-banner').hidden=!s.demo;$('mode').textContent=s.demo?`${s.rehearsal?'REHEARSAL':'DEMO'} / ${s.mode}`:s.mode;$('clock').textContent=stamp(s.clock);$('health').textContent=s.health;$('health-dot').className='dot '+(s.ready?'good':'bad');$('connection-dot').className='dot '+(s.connected?'good':'bad');$('connection-label').textContent=s.connected?(s.demo?'Simulator connected':s.market_data_errors?.length?'IBKR connected · live data unavailable':'IBKR connected'):'Disconnected';$('account').textContent=`${s.account} · IBKR API observation ${stamp(s.account_nav_at)}`;$('nav').textContent=money(s.nav);$('api-nav').textContent=s.official_account_nav?`Recorded closing NAV: ${money(s.official_account_nav.nav)} · ${s.official_account_nav.date} · ${s.official_account_nav.source}`:'Closing NAV not yet recorded';$('previous').textContent=money(s.previous_close);$('daily').textContent=money(s.provisional_score);$('adjusted-target').textContent=`Adjusted closing target: ${money(s.closing_target)} · ${s.scores.scoring_rule==='actual_daily_cash_rate'?(s.closing_target_estimated?'ESTIMATED using approved approximate rate; official score awaits actual rate':'Actual recorded daily cash rate'):s.scores.annual_rate ? 'Annual benchmark '+(Number(s.scores.annual_rate)*100).toFixed(2)+'%' : 'Rate not configured'}`;$('score').textContent=money(s.scores.total);$('score-status').textContent=s.scores.scoring_rule==='pending_interest_adjustment'?s.scores.status:s.scores.complete?'All closing inputs recorded · see NAV provenance':['interest_benchmark','actual_daily_cash_rate'].includes(s.scores.scoring_rule)?s.scores.status:'Partial ledger · missing closes remain unscored';$('profile').textContent=s.profile;
renderContinuity(s);
const closing=s.closing_recorder||{};$('closing-recorder').textContent=`${closing.enabled?'Enabled':'Disabled'} · ${closing.status||'Waiting'} · Next close ${stamp(closing.next_close)}. Keep this Mac awake and Paper TWS connected. API observations can be revised from the IBKR statement.`;
$('market-provider').textContent=`${s.market_data_provider} · ${s.market_data_execution_verified===false?'Not executable: streaming freshness or quote rights unverified.':'Data status governed by risk checks.'} Collector receipt ${stamp(s.market_data_received_at)}. ${(s.market_data_notes||[]).join('; ')} ${Object.values(s.unavailable_instruments||{}).join('; ')}`;
rows('market-quote-rows',s.market_quotes||[],x=>`<tr><td>${esc(x.label)}</td><td>${money(x.bid)}</td><td>${money(x.ask)}</td><td>${stamp(x.at)}</td><td>${esc(x.data_type)}</td></tr>`,5);
const r=s.risk?.metrics||{};$('stress').textContent=money(r.stress_loss);$('stress-limit').textContent=money(r.stress_limit);$('gross').textContent=money(r.gross_notional);$('liquidity').textContent=money(r.excess_liquidity_estimate);badge('risk-badge',s.risk?(s.risk.passed?'WITHIN LIMITS':'CHECK REQUIRED'):'WAITING',s.risk?.passed);$('risk-meter').style.width=`${Math.min(100,(r.stress_loss||0)/(r.stress_limit||1)*100)}%`;
const rec=s.recovery||{};renderRecoveryAvailability(rec);$('recovery-mode').textContent=(rec.mode||'off').toUpperCase();$('recovery-status').textContent=rec.status||'Not evaluated';$('recovery-detail').textContent=`Recovery stress budget ${money(rec.stress_budget)} · trading loss stop ${money(rec.loss_stop)} · attributed trading P&L ${money(rec.pnl?.daily_trading_pnl)} (financing remains in combined NAV)`;rows('recovery-signals',rec.forecasts||[],f=>`<tr><td>${esc(f.symbol)}</td><td>${esc(f.strategy)}</td><td>${(100*f.mean_return).toFixed(3)}%</td><td>${(100*f.conservative_return).toFixed(3)}%</td><td>${stamp(f.bar_at)}</td></tr>`,5);
const d=s.decisions[0];badge('decision-badge',d?.status||'WAITING',d?.trade_eligible);$('improvement').textContent=money(d?.improvement);$('threshold').textContent=money(d?.threshold);$('decision-text').textContent=d?.explanation||'Start shadow mode to calculate proposals.';$('decision-gap').textContent=money(d?.nav_gap);$('decision-distances').textContent=`${money(d?.hold_distance)} / ${money(d?.proposed_distance)}`;$('decision-blocker').textContent=(d?.blocking_reasons||[]).join('; ');
rows('score-rows',s.scores.days,x=>`<tr><td>${esc(x.date)}</td><td>${esc(x.previous_date)}</td><td>${money(x.nav)}</td><td>${x.score===null?'Awaiting official NAV':money(x.score)}</td></tr>`,4);
rows('position-rows',s.positions,x=>`<tr><td>${esc(x.label)}</td><td><span class="badge ${x.role==='MANDATORY'?'good':''}">${esc(x.role)}</span></td><td>${x.quantity}</td><td>${money(x.mark)}</td><td>${money(x.value)}</td></tr>`,5);
rows('order-rows',s.orders,x=>`<tr><td>${stamp(x.created_at)}</td><td>${esc(label(x.con_id))}</td><td>${x.signed_quantity>0?'BUY':'SELL'} ${Math.abs(x.signed_quantity)}</td><td>${esc(x.order_type||'LMT')} · ${x.package_id?'Net package '+money(x.net_limit):money(x.limit_price)}${x.order_type==='MKT'?' reference':''}<small>${esc(x.urgency||'')}${x.initial_limit&&Number(x.initial_limit)!==Number(x.limit_price)?' · Initial '+money(x.initial_limit):''}</small></td><td><span class="badge ${x.state==='UNKNOWN'?'bad':''}">${esc(x.state)}</span></td><td><a href="#" class="decision-link" data-id="${esc(x.decision_id)}">${esc(x.decision_id.slice(0,8))} ↗</a></td></tr>`,6);
rows('quality-rows',s.execution_quality||[],x=>`<tr><td>${esc(label(x.con_id))}<small>${esc(x.intent_id.slice(0,8))} · ${esc(x.state)}</small></td><td>${x.filled_quantity} / ${x.requested_quantity}${x.quantity_anomaly?' · CHECK':''}</td><td>${x.first_fill_seconds===null?'—':Number(x.first_fill_seconds).toFixed(2)} / ${x.weighted_fill_seconds===null?'—':Number(x.weighted_fill_seconds).toFixed(2)}</td><td>${money(x.decision_slippage_usd)} / ${money(x.arrival_slippage_usd)}</td><td>${money(x.known_fees_usd)} · ${x.fees_complete?'complete':'pending / non-USD'}</td><td>${x.amendment_attempts} / ${x.cancel_attempts}</td></tr>`,6);
rows('fill-rows',s.fills,x=>`<tr><td>${stamp(x.at)}</td><td>${esc(label(x.con_id))}</td><td>${x.signed_quantity>0?'BUY':'SELL'} ${Math.abs(x.signed_quantity)}</td><td>${money(x.price)}</td><td>${x.commission===null?'Pending':money(x.commission)} ${esc(x.commission_currency||'')}</td><td>${esc(x.exec_id)}${x.correction_of?' · correction':''}</td></tr>`,6);
rows('package-report-rows',s.package_executions||[],x=>`<tr><td>${esc(x.order_ref)}</td><td>${money(x.net_price)}</td><td>${money((s.package_commissions||[]).find(f=>f.exec_id===x.exec_id)?.amount)}</td></tr>`,3);
$('transition-list').innerHTML=s.transitions.length?s.transitions.map(x=>`<div class="log-entry"><small>${stamp(x.at)}</small> <strong>${esc(x.state)}</strong> · ${esc(x.intent_id?.slice(0,8))}</div>`).join(''):'<p class="muted">Broker acknowledgments and cancellation confirmations will appear here.</p>';
$('factor-status').textContent=`${s.risk_factors?.beta_source||'Beta unavailable'} · ${s.risk_factors?.rho_source||'Rho unavailable'} · Coverage 1 = complete, 0 = unavailable; missing factors are not zero exposure.`;
$('greeks').innerHTML=Object.entries(r).filter(([k])=>k.includes(':')).map(([k,v])=>`<div><small>${esc(k)}</small><strong>${Number(v).toLocaleString('en-US',{maximumFractionDigits:2})}</strong></div>`).join('');$('risk-reasons').innerHTML=(s.risk?.reasons||[]).map(x=>`<li>${esc(x)}</li>`).join('');
$('decision-list').innerHTML=s.decisions.length?s.decisions.map(x=>`<div class="log-entry" id="decision-${esc(x.id)}"><div class="row"><strong>${esc(x.status)} · ${esc(x.profile||'Day 0')}</strong><small>${stamp(x.at)}</small></div><p>${esc(x.explanation)}</p>${solverSummary(x.solver_metrics)}<div class="row"><span>Expected score ${money(x.expected_score)} · Improvement ${money(x.improvement)}</span><span>${x.trade_eligible?'Eligible':'No trade'}</span></div><p>${esc((x.risk_reasons||[]).join('; '))}</p><small>Snapshot ${esc(x.snapshot_id?.slice(0,12))} · Scenario ${esc(x.scenario_id?.slice(0,12))}</small></div>`).join(''):'<p class="muted">No decisions yet. Start shadow mode.</p>';
$('ai-status').textContent=`${s.ai_status.provider} · ${s.ai_status.model} · ${s.ai_enabled?'Enabled':'Disabled'} · ${s.ai_status.configured?'Key configured':'Local key needed'} · ${s.ai_status.status}`;
if(document.activeElement!==$('ai-model'))$('ai-model').value=s.ai_status.model;
$('ai-toggle').textContent=s.ai_enabled?'Disable AI':'Enable AI';
$('ai-toggle').disabled=s.ai_status.busy||!s.ai_status.configured;
$('ai-apply').disabled=s.ai_status.busy;
$('ai-review').textContent=(s.ai[0]?.stale?'Historical review of an earlier snapshot: ':'')+(s.ai[0]?.explanation||(s.ai_enabled?'Supervisor enabled; waiting for scheduled review.':'AI disabled. Deterministic monitoring remains active.'));
if(document.activeElement!==$('profile-select')){$('profile-select').innerHTML=s.profiles.map(p=>`<option ${p===s.profile?'selected':''}>${esc(p)}</option>`).join('');}
$('settings-values').innerHTML=[['Stock optimization',s.policy.stock_seconds+' seconds'],['Option optimization',s.policy.option_seconds+' seconds'],['Score threshold',money(s.policy.score_threshold)],['Minimum ordinary stock slice',money(s.execution_policy?.minimum_stock_notional)],['Order urgency','Patient limits; eligible urgent stock markets'],['Day 0 opening wait',(s.setup.opening_wait_minutes??5)+' minutes'],['Stress budget',s.limits.stress_loss*100+'%'],['Configuration',s.config_hash.slice(0,12)],['Readiness',s.ready?'Ready':'Blocked / waiting']].map(([k,v])=>`<div class="row"><span class="muted">${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('');
$('alert-list').innerHTML=s.alerts.length?s.alerts.map(x=>`<div class="log-entry"><span class="badge ${x.severity==='error'?'bad':''}">${esc(x.severity)}</span> <small>${stamp(x.at)}</small><p>${esc(x.message)}</p></div>`).join(''):'<p class="muted">No alerts.</p>';
$('interest-values').innerHTML=[['Cash balance',money(s.cash)],['Accrued interest already in NAV',money(s.accrued_cash)],['Future annual cash rate',`${(s.interest.annual_rate*100).toFixed(3)}% · ${s.interest.rate_status}`],['Rate provenance',s.interest.rate_source||'Unconfigured'],['Forecast convention',s.interest.note]].map(([k,v])=>`<div class="row"><span class="muted">${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('');
rows('setup-rows',s.setup.holdings,x=>`<tr><td>${esc(x.label)}</td><td>${x.required}</td><td>${x.held}</td><td>${x.working}</td></tr>`,4);
$('baseline-reference').textContent=s.baseline_reference.length?`Unverified baseline reference: ${money(s.baseline_reference[0].nav)}. Official closing report still required.`:'';
$('setup-plan').innerHTML=s.setup.plan?'<p>Initial portfolio plan prepared:</p><ul>'+Object.entries(s.setup.plan).map(([id,qty])=>`<li>${esc(label(Number(id)))}: ${qty}</li>`).join('')+'</ul>':'A complete mandatory-plus-hedge plan is required before buying. Setup waits for narrow live quotes after the opening stabilization period; every new mandatory option arrives with a matched option hedge.';
$('setup-status').textContent=`${s.rehearsal?'SYNTHETIC rehearsal · ':''}Day 0 baseline: ${s.day_zero}. Purchase session: ${s.purchase_session}. ${s.setup.complete?'Mandatory holdings complete and protected.':'Purchase missing mandatory holdings before ordinary hedging.'} Scored sessions: ${s.scoring_sessions.join(', ')}.`;
$('setup-baseline').textContent=(!s.setup.complete&&!s.setup.window_open?'Purchase window is not open. Scheduled purchases wait for the configured session. ':'')+(s.setup.baseline_recorded?'Day 0 closing baseline recorded.':'Awaiting Day 0 official closing NAV; no score is assigned to Day 0. Day 1 purchase costs affect the first scored close.');
$('arm-setup').disabled=!s.connected||!s.confirmed||s.setup.complete||s.pause_pending||s.setup.armed;
$('arm-setup').textContent=s.setup.armed?'Patient setup armed':'Arm patient Day 0 setup + hedging';
$('setup').disabled=!s.connected||!s.confirmed||!s.setup.window_open||s.setup.complete||s.pause_pending;
$('enable').disabled=!s.connected||!s.confirmed||s.pause_pending||s.continuity?.automatic_resume_pending;$('enable').textContent=s.continuity?.automatic_resume_pending?'Waiting · automatic resume enabled':s.demo?'Enable simulated trading':'Enable paper trading';$('freshness').textContent=`${s.market_data_provider} · ${s.quote_count} quotes · Last quote ${stamp(s.last_quote_at)} · Times shown in ET`;
}
function show(view){document.querySelectorAll('.view').forEach(x=>x.hidden=x.id!==view);document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x.dataset.view===view));$('page-title').textContent={overview:'Portfolio overview',positions:'Positions & risk',trades:'Trades & executions',decisions:'Hedge decisions',settings:'Settings & alerts'}[view];}
async function refresh(){try{const response=await fetch('/api/state');if(!response.ok)throw Error('Dashboard service unavailable');render(await response.json());}catch(e){$('connection-label').textContent='Service unreachable';$('health').textContent='Dashboard connection lost. Check the local service before issuing controls.';}}
document.querySelectorAll('.nav').forEach(b=>b.addEventListener('click',()=>show(b.dataset.view)));
document.querySelectorAll('[data-action]').forEach(b=>b.addEventListener('click',()=>{const action=b.dataset.action;if(!state&&action!=='pause'){notice('Dashboard state is loading. Please wait or reload.');return;}if(action==='paper'||action==='setup'||action==='arm_setup'||action==='recovery_paper'){pendingAction=action;$('confirm-text').textContent=action==='recovery_paper'?'Enable a validated recovery model alongside hedging. Matching quote-based contest evidence, recovery budgets, and all portfolio limits are required.':action==='arm_setup'?`Schedule purchases of missing mandatory positions in ${state.account} for ${state.purchase_session} throughout its setup window. First plan the mandatory positions with protective option and stock hedges; use guaranteed matched option packages, then reconcile. Keep this service running; restart requires re-arming.`:action==='setup'?`Start the complete protected setup in ${state.account} on ${state.purchase_session}. Existing stock hedge preparation and guaranteed mandatory-plus-option-hedge pairs must pass risk and spread checks. No naked mandatory purchases or market fallback.`:`Allow the deterministic service to submit ${state.demo?'synthetic':'IBKR paper'} hedge orders in ${state.account}. Filled positions remain held when you pause.`;$('confirm-yes').textContent=action==='arm_setup'?'Arm patient setup':action==='setup'?'Begin purchases':'Enable';$('confirm').showModal();}else control(action);}));
$('confirm-cancel').addEventListener('click',()=>$('confirm').close());$('confirm-yes').addEventListener('click',()=>{$('confirm').close();control(pendingAction);});
$('profile-apply').addEventListener('click',async()=>{try{await api('/api/control',{action:'profile',profile:$('profile-select').value});notice('Profile applied in shadow mode.',false);await refresh();}catch(e){notice(e.message);}});
$('close-form').addEventListener('submit',async e=>{e.preventDefault();const form=new FormData(e.target);try{await api('/api/closes',{date:form.get('date'),nav:form.get('nav'),source:form.get('source'),account:state.account});notice('Official close recorded. Adjacent daily scores recalculated.',false);await refresh();}catch(e){notice(e.message);}});
document.addEventListener('click',e=>{const link=e.target.closest('.decision-link');if(link){e.preventDefault();show('decisions');document.getElementById('decision-'+link.dataset.id)?.scrollIntoView({behavior:'smooth'});}});
refresh();setInterval(refresh,2000);


async function setAI(enabled){try{await api('/api/ai/settings',{model:$('ai-model').value,enabled});notice('AI settings saved; trading state unchanged.',false);await refresh();}catch(e){notice(e.message);}}
$('ai-apply').addEventListener('click',()=>setAI(state.ai_enabled));
$('ai-toggle').addEventListener('click',()=>setAI(!state.ai_enabled));

$('daily-rate-form').addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(e.target);try{await api('/api/daily-cash-rate',{date:f.get('date'),daily_rate:f.get('daily_rate'),source:f.get('source')});notice('Daily benchmark recorded; scores recomputed.',false);await refresh();}catch(e){notice(e.message);}});
