// Live evaluation view: polls the read-only /api/v1/live API once a second while the view is open.
// Uses $, esc, api and setView from app.js. Logs hold ids and numbers only, so nothing here renders conversation text.
const live={on:false,runs:[],id:null,follow:true,ev:0,st:0,events:[],stats:[],status:null,timer:null,runsAt:0};
const LIVE_COLORS=['#23775c','#a17124','#4a6fa5','#b0614f'];
const LIVE_TABLES={
 retrieval:[['recall@10','recall@10',1],['all@10','all@10',1],['pack_recall','pack recall',1],['pack_all','pack all',1],['pack_precision','precision',1],['answer_text_in_pack','answer in pack',1],['passages_mean','passages',0],['message_chars_mean','chars',0]],
 reactor:[['correct','correct',1],['unjudged','unjudged',1],['abstention_recall','abstain recall',1],['false_abstention','false abstain',1],['evidence_f1','evidence F1',1],['evidence_f1_with_fallback','F1 + fallback',1]]};
const LIVE_SERIES={
 retrieval:[['pack_all','pack all'],['pack_recall','pack recall'],['recall@10','recall@10'],['answer_text_in_pack','answer in pack']],
 reactor:[['correct','correct'],['evidence_f1','evidence F1'],['abstention_recall','abstain recall'],['evidence_f1_with_fallback','F1 + fallback']]};
const TYPE_ORDER=['all','single-session-user','single-session-assistant','multi-session','knowledge-update','temporal-reasoning'];

const lpct=x=>x==null?'—':(x*100).toFixed(1)+'%';
const lnum=(x,d=1)=>x==null?'—':Number(x).toLocaleString('en-US',{maximumFractionDigits:d});
function ldur(s){if(s==null)return'—';s=Math.round(s);if(s<60)return s+' s';const m=Math.floor(s/60);if(m<60)return`${m} m ${s%60} s`;return`${Math.floor(m/60)} h ${m%60} m`;}
const lastSnap=()=>live.stats[live.stats.length-1]||null;
const runMeta=()=>live.runs.find(r=>r.id===live.id)||null;

function liveView(on){live.on=on;if(on&&!live.timer)liveTick();}
async function liveTick(){
 live.timer=null;if(!live.on)return;
 try{
  if(!live.id||Date.now()-live.runsAt>4000){await liveRuns();if(!live.stats.length)liveRender();}  // progress from the run list while history loads
  if(live.id)await liveFetch();
  $('#liveError').classList.add('hidden');liveRender();
 }catch(e){$('#liveError').textContent=e.message;$('#liveError').classList.remove('hidden');}
 if(live.on)live.timer=setTimeout(liveTick,1000);
}
async function liveRuns(){
 const data=await api('/api/v1/live/runs');live.runs=data.runs;live.runsAt=Date.now();
 const sel=$('#liveRun');
 sel.innerHTML=live.runs.length?live.runs.map(r=>`<option value="${esc(r.id)}">${esc(r.id)} · ${esc(r.kind)} · ${esc(r.status)}</option>`).join(''):'<option value="">No runs with live logs yet</option>';
 const newest=live.runs[0]?.id;
 if(newest&&(!live.id||live.follow&&newest!==live.id))liveSelect(newest);
 sel.value=live.id||'';
}
function liveSelect(id){live.id=id;live.ev=0;live.st=0;live.events=[];live.stats=[];live.status=null;$('#liveRun').value=id;}
async function liveFetch(){
 const id=live.id,base=`/api/v1/live/runs/${encodeURIComponent(id)}`;
 for(let page=0;page<10;page++){  // catch up on a long run in a few large pages
  const s=await api(`${base}/stats?after=${live.st}&limit=2000`);if(id!==live.id)return;
  live.stats.push(...s.items);live.st=s.next;live.status=s.status;if(s.items.length<2000)break;
 }
 const e=await api(live.ev?`${base}/events?after=${live.ev}&limit=500`:`${base}/events?tail=40`);if(id!==live.id)return;
 live.events=[...e.items.reverse(),...live.events].slice(0,40);live.ev=e.next;
}

function bar(label,n,total,cls,note){
 const share=total?Math.min(1,n/total):0;
 return`<div class="lp-row"><div class="lp-label"><strong>${label}</strong><span>${note??`${lnum(n,0)} / ${lnum(total,0)} · ${(share*100).toFixed(1)}%`}</span></div><div class="lp-track ${note?'none':''}"><i class="${cls}" style="width:${note?0:share*100}%"></i></div></div>`;
}
function liveRender(){
 const meta=runMeta(),snap=lastSnap(),p=snap?.progress||meta?.progress||{},kind=meta?.kind||'retrieval';
 const status=live.status||meta?.status||'—',age=snap?Date.now()/1000-snap.ts:null;
 const stale=status==='running'&&age>120;
 $('#liveStatus').className='badge '+(stale?'warn':status==='failed'?'bad':status==='running'?'':'neutral');
 $('#liveStatus').textContent=stale?`no update for ${ldur(age)}`:status;
 $('#liveMeta').textContent=meta?[meta.command,meta.split&&`split ${meta.split}`,meta.partition&&meta.partition!=='all'&&meta.partition,meta.config&&`config ${meta.config}`,meta.backend&&`backend ${meta.backend}`].filter(Boolean).join(' · '):'';
 const total=p.total??meta?.total??0;
 $('#liveProgress').innerHTML=!meta?'<p class="caption">Start a run with live logs (evaluate-retrieval, run-ablation, retrieve or run-reactor) and it appears here.</p>':
  bar('Processed',p.processed||0,total,'s1')+bar('Evaluated',p.evaluated||0,total,'s2')+
  bar('Graded',p.graded||0,total,'s3',p.graded==null?'no gold for this split':null)+
  `<div class="lp-foot"><span>ETA <strong>${status==='running'?ldur(p.eta_s):'—'}</strong></span><span>elapsed <strong>${ldur(p.elapsed_s)}</strong></span><span><strong>${lnum(p.rate_per_min)}</strong> questions / min</span>${p.failures!=null?`<span><strong>${p.failures}</strong> failed</span>`:''}${p.late_salvaged!=null?`<span><strong>${p.late_salvaged}</strong> late answers salvaged</span><span><strong>${p.pings}</strong> drain pings</span>`:''}</div>`;
 liveTable(kind,snap);liveCharts(kind);liveSide(kind,snap);liveFeed(kind);
}
function liveTable(kind,snap){
 const cols=LIVE_TABLES[kind]||LIVE_TABLES.retrieval,m=snap?.metrics;
 $('#liveTableLabel').textContent=kind==='reactor'?(snap?.metrics_label||'local, unofficial'):'cumulative over graded questions';
 if(!m){$('#liveTable').innerHTML=`<p class="caption">${snap?'No graded questions yet (gold is needed for per-type metrics).':'Waiting for the first question…'}</p>`;return;}
 const types=Object.keys(m).sort((a,b)=>(TYPE_ORDER.indexOf(a)+99)%99-(TYPE_ORDER.indexOf(b)+99)%99||a.localeCompare(b));
 $('#liveTable').innerHTML=`<div class="lt-wrap"><table class="lt"><thead><tr><th>type</th><th>n</th>${cols.map(c=>`<th>${esc(c[1])}</th>`).join('')}</tr></thead><tbody>${types.map(t=>`<tr class="${t==='all'?'all':''}"><td>${esc(t)}</td><td>${lnum(m[t].n,0)}</td>${cols.map(([k,,isPct])=>`<td>${isPct?lpct(m[t][k]):lnum(m[t][k])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function thin(points,max=400){if(points.length<=max)return points;const k=points.length/max,out=[];for(let i=0;i<max;i++)out.push(points[Math.floor(i*k)]);out.push(points[points.length-1]);return out;}
function lineChart(el,series,yMax,fmt,unit='questions'){
 const W=Math.max(260,el.clientWidth||560),H=180,L=40,R=12,T=10,B=22,all=series.flatMap(s=>s.pts);
 if(!all.length)return'<p class="caption">Waiting for data…</p>';
 const xMax=Math.max(1,...all.map(p=>p[0]));yMax=yMax||Math.max(1e-9,...all.map(p=>p[1]))*1.15;
 const x=v=>L+(W-L-R)*v/xMax,y=v=>T+(H-T-B)*(1-v/yMax);
 const ticks=[0,.25,.5,.75,1].map(f=>f*yMax);
 const grid=ticks.map(v=>`<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" class="lc-grid"/><text x="${L-6}" y="${y(v)+3}" class="lc-tick" text-anchor="end">${fmt(v)}</text>`).join('');
 const lines=series.map(s=>{const pts=thin(s.pts);if(!pts.length)return'';const last=pts[pts.length-1];
  if(s.dots)return pts.map(p=>`<circle cx="${x(p[0]).toFixed(1)}" cy="${y(p[1]).toFixed(1)}" r="3.5" fill="${s.color}"/>`).join('');
  return`<path d="${pts.map((p,i)=>`${i?'L':'M'}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join('')}" stroke="${s.color}" class="lc-line"/><circle cx="${x(last[0])}" cy="${y(last[1])}" r="3" fill="${s.color}"/>`;}).join('');
 const axis=`<text x="${W-R}" y="${H-5}" class="lc-tick" text-anchor="end">${lnum(xMax,0)} ${esc(unit)}</text><text x="${L}" y="${H-5}" class="lc-tick">0</text>`;
 const legend=series.filter(s=>s.pts.length).map(s=>`<span><i style="background:${s.color}"></i>${esc(s.name)} <strong>${fmt(s.pts[s.pts.length-1][1])}</strong></span>`).join('');
 return`<svg viewBox="0 0 ${W} ${H}" class="lc" role="img" aria-label="${esc(series.map(s=>s.name).join(', '))} over ${esc(unit)}">${grid}${axis}${lines}</svg><div class="lc-legend">${legend}</div>`;
}
function liveCharts(kind){
 const x=s=>s.progress?.[kind==='reactor'?'evaluated':'processed']||0;
 const quality=(LIVE_SERIES[kind]||[]).map(([k,name],i)=>({name,color:LIVE_COLORS[i],pts:live.stats.filter(s=>s.metrics?.all?.[k]!=null).map(s=>[x(s),s.metrics.all[k]])}));
 $('#liveQuality').innerHTML=lineChart($('#liveQuality'),quality,1,v=>(v*100).toFixed(0)+'%');
 $('#liveLatencyUnit').textContent=kind==='reactor'?'seconds':'ms';
 $('#liveLatencyTitle').textContent=kind==='reactor'?'Reactor latency per request, in order':'Latency';
 if(kind==='reactor'){  // one point per request in send order: latency growing over a history shows as a rising line
  const req=live.stats.filter(s=>s.latest_request);
  const lat=[{name:'answered in',color:LIVE_COLORS[2],pts:req.filter(s=>s.latest_request.reactor_latency_s!=null).map(s=>[s.latest_request.order,s.latest_request.reactor_latency_s])},
   {name:'timed out after',color:LIVE_COLORS[3],dots:true,pts:req.filter(s=>s.latest_request.outcome==='ambiguous').map(s=>[s.latest_request.order,s.latest_request.wall_s])},
   {name:'p50 so far',color:LIVE_COLORS[0],pts:req.filter(s=>s.reactor_latency_s?.p50!=null).map(s=>[s.latest_request.order,s.reactor_latency_s.p50])}];
  $('#liveLatency').innerHTML=lineChart($('#liveLatency'),lat,0,v=>lnum(v,v<10?1:0),'requests');return;
 }
 const lat=[['retrieval_p50','retrieval p50'],['retrieval_p95','retrieval p95']].map(([k,name],i)=>({name,color:LIVE_COLORS[i+2],pts:live.stats.filter(s=>s.latency_ms?.[k]!=null).map(s=>[x(s),s.latency_ms[k]])}));
 $('#liveLatency').innerHTML=lineChart($('#liveLatency'),lat,0,v=>lnum(v,v<10?1:0));
}
function liveSide(kind,snap){
 let rows=[];
 if(kind==='reactor'){const o=snap?.outcomes||{};rows=Object.keys(o).length?Object.entries(o).map(([k,v])=>[k,v]):[['outcomes','—']];
  const l=snap?.reactor_latency_s||{};rows.push(['latency mean',l.mean==null?'—':l.mean+' s']);}
 else{const l=snap?.latency_ms||{},z=snap?.size||{};rows=[['index build p50',l.index_build_p50==null?'—':lnum(l.index_build_p50)+' ms'],['pack p50',l.pack_p50==null?'—':lnum(l.pack_p50)+' ms'],['passages / question',lnum(z.passages_mean)],['message chars',lnum(z.message_chars_mean,0)]];}
 $('#liveSide').innerHTML=rows.map(([k,v])=>`<div class="metricrow"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('');
}
function feedRow(kind,e){
 const stage=`<span class="badge ${e.stage==='graded'?'':e.stage==='processed'||e.stage==='ping'?'neutral':'warn'}">${esc(e.stage)}</span>`;
 let detail;
 if(e.stage==='ping')return`<div class="lf-row"><span class="mono">#${esc(e.seq)}</span>${stage}<span class="mono">${esc(e.dataset_history_id)}</span><span class="lf-detail">${esc([e.status,`${e.late_replies} late ${e.late_replies===1?'reply':'replies'}`,`${lnum(e.wall_s)} s`,e.reason].join(' · '))}</span></div>`;
 if(kind==='reactor'){const g=e.local_unofficial,l=e.late_salvaged;
  detail=e.stage==='processed'?`sent · attempt ${esc(e.attempt)}`:[l&&`late answer salvaged ${l.via==='ping'?'by the drain ping':`while asking ${l.seen_during}`}`,e.outcome,e.reactor_latency_s!=null&&`${lnum(e.reactor_latency_s)} s`,`${esc(e.citations??0)} cited`,e.fallback_would_cite&&`fallback ${e.fallback_would_cite}`,g&&`answer ${g.answer}`,g&&g.evidence_f1!=null&&`F1 ${lnum(g.evidence_f1,2)}`].filter(Boolean).map(esc).join(' · ');}
 else{const m=e.metrics||{},ok=m.pack_all;
  detail=[e.type,ok==null?(e.abstain?'unanswerable':null):(ok?'all evidence packed':'evidence missed'),m.pack_recall!=null&&`recall ${lpct(m.pack_recall)}`,`${e.passages} passages`,`${lnum(e.latency?.retrieval_ms)} ms`].filter(Boolean).map(esc).join(' · ');}
 return`<div class="lf-row"><span class="mono">#${esc(e.seq)}</span>${stage}<span class="mono">${esc(e.question_id)}</span><span class="lf-detail">${detail}</span></div>`;
}
function liveFeed(kind){$('#liveFeed').innerHTML=live.events.length?live.events.map(e=>feedRow(kind,e)).join(''):'<p class="caption">No events yet.</p>';}

$('#liveRun').onchange=e=>{live.follow=false;$('#liveFollow').checked=false;liveSelect(e.target.value);};
$('#liveFollow').onchange=e=>{live.follow=e.target.checked;live.runsAt=0;};
if(location.hash.startsWith('#live'))setView('live');
addEventListener('hashchange',()=>{if(location.hash.startsWith('#live')&&view!=='live')setView('live');});
