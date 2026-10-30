const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
 chat:'M4 4h16v12H9l-5 4V4Z', chart:'M4 19V5m0 14h16M8 15v-4m5 4V7m5 8V4', upload:'M12 16V3m-5 5 5-5 5 5M4 15v5h16v-5', download:'M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4', settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M19 5l-1.5 1.5m-11 11L5 19', search:'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm5 12 6 6', layers:'m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5', arrow:'M5 12h14m-5-5 5 5-5 5', left:'m14 6-6 6 6 6', right:'m9 6 6 6-6 6', close:'m6 6 12 12M18 6 6 18', list:'M8 6h12M8 12h12M8 18h12M3 6h1M3 12h1M3 18h1', database:'M4 6c0-4 16-4 16 0s-16 4-16 0Zm0 0v12c0 4 16 4 16 0V6M4 12c0 4 16 4 16 0', spark:'m12 2 2.7 7.3L22 12l-7.3 2.7L12 22l-2.7-7.3L2 12l7.3-2.7L12 2Z', check:'m5 12 4 4L19 6', clock:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4v5l3 2', warning:'m12 3 10 18H2L12 3Zm0 6v5m0 3v.1', copy:'M9 9h12v12H9V9ZM5 15H3V3h12v2', shield:'m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Zm-4 9 3 3 5-5', pause:'M8 5v14M16 5v14'
};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.chat}"/></svg>`;
function hydrateIcons(){ document.querySelectorAll('[data-icon]').forEach(e => e.innerHTML = icon(e.dataset.icon)); }
let rows=[], imported=[], gold=[], selected=0, state={}, evidence=[], questionDate='2023/05/30 (Tue) 08:08';
let draftEvidence=[], evidenceMode='fields';
let initialized=false, lastSignature='', view='transactions', detailTab='conversation', pending=null, toastTimer;
const outcome = r => r.status !== 'answered' ? 'failed' : r.abstained ? 'abstained' : 'answered';
const badge = r => `<span class="badge ${outcome(r)==='abstained'?'warn':outcome(r)==='failed'?'bad':''}">${icon(outcome(r)==='abstained'?'pause':outcome(r)==='failed'?'close':'check')}${esc(outcome(r)==='abstained'?'Abstained':outcome(r)==='failed'?'Failed':'Answered')}</span>`;
const pct = (a,b) => b ? Math.round(a/b*100)+'%' : '—';
const norm = s => String(s ?? '').replace(/\[E\d+\]/g,'').toLowerCase().replace(/[^\p{L}\p{N}\s]/gu,' ').replace(/\s+/g,' ').trim();
const formatDate = s => { if(!s) return 'No question date'; const match=s.match(/^(\d{4})[/-](\d{2})[/-](\d{2})/); return match ? new Date(+match[1],+match[2]-1,+match[3]).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}) : s; };
const citationsOf = r => (r?.citations || []).map(c => typeof c==='string'?c:c?.id).filter(Boolean);
function error(message){ $('#error').textContent=message||''; $('#error').classList.toggle('hidden',!message); }
function toast(message){ clearTimeout(toastTimer); $('#toast').textContent=message; $('#toast').classList.remove('hidden'); toastTimer=setTimeout(()=>$('#toast').classList.add('hidden'),3500); }
function metric(label,value,hint,symbol,warn=false){return `<div class="stat ${warn?'warn':''}"><label>${label}</label><strong>${value}</strong><span class="stat-icon" data-icon="${symbol}"></span><small>${hint}</small></div>`;}
function evaluate(predictions, references){
 let exact=0, abstain=0, f1=0, answerable=0, matched=0;
 for(const g of references){const p=predictions.find(r=>r.question_id===g.question_id&&r.status==='answered');if(p)matched++;
  if(p&&Boolean(p.abstained)===g.abstain)abstain++;
  if(!g.abstain){answerable++;if(p&&!p.abstained&&[g.answer,...(g.aliases||[])].some(a=>norm(a)===norm(p.answer)))exact++;
   const pred=new Set(citationsOf(p)), truth=new Set(g.evidence||[]);const overlap=[...pred].filter(id=>truth.has(id)).length;f1+=(pred.size+truth.size)?2*overlap/(pred.size+truth.size):0;}
 }return {exact,abstain,f1,answerable,matched};
}
function render(){
 const good=rows.filter(r=>r.status==='answered'), lat=good.filter(r=>Number.isFinite(r.latency_s)).map(r=>r.latency_s), nonabs=good.filter(r=>!r.abstained), cited=nonabs.filter(r=>r.citations?.length);
 $('#stats').innerHTML=metric('Transactions',rows.length,`${good.length} completed · ${rows.length-good.length} failed`,'chat')+metric('Avg. latency',lat.length?(lat.reduce((a,b)=>a+b,0)/lat.length).toFixed(1)+'<em>s</em>':'—',`${lat.length} measured responses`,'clock')+metric('Citation coverage',pct(cited.length,nonabs.length),'Non-abstained answers','layers',nonabs.length>cited.length)+metric('Abstention rate',pct(good.filter(r=>r.abstained).length,good.length),'Among completed responses','shield');
 renderList();renderDetail();renderEvaluation();hydrateIcons();
 $('#footer').textContent=`${rows.length} transactions · ${gold.length} references · Stored locally`;
 $('#listFooter').textContent=imported.length?`${imported.length} imported · ${state.rows?.length||0} local`:'Synced with local results';
}
function filtered(){const q=$('#search').value.toLowerCase(), f=$('#filter').value;return rows.map((r,i)=>({r,i})).filter(({r})=>(f==='all'||outcome(r)===f||(f==='uncited'&&r.status==='answered'&&!r.abstained&&!r.citations?.length))&&[r.question,r.answer,r.question_id].some(s=>String(s||'').toLowerCase().includes(q)));}
function renderList(){
 const matches=filtered();$('#count').textContent=rows.length;
 $('#rows').innerHTML=matches.map(({r,i})=>`<button class="row ${i===selected?'selected':''}" data-index="${i}" aria-current="${i===selected?'true':'false'}"><span class="row-top">${badge(r)}<small class="row-time">${Number.isFinite(r.latency_s)?r.latency_s.toFixed(1)+'s':'—'}</small></span><strong>${esc(r.question||r.question_id||'Untitled question')}</strong><span class="row-bottom"><small>${esc(r.question_id)}</small>${r.status==='answered'&&!r.abstained&&!r.citations?.length?'<span class="warning-dot" title="No final-answer citations"></span>':''}</span></button>`).join('')||`<div class="empty">${icon('search')}<strong>No matching transactions</strong><p>Try another search or outcome filter.</p><button class="button" id="clearFilters">Clear filters</button></div>`;
 document.querySelectorAll('[data-index]').forEach(b=>b.onclick=()=>selectRow(+b.dataset.index));
 if($('#clearFilters'))$('#clearFilters').onclick=()=>{$('#search').value='';$('#filter').value='all';renderList()};
}
function selectRow(index){selected=Math.max(0,Math.min(rows.length-1,index));detailTab='conversation';$('#transactions').classList.remove('show-list');renderList();renderDetail();$('#conversation').scrollTop=0;}
function emptyState(title,description,symbol='layers'){return `<div class="empty">${icon(symbol)}<strong>${title}</strong><p>${description}</p></div>`;}
function renderDetail(){
 const r=rows[selected];$('#detailtitle').textContent=r?.question_id||'New conversation';$('#position').textContent=`${rows.length?selected+1:0} / ${rows.length}`;
 $('#previous').disabled=selected<=0;$('#next').disabled=selected>=rows.length-1;
 $('#evidenceBadge').textContent=r?.evidence?.length??'—';
 document.querySelectorAll('[data-tab]').forEach(b=>{b.classList.toggle('active',b.dataset.tab===detailTab);b.setAttribute('aria-selected',String(b.dataset.tab===detailTab));b.tabIndex=b.dataset.tab===detailTab?0:-1;});
 let content='';
 if(!r)content=emptyState('Your next insight starts here.','Ask a question below, or import Reactor results to explore an existing run.','spark');
 else if(detailTab==='conversation'){
  const text=r.answer||r.error||r.code||'No final answer received';
  content=`<div class="date-separator">${esc(formatDate(r.question_date))} · QUESTION DATE</div><div class="message"><span class="speaker-avatar">Y</span><div><div class="speaker">You <small>Question</small></div><div class="question-bubble">${esc(r.question||'No question text stored')}</div></div></div><div class="message"><span class="speaker-avatar agent">${icon('spark')}</span><div><div class="speaker">Reactor ${badge(r)}</div><div class="answer-card ${r.abstained?'abstained':''}"><div class="answer-label">${r.abstained?'EVIDENCE CHECK':r.status==='answered'?'FINAL ANSWER':'REQUEST OUTCOME'}</div><strong>${esc(r.abstained?'Not enough evidence':text)}</strong>${r.abstained?'<p>The supplied evidence doesn’t contain an answer.</p>':''}</div><div class="answer-tools"><span>${Number.isFinite(r.latency_s)?r.latency_s.toFixed(1)+'s response time':''}${r.citations?.length?' · '+r.citations.length+' citation(s)':''}</span><button class="button subtle" id="copyAnswer" aria-label="Copy answer">${icon('copy')} Copy</button></div>${r.status==='answered'&&!r.abstained&&!r.citations?.length?`<div class="note">${icon('warning')}<div><strong>Answer needs a citation</strong>No evidence reference was included in the final-answer line.</div></div>`:''}${r.reply?`<details><summary>Read full agent response</summary><div class="explanation">${esc(r.reply)}</div></details>`:''}</div></div>`;
 } else if(detailTab==='evidence'){
  const cited=new Set(citationsOf(r));content=`<div class="detail-heading"><h3>Evidence & provenance</h3><span class="badge neutral">${cited.size} cited session(s)</span></div>`;
  if(r.evidence?.length){content+='<p class="detail-intro">The exact excerpts sent with this question.</p>'+r.evidence.map((e,i)=>`<article class="evidence-card"><div class="evidence-head"><div><span class="badge ${cited.has(e.id)?'':'neutral'}">E${i+1} · ${cited.has(e.id)?'Cited':'Not cited'}</span><small style="display:block;margin-top:5px">${esc(e.id)}</small></div><small>${esc(formatDate(e.date))}</small></div><p>${esc(e.text)}</p></article>`).join('')+'<button class="button" id="reuseEvidence">'+icon('layers')+' Use these excerpts in the next question</button>';}
  else content+=emptyState('Evidence wasn’t saved with this result.','Older client logs contain citations but not the original excerpts. New dashboard requests save both.');
  if(cited.size)content+=`<h3 style="font-size:11px;margin:20px 0 10px">Cited session IDs</h3><pre>${esc([...cited].join('\n'))}</pre>`;
 } else if(detailTab==='trace'){
  content=`<div class="detail-heading"><h3>Request diagnostics</h3>${badge(r)}</div><p class="detail-intro">Values reported by the Reactor client. No estimated processing stages.</p><div class="trace-grid">${[['Agent latency',Number.isFinite(r.latency_s)?r.latency_s.toFixed(1)+'s':'—'],['Retries',r.retries??'—'],['Agent cycle',r.cycle??'—'],['Final citations',r.citations?.length||0]].map(([a,b])=>`<div class="trace-cell"><small>${a}</small><strong>${esc(b)}</strong></div>`).join('')}</div><div class="kv"><span>Question reference</span><code class="mono">${esc(r.ref||'—')}</code></div><div class="kv"><span>Interim replies</span><b>${r.other_replies?.length||0}</b></div><div class="kv"><span>Late replies</span><b>${r.late_replies?.length||0}</b></div><div class="kv"><span>Unknown citations</span><b>${r.unknown_citations?.length||0}</b></div><p class="detail-intro" style="margin-top:20px">Reactor history</p><pre>${esc(r.history_id||'No history recorded')}</pre>${r.error?`<div class="note">${esc(r.error)}</div>`:''}`;
 } else content=`<div class="detail-heading"><h3>Raw transaction</h3><button class="button subtle" id="copyJSON">${icon('copy')} Copy JSON</button></div><pre>${esc(JSON.stringify(r,null,2))}</pre>`;
 const reference=gold.find(g=>g.question_id===r?.question_id)||r?.reference;
 if(reference&&detailTab==='conversation'){
  const hasResponse=r.status==='answered';
  const exact=hasResponse&&!r.abstained&&!reference.abstain&&[reference.answer,...(reference.aliases||[])].some(a=>norm(a)===norm(r.answer));
  const correctAbstention=hasResponse&&r.abstained&&reference.abstain;
  const label=!hasResponse?'No completed answer':correctAbstention?'Correct abstention':reference.abstain?'Expected an abstention':r.abstained?'Expected an answer':exact?'Exact answer match':'No exact match';
  content+=`<div class="reference-card"><div class="reference-top"><span class="eyebrow">REFERENCE COMPARISON</span><span class="badge ${exact||correctAbstention?'':'warn'}">${label}</span></div><p>${esc(reference.abstain?'The reference marks this question as unanswerable.':reference.answer)}</p><small>${reference.abstain?'Abstention is scored against the reference decision.':'Text matching is a diagnostic; semantic correctness needs a judge.'}</small></div>`;
 }
 if(pending&&detailTab==='conversation')content+=`<div class="message"><span class="speaker-avatar">Y</span><div><div class="speaker">You <small>Sending now</small></div><div class="question-bubble">${esc(pending.question)}</div></div></div><div class="message"><span class="speaker-avatar agent">${icon('spark')}</span><div><div class="speaker">Reactor <small>Waiting for an answer</small></div><div class="pending-dots"><i></i><i></i><i></i></div><p class="detail-intro">Replies usually take tens of seconds. You can inspect other transactions while you wait.</p></div></div>`;
 if(r?.pipeline&&detailTab==='trace')content+=`<h3 style="margin:20px 0 12px;font-size:12px">Harness pipeline</h3><pre>${esc(JSON.stringify({harness:r.pipeline.harness,stages:r.pipeline.stages,metrics:r.evaluation},null,2))}</pre><details style="margin-top:16px"><summary>Prepared prompt</summary><pre>${esc(r.prepared_prompt||r.question)}</pre></details><details style="margin-top:12px"><summary>Exact outgoing Reactor message</summary><pre>${esc(JSON.stringify(r.pipeline.outbound_message,null,2))}</pre></details>`;
 $('#conversation').innerHTML=content;
 if($('#copyAnswer'))$('#copyAnswer').onclick=()=>copy(r.answer||r.error||'', 'Answer copied');
 if($('#copyJSON'))$('#copyJSON').onclick=()=>copy(JSON.stringify(r,null,2),'Transaction JSON copied');
 if($('#reuseEvidence'))$('#reuseEvidence').onclick=()=>{evidence=structuredClone(r.evidence);questionDate=r.question_date||'';updateComposer();toast('Evidence attached to your next question')};
 const missing=r?.status==='answered'&&!r.abstained&&!r.citations?.length;
 $('#inspector').innerHTML=`<section><h3>At a glance</h3><div class="inspection-status">${icon(r?.status==='answered'?'check':'clock')}${r?.status==='answered'?'Response collected':'No completed response'}</div><div class="kv"><span>Response time</span><b>${Number.isFinite(r?.latency_s)?r.latency_s.toFixed(1)+'s':'—'}</b></div><div class="kv"><span>Retries</span><b>${r?.retries??'—'}</b></div><div class="kv"><span>Citations</span><b>${r?.citations?.length??0}</b></div></section><section><div class="insight-icon">${icon(missing?'warning':'shield')}</div><h3>${missing?'Missing evidence attribution':r?.abstained?'An intentional abstention':'Reference evaluation'}</h3><p>${missing?'The answer is present, but no session citation was extracted. Check the full response and evidence.':r?.abstained?'Reactor reported insufficient evidence. Compare with gold to verify that abstaining was correct.':'Import reference answers to check correctness and evidence overlap.'}</p><button class="tiny-link" id="inspectEvidence">Inspect evidence →</button></section><section><h3>Reference data</h3><p>${gold.some(g=>g.question_id===r?.question_id)?'A reference answer is available for this question.':'No reference answer for this question yet.'}</p><button class="tiny-link" id="inspectMetrics">Open evaluation →</button></section><section><h3>History</h3><code>${esc(r?.history_id||'—')}</code></section>`;
 $('#inspectEvidence').onclick=()=>setTab('evidence');$('#inspectMetrics').onclick=()=>setView('metrics');
}
function renderEvaluation(){
 const good=rows.filter(r=>r.status==='answered'), measured=good.filter(r=>Number.isFinite(r.latency_s)), lat=measured.map(r=>r.latency_s), nonabs=good.filter(r=>!r.abstained);
 $('#chart').innerHTML=lat.length?measured.slice(-12).map((r,i)=>`<div class="column" style="height:${Math.max(2,r.latency_s/Math.max(...lat,1)*100)}%" title="${esc(r.question_id)}: ${r.latency_s}s"><span>${r.latency_s.toFixed(1)}s</span><small>#${i+1}</small></div>`).join(''):'<p class="detail-intro">No measured response times yet.</p>';
 const mr=(a,b,c)=>`<div class="metricrow"><div>${a}<small>${c}</small></div><strong>${b}</strong></div>`;
 const summary=state.evaluation_summary;
 $('#apiMetricsCard').classList.toggle('hidden',!summary?.run_count);
 if(summary?.run_count)$('#apiMetrics').innerHTML=mr('Exact answer match',summary.exact_match.value===null?'—':Math.round(summary.exact_match.value*100)+'%',`${summary.exact_match.count} scored attempts`)+mr('Mean evidence F1',summary.evidence_f1.value===null?'—':summary.evidence_f1.value.toFixed(3),`${summary.evidence_f1.count} scored attempts`)+mr('Abstention accuracy',summary.abstention_accuracy.value===null?'—':Math.round(summary.abstention_accuracy.value*100)+'%',`${summary.abstention_accuracy.count} scored attempts`);
 const {exact,abstain,f1,answerable,matched}=evaluate(rows,gold);
 $('#goldmetrics').innerHTML=mr('Exact answer match',pct(exact,answerable),`${answerable} answerable reference questions`)+mr('Mean evidence F1',answerable?(f1/answerable).toFixed(3):'—','Session-level overlap · range 0–1')+mr('Abstention accuracy',pct(abstain,gold.length),`${matched} / ${gold.length} references have completed predictions`);
 $('#quality').innerHTML=mr('Completion rate',pct(good.length,rows.length),'Final answers received, including abstentions')+mr('Uncited answers',nonabs.filter(r=>!r.citations?.length).length,'Completed answers without a citation')+mr('Total retries',rows.reduce((a,r)=>a+(Number(r.retries)||0),0),'Reported by the Reactor client');
 $('#evaluationBanner').innerHTML=icon(gold.length?'check':'database')+`<div><strong>${gold.length?`${gold.length} reference answers loaded`:'Turn observations into evaluation.'}</strong><p>${gold.length?`${matched} matched predictions. Missing predictions count as incorrect.`:'Import gold.jsonl to measure answer correctness and evidence quality.'}</p></div><button class="button" id="importGold">${gold.length?'Replace gold':'Import gold'}</button>`;
 $('#importGold').onclick=()=>$('#importDialog').showModal();
}
function setTab(name){detailTab=name;renderDetail();$('#conversation').scrollTop=0;}
function setView(name){view=name;document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));$('#transactions').classList.toggle('hidden',view!=='transactions');$('#metrics').classList.toggle('hidden',view!=='metrics');$('#title').innerHTML=view==='metrics'?'Evaluate with confidence<span>.</span>':'Conversation workspace<span>.</span>';$('#subtitle').textContent=view==='metrics'?'Know where your agent succeeds — and where evidence is missing.':'A clear view of what your agent knows, says, and cites.';$('#breadcrumb').textContent=view==='metrics'?'Evaluation':'Conversations';}
function updateComposer(){
 $('#evidenceCount').textContent=`${evidence.length} excerpt${evidence.length===1?'':'s'}`;$('#dateHint').textContent=formatDate(questionDate);
 const hasKey=Boolean($('#key').value.trim()||state.has_server_key);
 $('#connectionLabel').textContent=hasKey?'Key set':'Set up agent';$('#connection').classList.toggle('ready',hasKey);
 $('#composeStatus').innerHTML=state.busy?'<i class="dot"></i> Reactor is processing a request…':hasKey?'<i class="dot"></i> Ready · same history, one question at a time':'<i class="dot"></i> Add your key in Connection to send';
 $('#send').disabled=Boolean(state.busy);$('#newhistory').disabled=Boolean(state.busy);
 $('#send').innerHTML=state.busy?'Waiting…':`<span>Send</span>${icon('arrow')}`;
}
async function copy(value,message){try{await navigator.clipboard.writeText(value);toast(message)}catch{toast('Clipboard unavailable. Select the text to copy it.')}}
async function poll(){
 try{const res=await fetch('/api/state');if(!res.ok)throw Error('Local server is unavailable. Restart dashboard/server.py and refresh.');const previous=state;state=await res.json();
  if(!initialized){evidence=state.example_evidence||[];initialized=true;}
  const oldCount=rows.length, currentId=rows[selected]?.question_id;rows=[...state.rows,...imported];const signature=JSON.stringify(rows);
  if(signature!==lastSignature){selected=Math.max(0,rows.findIndex(r=>r.question_id===currentId));if(pending&&rows.length>oldCount){selected=Math.max(0,state.rows.length-1);pending=null;detailTab='conversation';toast('New response received')}lastSignature=signature;render();}
  if(previous.busy&&!state.busy&&pending){pending=null;renderDetail();}
  $('#history').value=state.history_id||'No history yet';updateComposer();
  if(state.error&&state.error!==previous.error)error(state.error);
 }catch(e){error(e.message)}setTimeout(poll,2000);
}
async function run(action){
 error('');if(!$('#key').value.trim()&&!state.has_server_key){$('#settings').showModal();$('#key').focus();return;}
 if(action==='ask'&&!state.history_id){$('#settings').showModal();error('Create a history in Connection settings before asking a question.');return;}
 const question=$('#question').value.trim();if(action==='ask'&&!question)return;
 try{state.busy=true;updateComposer();const res=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json','X-Reactor-Token':TOKEN},body:JSON.stringify({action,key:$('#key').value.trim(),question,date:questionDate,evidence})});const data=await res.json();if(!res.ok)throw Error(data.error||'Request failed');
  if(action==='ask'){pending={question};$('#question').value='';detailTab='conversation';renderDetail();$('#conversation').scrollTop=$('#conversation').scrollHeight;}else{$('#settings').close();toast('Creating a new Reactor history…');}
 }catch(e){state.busy=false;updateComposer();error(e.message)}
}
function validateImport(data,isGold,existing){
 const seen=new Set(isGold?[]:existing.map(r=>r.question_id));
 for(const r of data){
  if(!r||typeof r!=='object'||typeof r.question_id!=='string'||!r.question_id)throw Error('Each row needs a question_id.');
  if(seen.has(r.question_id))throw Error('Duplicate question_id: '+r.question_id);seen.add(r.question_id);
  if(isGold&&(typeof r.abstain!=='boolean'||typeof r.answer!=='string'||!Array.isArray(r.evidence)||r.evidence.some(x=>typeof x!=='string')||(r.aliases&&(!Array.isArray(r.aliases)||r.aliases.some(x=>typeof x!=='string')))))throw Error('Gold rows need abstain, answer, evidence, and optional aliases.');
  if(!isGold&&(typeof r.status!=='string'||(r.citations&&(!Array.isArray(r.citations)||r.citations.some(c=>!(typeof c==='string'||c&&typeof c==='object'))))))throw Error('Import raw Reactor results with status and valid citations fields.');
 }return data;
}
async function importFile(input,isGold){
 try{const file=input.files[0];if(!file)return;if(file.size>20000000)throw Error('Use a results or gold file smaller than 20 MB.');
  const data=(await file.text()).replace(/^\uFEFF/,'').split(/\r?\n/).filter(x=>x.trim()).map((line,i)=>{try{return JSON.parse(line)}catch{throw Error('Invalid JSON on line '+(i+1));}});
  if(!data.length)throw Error('The file contains no records.');validateImport(data,isGold,state.rows||[]);
  if(isGold)gold=data;else imported=data;rows=[...(state.rows||[]),...imported];selected=Math.min(selected,Math.max(0,rows.length-1));lastSignature=JSON.stringify(rows);render();$('#importStatus').textContent=`✓ Loaded ${data.length} ${isGold?'reference answers':'transactions'} from ${file.name}.`;toast(isGold?'Reference metrics are ready':'Transactions imported');
 }catch(e){$('#importStatus').textContent=e.message}input.value='';
}
function readEvidenceFields(){
 return [...document.querySelectorAll('.excerpt-edit')].map(card=>({id:card.querySelector('[data-field="id"]').value.trim(),date:card.querySelector('[data-field="date"]').value.trim(),text:card.querySelector('[data-field="text"]').value}));
}
function renderEvidenceFields(){
 $('#evidenceFields').innerHTML=draftEvidence.map((e,i)=>`<div class="excerpt-edit"><div class="excerpt-title"><strong>Excerpt ${i+1}</strong><button class="icon-button" data-remove-excerpt="${i}" aria-label="Remove excerpt ${i+1}">${icon('close')}</button></div><div class="excerpt-meta"><div><label for="excerpt-id-${i}">Session ID</label><input id="excerpt-id-${i}" data-field="id" value="${esc(e.id)}" placeholder="s_…"></div><div><label for="excerpt-date-${i}">Session date</label><input id="excerpt-date-${i}" data-field="date" value="${esc(e.date)}" placeholder="YYYY/MM/DD HH:MM"></div></div><label for="excerpt-text-${i}">Conversation excerpt</label><textarea id="excerpt-text-${i}" data-field="text" rows="3" placeholder="user: …&#10;assistant: …">${esc(e.text)}</textarea></div>`).join('')||'<p class="field-help">No evidence attached. Add an excerpt to ground your next answer.</p>';
 document.querySelectorAll('[data-remove-excerpt]').forEach(b=>b.onclick=()=>{draftEvidence=readEvidenceFields();draftEvidence.splice(+b.dataset.removeExcerpt,1);renderEvidenceFields()});
}
function setEvidenceMode(mode){
 try{if(mode===evidenceMode)return;if(mode==='json'){$('#evidence').value=JSON.stringify(readEvidenceFields(),null,2);}else{const parsed=JSON.parse($('#evidence').value);if(!Array.isArray(parsed)||parsed.some(e=>!e||typeof e!=='object'))throw Error('Evidence must be an array of excerpt objects.');draftEvidence=parsed;renderEvidenceFields();}evidenceMode=mode;$('#evidenceError').textContent='';updateEvidenceMode();}catch(e){$('#evidenceError').textContent=e.message;}
}
function updateEvidenceMode(){
 $('#evidenceFields').classList.toggle('hidden',evidenceMode!=='fields');$('#addExcerpt').classList.toggle('hidden',evidenceMode!=='fields');$('#evidence').classList.toggle('hidden',evidenceMode!=='json');$('#structuredMode').classList.toggle('active',evidenceMode==='fields');$('#jsonMode').classList.toggle('active',evidenceMode==='json');
}
async function api(path, body){
 const res=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Reactor-Token':TOKEN},...(body?{body:JSON.stringify(body)}:{})});const data=await res.json();if(!res.ok)throw Error(data.error||'API request failed');return data;
}
function showHarness(){
 const config=state.harness||{};$('#harnessEnabled').checked=!!config.enabled;$('#harnessURL').value=config.url||'';$('#harnessTimeout').value=config.timeout_s||30;$('#harnessToken').value='';$('#pipelineError').textContent='';$('#pipelineDialog').showModal();
}
async function previewPipeline(){
 const question=$('#question').value.trim();if(!question){$('#pipelineError').textContent='Enter a question in the composer first.';return;}
 $('#pipelineError').textContent='';$('#previewPipeline').disabled=true;$('#pipelinePreview').innerHTML='<p class="detail-intro">Preparing your prompt… Reactor will not be called.</p>';
 try{const accepted=await api('/api/v1/runs',{original_prompt:question,question_date:questionDate,evidence,dry_run:true});
  const deadline=Date.now()+150000;
  async function inspect(){try{const run=await api(accepted.poll_url);if(['prepared','failed','completed'].includes(run.status)){$('#pipelinePreview').innerHTML=`<strong>${run.status==='prepared'?'Ready to send · dry run':'Pipeline failed'}</strong><pre style="margin-top:10px;max-height:300px;overflow:auto">${esc(JSON.stringify({run_id:run.run_id,original_prompt:run.input.original_prompt,prepared:run.prepared,outbound_message:run.outbound_message,stages:run.stages,error:run.error},null,2))}</pre>`;$('#previewPipeline').disabled=false;}else if(Date.now()>deadline){throw Error('Preview is taking too long. Retrieve the run using '+accepted.poll_url);}else setTimeout(inspect,1000);}catch(e){$('#pipelineError').textContent=e.message;$('#previewPipeline').disabled=false;}}
  inspect();
 }catch(e){$('#pipelineError').textContent=e.message;$('#previewPipeline').disabled=false;$('#pipelinePreview').innerHTML='';}
}
function bind(){
 $('#pipelineBtn').onclick=showHarness;$('#previewPipeline').onclick=previewPipeline;
 $('#saveHarness').onclick=async()=>{try{const config={enabled:$('#harnessEnabled').checked,url:$('#harnessURL').value.trim(),timeout_s:Number($('#harnessTimeout').value)};if($('#harnessToken').value)config.token=$('#harnessToken').value;state.harness=await api('/api/v1/harness',config);$('#harnessToken').value='';$('#pipelineError').textContent='';toast('Harness configuration saved');}catch(e){$('#pipelineError').textContent=e.message}};

 $('#composer').onsubmit=e=>{e.preventDefault();run('ask')};
 $('#question').onkeydown=e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter'&&!state.busy){e.preventDefault();run('ask')}};
 $('#newhistory').onclick=()=>{if(confirm('Create a new history? This resets the current agent memory for everyone using this agent.'))run('history')};
 $('#connection').onclick=()=>$('#settings').showModal();$('#import').onclick=()=>$('#importDialog').showModal();
 $('#saveConnection').onclick=()=>{$('#settings').close();updateComposer();toast($('#key').value.trim()?'Key set for this tab. It will be verified on your next request.':'Connection settings closed')};
 $('#toggleKey').onclick=()=>{const show=$('#key').type==='password';$('#key').type=show?'text':'password';$('#toggleKey').textContent=show?'Hide':'Show'};
 $('#evidenceBtn').onclick=()=>{draftEvidence=structuredClone(evidence);evidenceMode='fields';renderEvidenceFields();updateEvidenceMode();$('#date').value=questionDate;$('#evidenceError').textContent='';$('#evidenceDialog').showModal()};
 $('#structuredMode').onclick=()=>setEvidenceMode('fields');$('#jsonMode').onclick=()=>setEvidenceMode('json');$('#addExcerpt').onclick=()=>{draftEvidence=readEvidenceFields();draftEvidence.push({id:'',date:'',text:''});renderEvidenceFields()};
 document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());
 $('#saveEvidence').onclick=()=>{try{const x=evidenceMode==='fields'?readEvidenceFields():JSON.parse($('#evidence').value);if(!Array.isArray(x)||x.some(e=>!e||typeof e.id!=='string'||!e.id||typeof e.text!=='string'||typeof e.date!=='string'))throw Error('Each excerpt needs an id, date, and text string.');evidence=x;questionDate=$('#date').value.trim();updateComposer();$('#evidenceDialog').close();toast('Evidence updated')}catch(e){$('#evidenceError').textContent=e.message}};
 $('#search').oninput=renderList;$('#filter').onchange=renderList;
 document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>setView(b.dataset.view));
 document.querySelectorAll('[data-tab]').forEach(b=>{b.onclick=()=>setTab(b.dataset.tab);b.onkeydown=e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft'){const tabs=[...document.querySelectorAll('[data-tab]')],i=tabs.indexOf(b),next=tabs[(i+(e.key==='ArrowRight'?1:tabs.length-1))%tabs.length];e.preventDefault();next.focus();setTab(next.dataset.tab)}}});
 $('#browse').onclick=()=>{$('#transactions').classList.add('show-list');$('#search').focus()};$('#closeList').onclick=()=>$('#transactions').classList.remove('show-list');
 $('#previous').onclick=()=>selectRow(selected-1);$('#next').onclick=()=>selectRow(selected+1);
 $('#resultFile').onchange=()=>importFile($('#resultFile'),false);$('#goldFile').onchange=()=>importFile($('#goldFile'),true);
 $('#export').onclick=()=>{if(!rows.length){toast('No transactions to export');return;}const url=URL.createObjectURL(new Blob([rows.map(r=>JSON.stringify(r)).join('\n')+'\n'],{type:'application/x-ndjson'}));const a=document.createElement('a');a.href=url;a.download='reactor-results.jsonl';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast(`${rows.length} transactions exported`)};
}
bind();render();updateComposer();poll();
