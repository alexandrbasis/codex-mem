'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const state = {period: 'all', paused: false, page: 1, query: '', route: null, data: null, request: 0, controller: null, busy: false, lastUpdated: null, dataContext: null, failure: null};
  const number = value => value == null ? 'No data' : new Intl.NumberFormat('en-US').format(Number(value));
  const money = value => value == null ? 'Unavailable' : new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:4}).format(Number(value));
  const compact = value => value == null ? 'No data' : new Intl.NumberFormat('en-US',{notation:'compact',maximumFractionDigits:1}).format(Number(value));
  const briefMoney = value => value == null ? 'Unavailable' : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(Number(value));
  const date = value => {if(value==null)return 'No data';const moment=new Date(typeof value==='number'?value*1000:value);return Number.isNaN(moment.valueOf())?'No data':new Intl.DateTimeFormat('en-US',{dateStyle:'short',timeStyle:'short',timeZone:'Asia/Jerusalem'}).format(moment);};
  const iconPaths = {overview:['M3 3h7v7H3z','M14 3h7v7h-7z','M14 14h7v7h-7z','M3 14h7v7H3z'],folder:['M20 20H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5l2 2h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2Z'],database:['M20 6c0 2.2-3.6 4-8 4S4 8.2 4 6s3.6-4 8-4 8 1.8 8 4Z','M4 6v12c0 2.2 3.6 4 8 4s8-1.8 8-4V6','M4 12c0 2.2 3.6 4 8 4s8-1.8 8-4'],moon:['M20.9 13A9 9 0 0 1 11 3.1 9 9 0 1 0 20.9 13Z'],sun:['M12 3v1','M12 20v1','M3 12h1','M20 12h1','m5 5 1 1','m18 18 1 1','m5 19 1-1','m18 6 1-1','M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0'],pause:['M6 4h4v16H6z','M14 4h4v16h-4z'],play:['m7 4 14 8-14 8V4Z'],refresh:['M20 7a9 9 0 0 0-15-2L2 8','M2 3v5h5','M4 17a9 9 0 0 0 15 2l3-3','M22 21v-5h-5'],download:['M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4','m7 10 5 5 5-5','M12 15V3'],coins:['M20 7c0 2.2-3.6 4-8 4S4 9.2 4 7s3.6-4 8-4 8 1.8 8 4Z','M4 7v10c0 2.2 3.6 4 8 4s8-1.8 8-4V7','M4 12c0 2.2 3.6 4 8 4s8-1.8 8-4'],activity:['M22 12h-4l-3 9L9 3l-3 9H2'],layers:['m12 3 10 5-10 5L2 8l10-5Z','m2 12 10 5 10-5','m2 16 10 5 10-5'],arrow:['M7 7v6a4 4 0 0 0 4 4h9','m16 13 4 4-4 4']};
  function el(tag, className, text) {const node=document.createElement(tag);if(className)node.className=className;if(text!=null)node.textContent=String(text);return node;}
  function icon(name) {const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('class','icon');svg.setAttribute('aria-hidden','true');for(const d of iconPaths[name]||iconPaths.activity){const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d',d);svg.append(p);}return svg;}
  document.querySelectorAll('[data-icon]').forEach(n=>n.append(icon(n.dataset.icon)));
  function link(text, hash, className) {const a=el('a',className,text);a.href=hash;return a;}
  const projectHash = project => '#project/'+encodeURIComponent(project);
  const sessionHash = (project,id) => '#session/'+encodeURIComponent(project)+'/'+encodeURIComponent(id);
  function button(text, fn, disabled=false) {const n=el('button','button secondary',text);n.type='button';n.disabled=disabled;n.addEventListener('click',fn);return n;}
  function panel(title, subtitle, content) {const n=el('section','panel');const head=el('div','panel-heading'),copy=el('div');copy.append(el('h2',null,title));if(subtitle)copy.append(el('p',null,subtitle));head.append(copy);n.append(head);if(content)n.append(content);return n;}
  function disclosure(key, title, ...content) {
    const node=el('details','detail-section'),body=el('div','detail-body');
    node.dataset.detailKey=key;node.append(el('summary',null,title));body.append(...content);node.append(body);return node;
  }
  function metric(label,value,note,iconName) {const n=el('article','metric');const l=el('div','metric-label');l.append(icon(iconName),el('span',null,label));n.append(l,el('div','metric-value',value),el('div','metric-note',note));return n;}
  function facts(rows) {const dl=el('dl','fact-list');for(const [label,value] of rows){const r=el('div','fact');r.append(el('dt',null,label),el('dd',null,value));dl.append(r);}return dl;}
  function badge(value) {const labels={pending:'Queued',running:'Running',failed:'Failed',error:'Failed',processed:'Processed',completed:'Done',done:'Done',success:'Succeeded',succeeded:'Succeeded',skipped:'Skipped',reported:'Recorded',partial:'Partial',unavailable:'Unavailable'};return el('span','badge '+(['failed','error'].includes(value)?'failed':['completed','success','succeeded','done'].includes(value)?'healthy':''),labels[value]||value||'Unknown');}
  function table(headers, rows, empty='No records for the selected period.') {if(!rows.length)return el('div','empty',empty);const wrap=el('div','table-wrap');wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label',headers.join(', '));const t=el('table'),head=el('thead'),tr=el('tr');for(const h of headers){const th=el('th',null,h);th.scope='col';tr.append(th);}head.append(tr);t.append(head);const body=el('tbody');for(const row of rows){const r=el('tr');for(const value of row){const td=el('td');if(value instanceof Node)td.append(value);else td.textContent=value==null?'No data':String(value);r.append(td);}body.append(r);}t.append(body);wrap.append(t);return wrap;}
  function pagination(p) {if(!p||p.pages<2)return el('div');const wrap=el('div','pagination');wrap.append(el('span',null,`Page ${p.page} of ${p.pages} · ${number(p.total)} records`));const actions=el('div','actions');actions.append(button('Previous',()=>{state.page--;load();},p.page<=1),button('Next',()=>{state.page++;load();},p.page>=p.pages));wrap.append(actions);return wrap;}
  function amount(metric, formatter) {if(metric?.total!=null)return formatter(metric.total);if(metric?.selected_subtotal!=null&&(Number(metric.selected_subtotal)>0||metric.selected_events>0))return formatter(metric.selected_subtotal)+' · partial';return 'Unavailable';}
  const cost = stream => amount(stream?.api_equivalent_usd,money);
  const credits = stream => amount(stream?.estimated_codex_credits,number);
  function summary(data) {
    const n=el('div','metrics metrics-compact'),c=data.combined||{},observer=data.observer||{},main=data.main||{},usd=c.api_equivalent_usd||{};
    const share=typeof observer.total_tokens==='number'&&typeof main.total_tokens==='number'&&main.total_tokens>0?new Intl.NumberFormat('en-US',{maximumFractionDigits:1}).format(observer.total_tokens/main.total_tokens*100)+'% of main Codex':'Share of main Codex unknown';
    const knownUsd=usd.total ?? (usd.selected_subtotal!=null&&(usd.selected_subtotal>0||usd.selected_events>0)?usd.selected_subtotal:null);
    n.append(metric('Combined tokens',compact(c.total_tokens),data.status==='partial'?'Main Codex + processing · partial records':'Main Codex + processing','layers'),metric('Processing tokens',compact(observer.total_tokens),share,'database'),metric(usd.total==null?'API estimate · partial':'API estimate',briefMoney(knownUsd),usd.total==null?'USD · full total unavailable':'USD · main Codex + processing','coins'));
    n.children[0].title=number(c.total_tokens)+' tokens';n.children[1].title=number(observer.total_tokens)+' tokens';n.children[2].title=knownUsd==null?'Estimate unavailable':money(knownUsd);
    return n;
  }
  function streams(data) {
    const rows=[['Main Codex',data.main],['Memory processing',data.observer],['Combined',data.combined]];
    const n=panel('Costs and tokens','Standard and Fast show scenarios when the service tier is unknown. These are API estimates, not billed charges.',table(['Stream','API estimate · USD','Standard scenario · USD','Fast scenario · USD','Tokens','Estimated Codex credits'],rows.map(([name,s])=>[name,cost(s),money(s?.api_equivalent_usd?.standard_scenario_subtotal),money(s?.api_equivalent_usd?.fast_scenario_subtotal),number(s?.total_tokens),credits(s)])));
    n.append(facts([['Codex responses without duplicates',number(data.main?.event_count)],['Processing attempts, including failures and retries',number(data.observer?.event_count)]]));return n;
  }
  function queue(data) {
    const q=data.queue||{},progress=q.progress||{},service=data.service||{},wrap=el('div','queue'),status=el('div','queue-status'),copy=el('div','queue-status-copy');
    const completed=progress.available&&typeof progress.processed_jobs==='number'?progress.processed_jobs:null;
    const skipped=progress.available&&typeof progress.skipped_jobs==='number'?progress.skipped_jobs:null;
    const running=q.available&&typeof q.running==='number'?q.running:null;
    const hasProgress=(completed??0)+(skipped??0)>0;
    const title=running>0?'Jobs are running':hasProgress?'Progress recorded in the last hour':q.available&&q.pending===0?'No unprocessed records':'No confirmed progress in the last hour';
    copy.append(el('h2','queue-status-title',title));
    const notes=[];
    if(progress.available)notes.push('Last hour: processed '+number(completed)+', skipped '+number(skipped)+', failed '+number(progress.failed_jobs));
    else notes.push('Progress data unavailable');
    if(progress.last_completed_at)notes.push('Last completed '+date(progress.last_completed_at));
    if(typeof service.running==='boolean')notes.push(service.running?'Background service is running':'Background service is stopped');
    copy.append(el('p','queue-status-note',notes.join(' · ')));status.append(copy);
    const failed=q.available&&typeof q.failed==='number'&&typeof q.quarantined==='number'?Math.max(0,q.failed-q.quarantined):null;
    for(const [value,label] of [[q.available?q.pending:null,'Unprocessed records'],[running,'Running jobs'],[failed,'Failures outside quarantine'],[q.available?q.quarantined:null,'Quarantined']]){const cell=el('div','queue-cell');cell.append(el('strong',null,number(value)),el('span',null,label));wrap.append(cell);}
    const n=el('section','panel queue-summary');n.append(status,wrap);
    if(q.available&&q.pending>0){
      const oldest=q.pending_oldest_at==null?NaN:new Date(typeof q.pending_oldest_at==='number'?q.pending_oldest_at*1000:q.pending_oldest_at).valueOf();
      const snapshot=new Date(data.coverage?.snapshot_at||Date.now()).valueOf(),minutes=Math.floor((snapshot-oldest)/60000);
      const age=Number.isFinite(minutes)&&minutes>=0?(minutes>=1440?number(Math.floor(minutes/1440))+' days':minutes>=60?number(Math.floor(minutes/60))+' hr':number(minutes)+' min'):'unknown';
      n.append(el('p','muted','Oldest pending record: '+age+' · since '+date(q.pending_oldest_at)));
    }
    n.append(el('p','muted','The queue covers all dates. Last-hour progress uses the current state of saved jobs.'));
    const details=facts([['Queued jobs',number(q.available?q.pending_jobs:null)],['Projects in the service queue',number(q.queued_projects)],['Blocked projects',number(q.blocked_projects)],['Last attempt',date(progress.last_attempt_at)]]);
    const contents=[details];
    if(q.service_record){const r=q.service_record;contents.push(facts([['Blocked',r.blocked===true?'Yes':r.blocked===false?'No':'Unknown'],['Waiting for new data',r.parked===true?'Yes':r.parked===false?'No':'Unknown'],['Next attempt',date(r.due_at)],['Last code',r.last_code||'No data'],['Active until',date(r.inflight_until)]]));}
    contents.push(el('p','muted','The processor has not claimed unprocessed records. Quarantined jobs are separate from other failures.'));
    n.append(disclosure('queue-details','Queue details',...contents));return n;
  }
  function processingUsage(data) {
    const j=data.jev||{},jevTokens=receipt=>receipt?.available&&typeof receipt.input_tokens==='number'&&typeof receipt.output_tokens==='number'?receipt.input_tokens+receipt.output_tokens:null;
    const rows=[['codex','Codex · memory processing',data.observer?.total_tokens],['filter','Jev · filtering',jevTokens(j.filter)],['quality','Jev · note quality',jevTokens(j.quality)],['retrieval','Jev · memory search',jevTokens(j.retrieval)]];
    const maximum=Math.max(0,...rows.map(([, ,value])=>typeof value==='number'?value:0)),chart=el('div','usage-chart');
    for(const [key,label,value] of rows){const known=typeof value==='number',row=el('div','usage-row usage-'+key+(known?'':' usage-unavailable')),track=el('div','usage-track'),fill=el('div','usage-fill');fill.style.setProperty('--usage-share',known&&maximum>0?String(value/maximum*100)+'%':'0%');track.setAttribute('aria-hidden','true');track.append(fill);row.append(el('span','usage-label',label),track,el('span','usage-value',number(known?value:null)));chart.append(row);}
    const n=panel('Processing token usage','Codex and Jev tokens are separate. Bar lengths use the largest value as their scale.',chart);
    n.append(el('p','muted','Jev counts saved input and output tokens. Quality and search include up to 2,000 audits per project. Jev tokens are outside Codex totals.'));return n;
  }
  function capture(data) {const c=data.capture||{};return panel('Memory capture','Metadata for records saved in the selected period.',facts([['Records',number(c.available?c.entries:null)],['Last capture',date(c.last_capture_at)],['Last note',date(c.last_note_at)],...Object.entries(c.by_kind||{}).map(([key,value])=>[({observation:'Observations',session_summary:'Session summaries',decision:'Decisions'}[key]||key),number(value)])]));}
  function pricing(data) {const c=data.combined||{},m=c.api_equivalent_usd||{},g=c.completeness||data.completeness?.combined||{};const n=panel('Estimate coverage','Incomplete records and unknown service tiers do not mean zero cost.',facts([['Priced at the selected tier',money(m.selected_subtotal)],['Records without estimates',number(m.unpriced_events)],['Unknown service tier',number(g.unknown_tier_events)],['Confirmed service tier',number(g.confirmed_tier_events)],['Requested service tier only',number(g.requested_tier_events)],['Standard scenario',money(m.standard_scenario_subtotal)],['Fast scenario',money(m.fast_scenario_subtotal)]]));if(typeof c.event_count==='number'&&c.event_count>0&&typeof m.unpriced_events==='number'){const row=el('div','meter-row'),priced=Math.max(0,c.event_count-m.unpriced_events),meter=el('meter');meter.min=0;meter.max=c.event_count;meter.value=priced;meter.setAttribute('aria-label','Records with cost estimates');row.append(el('span','muted',`${number(priced)} of ${number(c.event_count)} records have estimates`),meter);n.append(row);}if(data.pricing){const p=data.pricing;n.append(el('p','muted','Pricing catalog '+(p.version||'unknown')+' · reviewed '+(p.reviewed_at||'date unknown')+'. Historical estimates use this catalog.'));n.append(el('p','muted','Estimated credits apply to purchased credits and Enterprise PAYG. They exclude subscription fees, taxes, and actual charges.'));}return n;}
  function jev(data) {const j=data.jev||{};const rows=[['Filtering',j.filter],['Note quality',j.quality],['Memory search',j.retrieval]].map(([name,r])=>[name,number(r?.available?r.receipts:null),number(r?.available?r.input_tokens:null),number(r?.available?r.output_tokens:null),number(r?.available?r.requests:null),number(r?.available?r.cache_hits:null)]);const n=panel('Jev checks','Jev API estimates are unavailable. No local price is set.',table(['Check','Audits','Input','Output','Requests','Cache'],rows));n.append(el('p','muted','Quality and search include up to 2,000 saved audits per project. Filtering dates use each audit\'s last update.'));const attempts=[];for(const [route,label] of [['filter','Filter'],['quality','Quality'],['retrieval','Search']])for(const a of j[route]?.attempts||[])attempts.push([label,number(a.job_id),badge(a.status),a.model||'Unknown',badge(a.usage_status),number(a.duration_ms)+' ms']);if(attempts.length)n.append(el('p','muted','Latest saved session audits'),table(['Check','Job','Status','Model','Usage','Duration'],attempts));return n;}
  function health(data) {const s=data.service||{},sizes=Object.values(data.storage?.files_bytes||{}).filter(v=>typeof v==='number');return panel('Service and storage','Status of local data sources.',facts([['Background service',s.running===true?'Running':s.running===false?'Stopped':'Unknown'],['Running service version',s.runtime_version||'Unknown'],['Service started',date(s.started_at)],['State file updated',date(s.state_updated_at)],['Heartbeat',s.heartbeat_at?date(s.heartbeat_at):s.heartbeat_status==='not_recorded'?'Not recorded':'Unknown'],['Stop requested',s.stop_requested===true?'Yes':s.stop_requested===false?'No':'Unknown'],['Projects in service state',number(s.queued_projects)],['Local database',({available:'Available',partial:'Partial data',unavailable:'Unavailable'}[data.status]||'Unknown')],['Database and logs',sizes.length?number(sizes.reduce((a,b)=>a+b,0)/1024/1024)+' MB':'No data']]));}
  function overview(data) {
    const n=el('div');n.append(queue(data),summary(data),processingUsage(data),disclosure('costs','Costs and service tiers',streams(data),pricing(data)),disclosure('jev','Jev checks',jev(data)),disclosure('service','Capture and service status',capture(data),health(data)));return n;
  }
  function processorState(p) {const node=el('div');node.append(badge(p.breaker_status==='blocked'?'failed':'open'));node.firstChild.textContent=p.breaker_status==='blocked'?'Blocked':p.breaker_status==='open'?'Not blocked':'Unknown';if(p.last_error_code)node.append(el('span','path',p.last_error_code));if(p.next_retry_at)node.append(el('span','path','Retry: '+date(p.next_retry_at)));return node;}
  function projects(data) {const n=el('div'),section=panel('Projects','The queue shows unprocessed records and running jobs. Other values cover the selected period.');const search=el('div','search-field'),label=el('label',null,'Search projects'),input=el('input');input.id='project-search';input.type='search';input.placeholder='Name or path';input.value=state.query;input.maxLength=256;label.htmlFor=input.id;let timer;input.addEventListener('input',()=>{state.query=input.value;state.page=1;clearTimeout(timer);timer=setTimeout(load,350);});search.append(label,input);section.append(search);section.append(table(['Project','Tokens','API estimate · USD','Unprocessed / running','Records','Last completed','Processor'],(data.projects||[]).map(p=>{const cell=el('div');cell.append(link(p.name,projectHash(p.project)),el('span','path',p.path));return [cell,number(p.combined?.total_tokens),cost(p.combined),p.queue?.available?number(p.queue.pending)+' / '+number(p.queue.running):'No data',number(p.capture?.available?p.capture.entries:null),date(p.queue?.progress?.last_completed_at),processorState(p)];}),state.query?'No projects match this search.':'No local projects yet.'));section.querySelector('table')?.classList.add('project-table');section.append(pagination(data.pagination));n.append(section);return n;}
  function models(data) {const rows=[];const supported=new Set(data.pricing?.supported_models||[]);for(const [stream,label] of [['main','Main Codex'],['observer','Processing']])for(const r of data.models?.[stream]||[]){const cell=el('div');cell.append(el('span',null,r.value||'Model unknown'));if(r.value&&!supported.has(r.value)&&supported.size)cell.append(el('span','path','Model price unavailable'));rows.push([cell,label,number(r.event_count),number(r.input_tokens),number(r.cached_input_tokens),number(r.output_tokens),number(r.total_tokens),cost(r),money(r.api_equivalent_usd?.standard_scenario_subtotal),money(r.api_equivalent_usd?.fast_scenario_subtotal)]);}const n=panel('Usage by model','Names come from saved metadata. Recorded models and service tiers do not confirm actual charges.',table(['Model','Stream','Responses / attempts','Input','Cache','Output','Total tokens','API estimate · USD','Standard scenario · USD','Fast scenario · USD'],rows,'No model usage records for the selected period.'));const omitted=Object.values(data.model_coverage||{}).reduce((sum,c)=>sum+(c.omitted_groups||0),0);if(omitted)n.append(el('p','muted','Model groups omitted: '+number(omitted)+'. Stream totals include all recorded groups.'));return n;}
  function attemptCost(a) {const m=a.priced_usage?.api_equivalent_usd;if(m?.selected!=null)return money(m.selected);return m?.standard!=null||m?.fast!=null?'Service tier unknown':'Unavailable';}
  function project(data) {
    const n=el('div');n.append(queue(data),summary(data));
    const sessions=panel('Project sessions','Open a session to view streams and processing attempts.',table(['Session','Streams','Tokens','Codex · USD','Processing · USD','Last response'],(data.sessions||[]).map(s=>[link(s.session_id,sessionHash(data.project,s.session_id),'path'),number(s.thread_count??s.threads?.length),number(s.combined?.total_tokens),cost(s.main),cost(s.observer),date(s.main?.observed_through)]),'No saved sessions in this project yet.'));
    sessions.append(pagination(data.pagination));n.append(sessions,disclosure('costs','Costs and service tiers',streams(data),pricing(data)),disclosure('models','Usage by model',models(data)),disclosure('capture','Memory capture',capture(data)),disclosure('jev','Jev checks',jev(data)));return n;
  }
  function tokens(data) {const fields=[['input_tokens','Input'],['cached_input_tokens','Cached input'],['cache_write_input_tokens','Cache writes'],['output_tokens','Output'],['reasoning_output_tokens','Reasoning'],['total_tokens','Total']];return panel('Token breakdown','Cached tokens are part of input tokens. Reasoning tokens are part of output tokens. Do not add these columns together.',table(['Count','Main Codex','Processing','Combined'],fields.map(([key,label])=>[label,number(data.main?.[key]),number(data.observer?.[key]),number(data.combined?.[key])])));}
  function session(data) {const n=el('div');n.append(summary(data),streams(data),disclosure('tokens','Token breakdown',tokens(data)),disclosure('models','Usage by model',models(data)));const threads=data.threads||[];const threadIds=new Set(threads.map(t=>t.thread_id));n.append(disclosure('threads','Thread hierarchy',panel('Thread hierarchy','Parent and child agents within this session.',table(['Thread / parent','Role','Models / provider','Tokens','API estimate · USD'],threads.map(t=>{const node=el('div');const title=el('div',t.is_child?'hierarchy':null);if(t.is_child)title.append(icon('arrow'));title.append(el('span','path',t.agent_nickname||t.agent_path||t.thread_id));node.append(title,el('span','path',t.thread_id));if(t.parent_thread_id)node.append(el('span','path','Parent: '+t.parent_thread_id+(threadIds.has(t.parent_thread_id)?'':' · outside the saved session')));return [node,t.agent_role||'Main',(t.models?.length?t.models.join(', '):'Model not recorded')+' · '+(t.model_provider||'Provider unknown'),number(t.usage?.total_tokens),cost(t.usage)];})))));const jobs=data.jobs||[];n.append(disclosure('jobs','Processing jobs · '+number(jobs.length),panel('Processing jobs','Current state across all dates, including jobs outside the selected period.',table(['Job','Processor / model','State','Attempts','Worker thread','Updated'],jobs.map(j=>[j.id,(j.processor_id||'Unknown')+' · '+(j.model||'Unknown'),badge(j.status),number(j.attempt_count),j.worker_thread_id||'No data',date(j.updated_at)])))));n.append(disclosure('attempts','Processing attempts · '+number((data.processor_attempts||[]).length),panel('Processing attempts','Attempts started in the selected period. All retries count.',table(['Job / attempt','Outcome','Requested model','Tokens','API estimate · USD','Standard scenario · USD','Fast scenario · USD','Usage','Worker thread','Started / duration'],(data.processor_attempts||[]).map(a=>{const out=el('div');out.append(badge(a.outcome));if(a.error_code)out.append(el('span','path',a.error_code));return [String(a.job_id)+' / '+number(a.attempt_count),out,a.model||'Model unknown',number(a.total_tokens),attemptCost(a),money(a.priced_usage?.api_equivalent_usd?.standard),money(a.priced_usage?.api_equivalent_usd?.fast),badge(a.usage_status),a.worker_thread_id||'No data',date(a.started_at)+' · '+(a.duration_ms==null?'No data':number(a.duration_ms)+' ms')];})))));if(data.failure_receipts?.length)n.append(disclosure('failures','Processing failures · '+number(data.failure_receipts.length),panel('Processing failures','Saved session error codes across all dates. Rejected response content is hidden.',table(['Job / attempt','Error code','Reason','Recorded'],data.failure_receipts.map(r=>[String(r.job_id)+' / '+number(r.attempt_count),r.error_code||'Unknown',r.reason_code||'Unknown',date(r.created_at)])))));n.append(disclosure('pricing','Service tiers and estimate coverage',pricing(data)),disclosure('jev','Jev checks',jev(data)));return n;}
  function parseRoute() {const parts=location.hash.slice(1).split('/');try{if(parts[0]==='project'&&parts[1])return {name:'project',project:decodeURIComponent(parts[1])};if(parts[0]==='session'&&parts[1]&&parts[2])return {name:'session',project:decodeURIComponent(parts[1]),session_id:decodeURIComponent(parts[2])};}catch(_){}return {name:parts[0]==='projects'?'projects':'overview'};}
  function setHeading(data) {const r=state.route;$('page-title').textContent=r.name==='overview'?'Overview':r.name==='projects'?'Projects':r.name==='project'?(data.name||'Project'):'Session';$('subtitle').textContent=r.name==='overview'?'Memory processing and token usage.':r.name==='projects'?'Saved local projects.':r.name==='project'?(data.path||r.project):(data.session_id||r.session_id);const crumbs=$('breadcrumbs');crumbs.replaceChildren();if(r.name!=='overview'){crumbs.append(link('Overview','#overview'),el('span',null,'/'));crumbs.append(r.name==='projects'?el('span',null,'Projects'):link('Projects','#projects'));if(r.project){crumbs.append(el('span',null,'/'));const name=r.project.split('/').filter(Boolean).pop()||r.project;crumbs.append(r.name==='session'?link(name,projectHash(r.project)):el('span',null,name));}if(r.name==='session')crumbs.append(el('span',null,'/'),el('span',null,'Session'));}$('nav-overview').removeAttribute('aria-current');$('nav-projects').removeAttribute('aria-current');$(r.name==='overview'?'nav-overview':'nav-projects').setAttribute('aria-current','page');document.title=$('page-title').textContent+' · Codex Mem';}
  function skeletonLine(size='') {return el('span','skeleton-line'+(size?' skeleton-'+size:''));}
  function loadingMetrics() {
    const grid=el('div','metrics metrics-compact');
    for(let i=0;i<3;i++) {const card=el('div','metric');card.append(skeletonLine('label'),skeletonLine('value'),skeletonLine('note'));grid.append(card);}
    return grid;
  }
  function loadingPanel(title, kind='table', columns=6) {
    const section=panel(title),body=el('div','skeleton-'+kind);
    if(kind==='queue') for(let i=0;i<4;i++) {const cell=el('div','queue-cell');cell.append(skeletonLine('value'),skeletonLine('note'));body.append(cell);}
    else if(kind==='facts') for(let i=0;i<5;i++) {const row=el('div','fact');row.append(skeletonLine('label'),skeletonLine('label'));body.append(row);}
    else {body.style.setProperty('--skeleton-columns',String(columns));for(let i=0;i<5;i++){const row=el('div','skeleton-row');for(let j=0;j<columns;j++)row.append(skeletonLine());body.append(row);}}
    section.append(body);return section;
  }
  function loadingView(route) {
    const view=el('div','loading-view');view.setAttribute('aria-hidden','true');
    if(route.name==='projects'){view.append(loadingPanel('Projects','table',7));return view;}
    if(route.name!=='session')view.append(loadingPanel('Memory processing','queue'));
    view.append(loadingMetrics());
    if(route.name==='project')view.append(loadingPanel('Project sessions'));
    else if(route.name==='session')view.append(loadingPanel('Costs and tokens'));
    else {const chart=el('div','usage-chart');for(const key of ['codex','filter','quality','retrieval']){const row=el('div','usage-row usage-'+key);row.append(skeletonLine('label'),skeletonLine(),skeletonLine('label'));chart.append(row);}view.append(panel('Processing token usage',null,chart));}
    return view;
  }
  function captureView() {
    const content=$('content'),active=document.activeElement,focus={node:active,id:active?.id,start:active?.selectionStart,end:active?.selectionEnd,path:null,tag:active?.tagName,href:active?.getAttribute?.('href')};
    if(content.contains(active)){focus.path=[];for(let node=active;node!==content;node=node.parentElement)focus.path.unshift(Array.prototype.indexOf.call(node.parentElement.children,node));}
    focus.detailKey=active?.tagName==='SUMMARY'?active.parentElement?.dataset.detailKey:null;
    return {focus,details:Array.from(content.querySelectorAll('details[data-detail-key]'),node=>[node.dataset.detailKey,node.open]),scroll:[window.scrollX,window.scrollY],tables:Array.from(content.querySelectorAll('.table-wrap'),node=>[node.scrollLeft,node.scrollTop])};
  }
  function restoreView(view) {
    const details=new Map(view.details||[]);$('content').querySelectorAll('details[data-detail-key]').forEach(node=>{if(details.has(node.dataset.detailKey))node.open=details.get(node.dataset.detailKey);});
    const focus=view.focus;let node=focus.node?.isConnected?focus.node:focus.id?$(focus.id):null;
    if(!node&&focus.detailKey)node=Array.from($('content').querySelectorAll('details[data-detail-key]')).find(detail=>detail.dataset.detailKey===focus.detailKey)?.querySelector('summary');
    if(!node&&focus.href)node=Array.from($('content').querySelectorAll('a')).find(link=>link.getAttribute('href')===focus.href);
    if(!node&&focus.path){node=$('content');for(const index of focus.path)node=node?.children[index];if(node?.tagName!==focus.tag||focus.href&&node.getAttribute('href')!==focus.href)node=null;}
    if(node?.focus){node.focus({preventScroll:true});if(typeof focus.start==='number'&&typeof node.setSelectionRange==='function')try{node.setSelectionRange(focus.start,focus.end);}catch(_){}}
    $('content').querySelectorAll('.table-wrap').forEach((node,index)=>{if(view.tables[index])[node.scrollLeft,node.scrollTop]=view.tables[index];});
    window.scrollTo(...view.scroll);
  }
  function updateStatus() {
    const suffix=state.paused?'auto-refresh paused':'every 15 seconds';
    $('updated').textContent=state.busy?(state.data?'Refreshing data… · showing the previous snapshot':'Loading local data…'):state.failure?(state.data?'Refresh failed · showing the previous snapshot':'Data unavailable'):state.lastUpdated?'Updated '+state.lastUpdated+' · '+suffix:'Data not loaded';
    document.body.dataset.loadState=state.failure?'error':state.busy?'loading':'ready';
  }
  function showFailure(error) {
    state.failure=error.message||'Cannot reach the local server.';
    $('error').hidden=false;$('error').textContent=state.failure+(state.data?' Showing previously loaded data. It can be out of date.':'');
    if(!state.data){$('notice').hidden=true;$('content').replaceChildren(el('div','empty','Data did not load. Click Refresh to try again.'));}
  }
  function responseFailure(data) {
    const reason=data.coverage?.refresh_error||data.coverage?.status;
    return ({query_deadline:'The local database timed out. Refresh to try again.',database_busy:'The local database is busy. Refresh to try again.',database_missing:'Local database not found.',database_corrupt:'Cannot read the local database.',database_unreadable:'Cannot access the local database.'}[reason]||'The local database is unavailable. Data could not refresh.');
  }
  async function load() {
    const serial=++state.request;if(state.controller)state.controller.abort();state.controller=new AbortController();state.busy=true;
    const route=state.route||parseRoute(),params=new URLSearchParams({period:state.period});
    if(route.name==='projects'||route.name==='project'){params.set('page',String(state.page));params.set('limit','20');}
    if(route.name==='projects')params.set('query',state.query);if(route.project)params.set('project',route.project);if(route.session_id)params.set('session_id',route.session_id);
    const context=params.toString();
    $('refresh').setAttribute('aria-busy','true');$('content').setAttribute('aria-busy','true');
    if(!state.data){$('content').replaceChildren(loadingView(route));$('error').hidden=true;$('notice').hidden=true;state.failure=null;}
    else if(state.dataContext!==context){$('notice').hidden=false;$('notice').textContent='Loading the selected data. Showing the previous snapshot.';}
    updateStatus();
    try {
      const response=await fetch('/api/'+route.name+'?'+params,{credentials:'same-origin',cache:'no-store',signal:state.controller.signal});
      if(!response.ok)throw new Error(response.status===401||response.status===403?'Access expired. Open the token URL shown in the terminal.':`Data request failed · HTTP ${response.status}`);
      const data=await response.json();if(serial!==state.request)return;
      if(data.status==='unavailable')throw new Error(responseFailure(data));
      const view=captureView();state.data=data;state.dataContext=context;state.failure=null;setHeading(data);
      $('content').replaceChildren(({overview,projects,project,session}[route.name]||overview)(data));restoreView(view);
      $('error').hidden=true;$('export').disabled=false;
      const snapshot=new Date(data.coverage?.snapshot_at||Date.now());
      state.lastUpdated=new Intl.DateTimeFormat('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(Number.isNaN(snapshot.valueOf())?new Date():snapshot);
      const notice=$('notice'),truncated=data.coverage?.truncated_tables||[],interrupted=data.coverage?.interrupted_tables||[];
      notice.hidden=data.status==='available'||data.status==='stale';notice.textContent=data.status==='partial'?'Data is partial. Token counts cover the observed records. Full cost is unavailable.'+(truncated.length?' Limited tables: '+truncated.join(', ')+'.':'')+(interrupted.length?' Incomplete reads: '+interrupted.join(', ')+'.':'')+(data.coverage?.status==='query_deadline'?' The local database timed out. Refresh to try again.':''):'Local data status is unknown.';
      if(data.status==='stale'||data.coverage?.stale)showFailure(new Error(responseFailure(data)));
    } catch(error) {if(error.name==='AbortError'||serial!==state.request)return;showFailure(error);}
    finally {if(serial===state.request){state.busy=false;$('refresh').setAttribute('aria-busy','false');$('content').setAttribute('aria-busy','false');updateStatus();}}
  }
  function navigate() {
    const initial=state.route===null;
    state.route=parseRoute();state.page=1;state.data=null;state.dataContext=null;state.lastUpdated=null;state.failure=null;
    $('export').disabled=true;$('error').hidden=true;$('notice').hidden=true;setHeading({});
    if(!initial)$('main').focus({preventScroll:true});window.scrollTo(0,0);load();
  }
  $('period').addEventListener('change',()=>{state.period=$('period').value;state.page=1;load();});$('refresh').addEventListener('click',()=>load());$('pause').addEventListener('click',()=>{state.paused=!state.paused;$('pause').setAttribute('aria-pressed',String(state.paused));$('pause-label').textContent=state.paused?'Resume':'Pause';$('pause').querySelector('[data-icon]').replaceChildren(icon(state.paused?'play':'pause'));document.body.classList.toggle('paused',state.paused);updateStatus();if(!state.paused)load();});
  function applyTheme(theme) {document.documentElement.classList.add('theme-switching');document.documentElement.dataset.theme=theme;$('theme-label').textContent=theme==='dark'?'Light theme':'Dark theme';$('theme').querySelector('[data-icon]').replaceChildren(icon(theme==='dark'?'sun':'moon'));void document.body.offsetHeight;requestAnimationFrame(()=>document.documentElement.classList.remove('theme-switching'));}
  let initialTheme;try{initialTheme=localStorage.getItem('codex-mem-theme');}catch(_){}applyTheme(initialTheme==='dark'||(!initialTheme&&matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light');$('theme').addEventListener('click',()=>{const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';applyTheme(theme);try{localStorage.setItem('codex-mem-theme',theme);}catch(_){}});
  $('export').addEventListener('click',()=>{if(!state.data)return;const url=URL.createObjectURL(new Blob([JSON.stringify(state.data,null,2)],{type:'application/json'}));const a=link('',url);a.download='codex-mem-'+state.route.name+'-'+(new URLSearchParams(state.dataContext).get('period')||state.period)+'.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);});window.addEventListener('hashchange',()=>{if(location.hash==='#main'){$('main').focus({preventScroll:true});return;}navigate();});setInterval(()=>{if(!state.paused&&!state.busy&&!document.hidden)load({background:true});},15000);navigate();
})();
