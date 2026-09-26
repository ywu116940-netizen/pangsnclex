const icons={grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',book:'<path d="M3 4h6a4 4 0 0 1 3 2 4 4 0 0 1 3-2h6v15h-6a4 4 0 0 0-3 2 4 4 0 0 0-3-2H3zM12 6v15"/>',quiz:'<rect x="5" y="3" width="14" height="18" rx="2"/><path d="m8 9 1 1 2-2m2 1h3m-8 6 1 1 2-2m2 1h3"/>',chart:'<path d="M4 3v17h17M8 15V9m5 6V5m5 10v-7"/>',folder:'<path d="M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v10H3z"/>',shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6zM8 12l3 3 5-6"/>',plus:'<path d="M12 5v14M5 12h14"/>',arrow:'<path d="M4 12h15m-5-5 5 5-5 5"/>',heart:'<path d="M20 5c-3-3-7-1-8 1-1-2-5-4-8-1-4 4 1 9 8 15 7-6 12-11 8-15Z"/><path d="M4 11h4l2-3 3 7 2-4h5"/>',lungs:'<path d="M10 4v7l-4 3m8-10v7l4 3M9 8C6 5 3 11 3 17s6 3 7 1V9m5-1c3-3 6 3 6 9s-6 3-7 1V9"/>',pill:'<path d="M5 19a5 5 0 0 1 0-7l7-7a5 5 0 0 1 7 7l-7 7a5 5 0 0 1-7 0ZM8 9l7 7"/>',bulb:'<path d="M9 18h6m-6 3h6M8 15a7 7 0 1 1 8 0l-1 3H9z"/>',target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',file:'<path d="M14 3H5v18h14V8zM14 3v5h5M8 12h8m-8 4h6"/>',check:'<path d="m5 12 4 4L19 6"/>',lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 4v3"/>'};
const icon=n=>`<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${icons[n]||icons.book}</svg>`;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const samples=[{id:'skin',name:'Skin assessment demo',icon:'book',color:'',sourceName:'Integumentary · Example provided in this chat',source:'Psoriasis is a chronic autoimmune condition characterized by dry, red skin covered with silvery-white scales, papules, and plaques. It commonly affects extensor surfaces such as the scalp, elbows, and knees/shins.\nTinea refers to fungal infections that typically present as annular (ring-like) lesions with raised scaly borders.\nPityriasis rosea typically begins with a single herald patch followed by a Christmas tree pattern distribution.\nEczematous dermatitis (eczema) presents as intensely pruritic, inflamed lesions that commonly affect flexor surfaces without silvery scales.'}].map(m=>({...m,demo:true}));
let stored;try{stored=JSON.parse(localStorage.getItem('studywell-v1')||'null')}catch{};
let migratedTableMarkers=false;
const cleanTableMarker=value=>typeof value==='string'?value.replace(/\[Table row\]\s*/g,''):value;
function cleanModuleMarkers(module){
 for(const key of ['source','formattedSource','sourceName'])if(typeof module[key]==='string'){const clean=cleanTableMarker(module[key]);if(clean!==module[key])migratedTableMarkers=true;module[key]=clean}
 if(Array.isArray(module.structuredSections))for(const section of module.structuredSections)for(const key of ['header','summary_notes'])if(typeof section[key]==='string'){const clean=cleanTableMarker(section[key]);if(clean!==section[key])migratedTableMarkers=true;section[key]=clean}
 if(Array.isArray(module.structuredSections))for(const section of module.structuredSections)for(const key of ['bullet_points','key_terms'])if(Array.isArray(section[key]))section[key]=section[key].map(value=>{const clean=cleanTableMarker(value);if(clean!==value)migratedTableMarkers=true;return clean});
 return module;
}
const legacyModules=Array.isArray(stored?.modules)?stored.modules.map(cleanModuleMarkers):[];
let custom=[...legacyModules],attempts=Array.isArray(stored?.attempts)?stored.attempts:[],hiddenDemo=stored?.hiddenDemo===true;
let modulesFromServer=false,modulesSyncError='';
if(migratedTableMarkers)try{localStorage.setItem('studywell-v1',JSON.stringify({modules:custom,attempts,hiddenDemo}))}catch{}
const formatStates=new Map();let activeSourceId=null,viewOriginal=false;
let draftSource='',draftFormatted='',draftStructuredSections=[],draftOriginalFilename='',draftStoragePath='',draftRequestId=0,draftTimer=null,draftBusy=false,draftError='';
async function requestDocumentStructure(source){
 const bytes=new TextEncoder().encode(source);let binary='';
 for(let i=0;i<bytes.length;i+=0x8000)binary+=String.fromCharCode(...bytes.subarray(i,i+0x8000));
 const response=await fetch('/api/document/extract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:'pasted-notes.txt',content_base64:btoa(binary)})});
 const data=await response.json();
 if(!response.ok||!Array.isArray(data.sections))throw Error(data.error||'Structured extraction is unavailable right now.');
 return data;
}
const sectionText=section=>[section.header,...(section.bullet_points||[]).map(point=>'- '+point)].filter(Boolean).join('\n');
const sectionsAsText=sections=>sections.map(section=>[sectionText(section),...(Array.isArray(section.subsections)?section.subsections:[]).map(subsection=>sectionText(subsection).split('\n').map(line=>'  '+line).join('\n'))].filter(Boolean).join('\n')).join('\n\n');
const subsectionAsHtml=subsection=>`<div class="extracted-subsection"><div class="extracted-subsection-head"><strong>${esc(subsection.header)}</strong><span>Page ${esc(subsection.page_number??'—')}</span></div>${subsection.bullet_points?.length?`<ul>${subsection.bullet_points.map(point=>`<li>${esc(point)}</li>`).join('')}</ul>`:''}${subsection.key_terms?.length?`<p><b>Key terms</b> ${subsection.key_terms.map(term=>`<span class="term-token">${esc(term)}</span>`).join(' ')}</p>`:''}${subsection.summary_notes?`<p class="section-summary">${esc(subsection.summary_notes)}</p>`:''}</div>`;
const sectionsAsHtml=sections=>sections.map(section=>`<article class="extracted-section"><div class="extracted-section-head"><strong>${esc(section.header)}</strong><span>Page ${esc(section.page_number??'—')}</span></div>${section.bullet_points?.length?`<ul>${section.bullet_points.map(point=>`<li>${esc(point)}</li>`).join('')}</ul>`:''}${section.key_terms?.length?`<p><b>Key terms</b> ${section.key_terms.map(term=>`<span class="term-token">${esc(term)}</span>`).join(' ')}</p>`:''}${section.summary_notes?`<p class="section-summary">${esc(section.summary_notes)}</p>`:''}${Array.isArray(section.subsections)&&section.subsections.length?`<div class="extracted-subsections">${section.subsections.map(subsectionAsHtml).join('')}</div>`:''}</article>`).join('');
function showDraftPreview(){
 const preview=document.getElementById('draftFormatPreview');
 if(draftStructuredSections.length)preview.innerHTML=sectionsAsHtml(draftStructuredSections);else preview.textContent=draftFormatted;
 document.getElementById('draftFormatStatus').textContent=draftError|| (draftBusy?'Organizing your notes…':draftStructuredSections.length?`Structured sections ready · ${draftStructuredSections.length} section(s). Review before saving.`:draftFormatted?'Formatted layout ready. Save the module to keep it.':draftSource.length<80&&draftSource?'Add at least 80 characters to preview your notes.':'Paste or upload your notes to see the formatted layout.');
 document.getElementById('retryDraftFormat').hidden=!draftError;
}
function resetDraft(source='',formatted='',sections=[]){
 clearTimeout(draftTimer);draftRequestId++;draftSource=source;draftFormatted=formatted;draftBusy=false;draftError='';draftStructuredSections=Array.isArray(sections)?sections:[];showDraftPreview();
}
async function formatDraft(){
 const source=draftSource,id=++draftRequestId;
 if(source.length<80)return;
 draftBusy=true;draftError='';showDraftPreview();
 try{const result=await requestDocumentStructure(source);if(id!==draftRequestId)return;draftStructuredSections=result.sections;draftFormatted=sectionsAsText(result.sections);draftError=(result.warnings||[]).join(' ');draftBusy=false;showDraftPreview();}
 catch(error){if(id!==draftRequestId)return;draftBusy=false;draftError=error.message||'Formatting is unavailable. Your original text is safe.';showDraftPreview();}
}
function queueDraftFormat(immediate=false){
 const source=document.getElementById('sourceText').value.trim();
 if(source===draftSource&&draftFormatted)return;
 resetDraft(source);
 if(source.length>=80)draftTimer=setTimeout(()=>void formatDraft(),immediate?0:1100);
}
function showSource(){
 const m=allModules().find(x=>x.id===activeSourceId);if(!m)return;
 document.getElementById('sourceContent').textContent=viewOriginal||!m.formattedSource?m.source:m.formattedSource;
 document.getElementById('sourceToggle').textContent=viewOriginal?'View formatted':'View original';
 document.getElementById('sourceToggle').hidden=!m.formattedSource;
 const state=formatStates.get(m.id),button=document.getElementById('sourceFormat');button.disabled=state?.busy===true;button.textContent=state?.busy?'Formatting…':m.formattedSource?'Reformat':'Format for reading';
 document.getElementById('sourceFormatStatus').textContent=state?.error|| (state?.busy?'Organizing your notes…':m.formattedSource?'Original text is preserved.':'Original text shown.');
}
async function formatModule(id){
 const m=custom.find(x=>x.id===id);if(!m||formatStates.get(id)?.busy)return;
 const source=m.source,state={busy:true};formatStates.set(id,state);if(activeSourceId===id)showSource();
 try{
  const result=await requestDocumentStructure(source),formatted=sectionsAsText(result.sections);
  const latest=custom.find(x=>x.id===id);if(latest?.source!==source||formatStates.get(id)!==state)return;
  latest.formattedSource=formatted;latest.structuredSections=result.sections;
  if(modulesFromServer)Object.assign(latest,applyServerModule(await persistModule(latest,true)));
  save();formatStates.set(id,{});
 }catch(error){if(formatStates.get(id)===state)formatStates.set(id,{error:error.message||'Formatting is unavailable. The original material is saved.'});}
 if(activeSourceId===id)showSource();
}
let page='dashboard',filter='all',quiz=null;let generationBusy=false;let generationProgress='';let generationProgressTimer=null;let practiceError='';let generationPipeline=null;let selectedModuleIds=new Set(custom.length?[custom[0].id]:hiddenDemo?[]:['skin']);let questionCount=25;let editingModuleId=null,deletingModuleId=null;let questionBank={moduleId:null,module:null,items:[],loading:false,generating:false,error:''};let statusRequestId=0;let generationStatus={enabled:null,message:'Checking clinical generation availability…'};const allModules=()=>custom.length?custom:(hiddenDemo?[]:samples);const demoOnly=()=>selectedModuleIds.size===1&&selectedModuleIds.has('skin')&&allModules().some(m=>m.id==='skin'&&m.demo);
function save(){try{localStorage.setItem('studywell-v1',JSON.stringify({modules:modulesFromServer?[]:custom,attempts,hiddenDemo}))}catch{toast('Storage is full. This session’s changes could not be saved.')}}
async function moduleRequest(path,options={}){
 const response=await fetch(path,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});
 let data={};try{data=await response.json()}catch{}
 if(!response.ok)throw Error(data.error||'Shared module service is unavailable.');
 return data;
}
function modulePayload(module){return {id:module.id,name:module.name,original_filename:module.originalFilename||'',storage_path:module.storagePath||'',source_text:module.source||'',formatted_text:module.formattedSource||'',structured_sections:Array.isArray(module.structuredSections)?module.structuredSections:[]}}
async function persistModule(module,updating=false){
 const data=await moduleRequest(updating?`/api/modules/${encodeURIComponent(module.id)}`:'/api/modules',{method:updating?'PUT':'POST',body:JSON.stringify(modulePayload(module))});
 return data.module;
}
function applyServerModule(module){
 return cleanModuleMarkers({...module,sourceName:module.sourceName||`${module.name} · Study notes`,icon:module.icon||'book',color:module.color||'',demo:false});
}
async function loadSharedModules(){
 try{
  let data=await moduleRequest('/api/modules',{cache:'no-store'});
  if(!data.modules?.length&&legacyModules.length){
   for(const legacy of legacyModules)await persistModule(legacy,false);
   data=await moduleRequest('/api/modules',{cache:'no-store'});
  }
  custom=(data.modules||[]).map(applyServerModule);
  modulesFromServer=true;hiddenDemo=false;modulesSyncError='';
  selectedModuleIds=new Set(custom.length?[custom[0].id]:['skin']);
  save();render();
 }catch(error){modulesSyncError=error.message||'Shared modules could not be loaded.';render();}
}
function toast(t){const el=document.getElementById('toast');el.textContent=t;el.classList.add('show');clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>el.classList.remove('show'),3500)}
function facts(source){const lines=source.split(/\n+/).map(x=>x.replace(/^\s*[-*#]+\s*/,'').trim()).filter(Boolean);const pairs=lines.map(x=>{const n=x.indexOf(':');return n>1&&n<100&&x.slice(n+1).trim().length>15?{term:x.slice(0,n).trim(),definition:x.slice(n+1).trim(),quote:x}:null}).filter(Boolean);if(pairs.length>=4)return {mode:'topic',items:pairs.filter((x,i,a)=>a.findIndex(y=>y.term.toLowerCase()===x.term.toLowerCase()||y.definition.toLowerCase()===x.definition.toLowerCase())===i)};const sentences=source.match(/[^.!?\n]+[.!?]?/g)||[];const stop=new Set('their about which these those where before after between through should would could during patient nursing patients assessment information because describe include record observe'.split(' '));const items=sentences.map(s=>{s=s.trim();const words=s.match(/[a-zA-Z][a-zA-Z-]{4,}/g)||[];const term=words.filter(w=>!stop.has(w.toLowerCase())).sort((a,b)=>b.length-a.length)[0];return term&&s.length>35?{term,definition:s,quote:s}:null}).filter(Boolean).filter((x,i,a)=>a.findIndex(y=>y.term.toLowerCase()===x.term.toLowerCase())===i);return{mode:'cloze',items}}
function shuffle(a){return [...a].sort(()=>Math.random()-.5)}
const attemptsForModule=id=>attempts.filter(a=>a.moduleIds?.includes(id)||a.moduleId===id);const answers=()=>attempts.flatMap(a=>a.answers);const score=()=>answers().length?Math.round(answers().filter(a=>a.correct).length/answers().length*100):null;
const moduleProgress=id=>{const m=allModules().find(m=>m.id===id);const total=facts(m.source).items.length;const done=new Set(attemptsForModule(id).flatMap(a=>a.answers.map(x=>x.term))).size;return Math.min(100,Math.round(done/total*100))};
function go(p){page=p;render();window.scrollTo({top:0,behavior:'instant'})}
function render(){const titles={dashboard:'Dashboard',modules:'Study modules',practice:'Practice quiz',progress:'My progress',materials:'My materials',bank:'Question Bank'};document.getElementById('app').innerHTML=`<aside class="sidebar"><div class="brand"><span class="mascot-mark"><img src="mascot.png" alt="Studywell cat mascot" width="40" height="40"></span><div>studywell<small>NCLEX-RN PRACTICE</small></div></div><div class="nav-caption">YOUR STUDY SPACE</div><nav>${[['dashboard','grid','Dashboard'],['modules','book','Study modules'],['practice','quiz','Practice quiz'],['bank','quiz','Question Bank'],['progress','chart','My progress'],['materials','folder','My materials']].map(([p,i,t])=>`<button class="nav-button ${page===p?'active':''}" data-page="${p}" ${page===p?'aria-current="page"':''}>${icon(i)}<span>${t}</span></button>`).join('')}</nav><div class="sidebar-bottom"><div class="source-promise">${icon('shield')}<strong>Grounded in your materials.</strong>Every question starts with your notes. Every answer leads back to the source.</div><div class="profile"><div class="avatar">S</div><div>Your study space<small>Future RN, one step at a time</small></div></div></div></aside><main class="main"><header class="topbar"><div class="breadcrumb">Your workspace <span style="padding:0 12px;color:#c7d1dc">/</span> <strong>${titles[page]}</strong></div><div class="brand mobile-brand"><span class="mascot-mark"><img src="mascot.png" alt="Studywell cat mascot" width="40" height="40"></span>studywell</div><div class="topbar-right"><span class="track-pill">NCLEX-RN</span><span class="avatar">S</span></div></header><div class="content">${generationStatus.enabled===false?`<div class="notice" role="status">${icon('shield')} ${esc(generationStatus.message||'Clinical generation is paused pending cost approval.')} You can still upload and read your materials.</div>`:''}${page==='dashboard'?dashboard():page==='modules'?modulesPage():page==='practice'?practice():page==='progress'?progressPage():page==='bank'?questionBankPage():materialsPage()}<footer class="footer"><span>${icon('shield')} Built around your notes. Designed for your next step.</span><span>Independent study tool · Not affiliated with NCSBN</span></footer></div></main>`;bind()}
function heading(title,sub,action=true){return `<div class="page-heading"><div><h1>${title}</h1><p>${sub}</p></div>${action?`<button class="button primary" data-new-quiz>${icon('plus')} Create a quiz</button>`:''}</div>`}
function stats(){return `<div class="stats"><div class="stat"><div class="stat-top">Questions practiced ${icon('quiz')}</div><div class="stat-value">${answers().length}</div><div class="stat-sub">Every question is a step forward</div></div><div class="stat"><div class="stat-top">Overall accuracy ${icon('target')}</div><div class="stat-value">${score()===null?'—':score()+'<span style="font-size:17px">%</span>'}</div><div class="stat-sub">${score()===null?'Your first quiz starts the story':'Across your completed quizzes'}</div></div><div class="stat"><div class="stat-top">Study modules ${icon('book')}</div><div class="stat-value">${allModules().length}</div><div class="stat-sub">${custom.length} shared · ${custom.length?'Synced to Supabase':'Demo fallback'}</div></div></div>`}
function cards(ms){return `<div class="modules-grid">${ms.map(m=>`<article class="module"><div class="module-top"><div class="module-icon ${m.color||''}">${icon(m.icon||'book')}</div><span class="badge">${m.demo?'Demo module':'Your material'}</span></div><h3>${esc(m.name)}</h3><p>${m.source.split(/\s+/).length} words <span style="padding:0 5px">·</span> 1 source</p><div class="module-progress"><div class="progress-label"><span>${attemptsForModule(m.id).length?'Practice sessions':'Ready when you are'}</span><span>${attemptsForModule(m.id).length} completed</span></div></div><div class="module-footer"><button class="text-button source-btn" data-source="${esc(m.id)}">View source</button><button class="text-button" data-bank-select="${esc(m.id)}">Question Bank</button><button class="button primary module-practice" data-start="${esc(m.id)}">Practice ${icon('arrow')}</button></div><div class="module-manage"><button class="text-button" data-edit="${esc(m.id)}">Edit module</button><button class="text-button delete-link" data-delete="${esc(m.id)}">Delete</button></div></article>`).join('')}</div>`}
function weekly(){const weekAgo=Date.now()-7*86400000;return attempts.filter(a=>a.date>weekAgo).reduce((n,a)=>n+a.answers.length,0)}
function weekPanel(){const n=weekly();return `<section class="panel"><div class="panel-title"><h2>Your weekly goal</h2>${icon('target')}</div><div class="ring" style="--angle:${Math.min(n/30,1)*360}deg"><div class="ring-center"><strong>${n}<span style="display:inline;font-size:17px;font-weight:500;color:#a3b2c2"> / 30</span></strong><span>questions practiced</span></div></div><p class="goal-desc">${n>=30?'You reached your goal. Nice work.':n?'A little practice adds up.':'Small steps. Steady progress.'}<br><strong>${n>=30?'Keep building your confidence.':`${30-n} questions to your weekly goal`}</strong></p><div class="weekdays">${Array.from({length:7},(_,i)=>{const d=new Date();d.setDate(d.getDate()-6+i);const did=attempts.some(a=>new Date(a.date).toDateString()===d.toDateString());return`<div class="day ${did?'done':''}"><b>${did?'✓':'·'}</b>${d.toLocaleDateString('en',{weekday:'narrow'})}</div>`}).join('')}</div></section>`}
function activities(limit=3){return attempts.length?[...attempts].reverse().slice(0,limit).map(a=>`<div class="activity-row"><div>${esc(a.name)}<br><small>${new Date(a.date).toLocaleDateString('en',{month:'short',day:'numeric'})} · ${a.answers.length} questions</small></div><span class="activity-score">${Math.round(a.answers.filter(x=>x.correct).length/a.answers.length*100)}%</span></div>`).join(''):`<div class="empty-activity">${icon('clock')}<span>Your completed quizzes will appear here.<br>Try a demo module or bring your own notes.</span></div>`}
function dashboard(){return `${heading('A little practice. A lot of possibility.','Make space for progress, one question at a time.')}<div class="dashboard-grid"><div><section class="welcome"><div><span class="eyebrow">YOUR NEXT CHAPTER STARTS HERE</span><h2>Your notes.<br>Your path to confident care.</h2><p>Turn what you’re learning into focused practice, with explanations you can trace back to the source.</p><button class="button primary" ${allModules().length?`data-start="${esc(custom[0]?.id||allModules()[0].id)}"`:'data-create'}>${allModules().length?(attempts.length?'Keep practicing':'Try a practice quiz'):'Add study material'} ${icon('arrow')}</button></div><figure class="mascot-welcome"><div class="mascot-portrait"><img src="mascot.png" alt="Your tabby cat, Studywell’s study buddy" width="180" height="180"></div><figcaption>Your study buddy.<br><strong>Here for every small win.</strong></figcaption></figure></section>${stats()}<section><div class="section-title"><h2>Your study modules</h2><button class="text-button" data-page="modules">View all modules ${icon('arrow')}</button></div>${cards(allModules().slice(0,4))}</section><section class="activity"><div class="section-title"><h2>Recent practice</h2><button class="text-button" data-page="progress">View progress</button></div>${activities()}</section></div><aside class="right-panel">${weekPanel()}<section class="tip">${icon('bulb')}<h3>Understand the “why.”</h3>After each question, take a moment to read the rationale—even when you get it right. Connect the answer back to your notes.</section><section class="panel"><div class="panel-title"><h2>Make it yours</h2>${icon('file')}</div><p class="small-copy">Your lectures. Your study guides. Your own words.<br>Add your material to build a module around what you’re learning.</p><button class="text-button" data-create style="margin-top:17px">Add study material ${icon('plus')}</button></section></aside></div>`}
function modulesPage(){const ms=allModules().filter(m=>filter==='all'||(filter==='mine'?!m.demo:m.demo));return`${heading('Your study modules','A focused place for everything you’re learning.')}<div class="toolbar"><select id="moduleFilter" aria-label="Filter modules"><option value="all" ${filter==='all'?'selected':''}>All modules (${allModules().length})</option><option value="mine" ${filter==='mine'?'selected':''}>Shared modules (${custom.length})</option><option value="demo" ${filter==='demo'?'selected':''}>Demo modules (${hiddenDemo?0:samples.length})</option></select></div>${modulesSyncError?`<div class="notice" role="alert">${icon('shield')} ${esc(modulesSyncError)}${custom.length?' Using the saved local copy for now.':''}</div>`:''}${hiddenDemo?`<button class="text-button restore-demo" id="restoreDemo">Restore built-in example</button>`:''}${ms.length?cards(ms):`<section class="panel result"><h2>A fresh page for your notes.</h2><p>Add your material to create your first shared module.</p><button class="button primary" data-create>${icon('plus')} Add study material</button></section>`}`}
async function loadQuestionBank(id){
 const module=allModules().find(item=>item.id===id);if(!module)return;
 questionBank={...questionBank,moduleId:id,module,loading:true,generating:false,error:''};render();
 try{const response=await fetch(`/api/modules/${encodeURIComponent(id)}/questions`,{cache:'no-store'});const result=await response.json();if(!response.ok)throw Error(result.error||'Question Bank could not be loaded.');questionBank={...questionBank,moduleId:id,module:applyServerModule(result.module||module),items:Array.isArray(result.items)?result.items:[],loading:false,error:''};}
 catch(error){questionBank={...questionBank,loading:false,error:error.message||'Question Bank could not be loaded.'};}
 render();
}
function questionBankPage(){
 const modules=allModules();
 if(!modules.length)return`${heading('Question Bank','Save source-grounded questions for practice.',false)}<section class="panel result"><h2>Add a module first.</h2><p>Question Banks belong to a study module.</p><button class="button primary" data-create>${icon('plus')} Add study material</button></section>`;
 const selected=modules.find(m=>m.id===questionBank.moduleId);
 const chooser=`<div class="toolbar"><label for="bankModule">Module <select id="bankModule">${modules.map(m=>`<option value="${esc(m.id)}" ${m.id===questionBank.moduleId?'selected':''}>${esc(m.name)}</option>`).join('')}</select></label></div>`;
 if(!selected)return`${heading('Question Bank','Practice saved questions without another AI request.',false)}${chooser}<section class="panel result"><h2>Choose a module.</h2><p>Each module has its own saved question bank.</p></section>`;
 if(questionBank.loading)return`${heading('Question Bank',esc(selected.name),false)}${chooser}<section class="panel result"><h2>Loading saved questions…</h2></section>`;
 const items=questionBank.items||[];
 return`${heading('Question Bank',esc(selected.name),false)}${chooser}${questionBank.error?`<div class="notice practice-error" role="alert">${icon('shield')} ${esc(questionBank.error)}</div>`:''}<section class="panel bank-toolbar"><div><span class="eyebrow">${esc(selected.name)}</span><h2>${items.length} saved question${items.length===1?'':'s'}</h2><p class="small-copy">Practice from this bank without calling Hikari.</p></div><div class="buttons"><button class="button primary" data-bank-practice ${items.length?'':'disabled'}>${icon('quiz')} Practice all</button><label>Generate more <select id="bankCount"><option value="10">10</option><option value="25">25</option><option value="50">50</option><option value="100">100</option></select></label><button class="button secondary" data-bank-generate ${questionBank.generating?'disabled':''}>${questionBank.generating?'Generating…':'Generate more'} ${icon('plus')}</button></div></section><section class="activity"><div class="section-title"><h2>Saved questions</h2><span class="badge">${items.length} total</span></div>${items.length?items.map((q,index)=>`<article class="bank-question"><strong>${index+1}. ${esc(q.stem)}</strong><small>${esc(q.concept||q.category||'Clinical reasoning')}</small></article>`).join(''):`<p class="small-copy">No questions saved yet. Generate a bank from this module’s source material.</p>`}</section>`;
}
function bankQuizQuestion(q,module){return {...q,prompt:q.stem,correct:q.correctIndex,fact:{term:q.concept,quote:(q.stemEvidence||[]).map(e=>e.quote).join('\n\n')},mode:'clinical',demo:false};}
async function generateMoreBank(){
 const id=questionBank.moduleId,count=Number(document.getElementById('bankCount')?.value||10);if(!id||questionBank.generating)return;
 questionBank={...questionBank,generating:true,error:''};render();
 try{const response=await fetch(`/api/modules/${encodeURIComponent(id)}/questions/generate`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({count})});const result=await response.json();if(!response.ok)throw Error(result.error||'Question generation failed.');questionBank={...questionBank,generating:false,items:[...(questionBank.items||[]),...(result.items||[])]};toast(`${result.inserted_count||0} new question${result.inserted_count===1?'':'s'} added. ${result.duplicate_count||0} duplicate${result.duplicate_count===1?'':'s'} skipped.`);}
 catch(error){questionBank={...questionBank,generating:false,error:error.message||'Question generation failed.'};}
 render();
}
function practiceQuestionBank(){
 const module=allModules().find(item=>item.id===questionBank.moduleId),items=questionBank.items||[];if(!module||!items.length)return;
 quiz={module,modules:[module],questions:items.map(q=>bankQuizQuestion(q,module)),requestedCount:items.length,index:0,selected:null,checked:false,answers:[],done:false};practiceError='';go('practice');
}
async function start(id,count){
  if(generationBusy)return;
  if(id && allModules().some(m=>m.id===id)){selectedModuleIds=new Set([id]);}
  const selected=allModules().filter(m=>selectedModuleIds.has(m.id));
  const requested=count||questionCount;
  if(!selected.length){practiceError='Choose at least one study module.';go('practice');return;}
  if(selected.length>5){practiceError='Choose no more than five modules for one quiz.';go('practice');return;}
  if(selected.reduce((n,m)=>n+m.source.length,0)>100000){practiceError='Choose smaller modules totaling no more than 100,000 characters.';go('practice');return;}
  const isDemo=selected.length===1&&selected[0].id==='skin'&&selected[0].demo;
  if(!isDemo&&generationStatus.enabled!==true)await refreshGenerationStatus();
  if(!isDemo&&!generationStatus.enabled){practiceError=generationStatus.message||'Clinical generation is unavailable.';go('practice');return;}
  const m=selected.length===1?selected[0]:{id:'selection:'+selected.map(m=>m.id).join(','),name:selected.map(m=>m.name).join(' + '),sourceName:'Selected modules',source:selected.map(m=>m.name+'\n'+m.source).join('\n\n'),demo:selected.every(m=>m.demo)};
  practiceError='';generationBusy=true;quiz=null;generationPipeline=null;go('practice');
  try{
    generationProgress=isDemo?'Preparing the example question…':`Generating ${requested} questions in parallel source-grounded batches…`;render();clearInterval(generationProgressTimer);generationProgressTimer=setInterval(()=>{if(generationBusy){generationProgress=`Generating ${requested} questions… Each batch is being checked for evidence and duplicates.`;render()}},3500);const res=await fetch(isDemo?'/api/quiz/demo':'/api/quiz/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({count:isDemo?1:requested,sources:selected.map(m=>({id:m.id,name:m.name,text:m.source,structuredSections:m.structuredSections||[]}))})});
    const result=await res.json();
    if(!res.ok)throw new Error(result.error||'Generation could not finish. Please try again.');
    if(result.status!=='ready'||!result.items?.length)throw new Error(result.message||'This material does not support enough clinical detail for a defensible quiz.');
    const qs=result.items.map(q=>({...q,prompt:q.stem,correct:q.correctIndex,fact:{term:q.concept,quote:q.stemEvidence.map(e=>e.quote).join('\n\n')},mode:'clinical',demo:isDemo}));
    generationPipeline=isDemo?null:result.pipeline||null;
    quiz={module:m,modules:selected,questions:qs,requestedCount:isDemo?1:requested,index:0,selected:null,checked:false,answers:[],done:false};
    if(!isDemo&&qs.length<requested)toast('Only '+qs.length+' question'+(qs.length===1?'':'s')+' passed the clinical and source checks.');
  }catch(error){practiceError=error.message||'Generation could not finish. Please try again.';toast(practiceError);}
  finally{generationBusy=false;clearInterval(generationProgressTimer);generationProgress='';render();}
}
function practice(){if(generationBusy)return heading('Preparing your clinical quiz',generationProgress||'Writing scenarios and checking every rationale against your selected material.',false)+'<section class="panel result" role="status"><h2>Generating and checking your questions…</h2><p>'+esc(generationProgress||'Your selected modules are being distributed across source-grounded batches.')+'</p></section>';if(!quiz)return`${heading('A focused moment to practice','Choose your modules, then take it one question at a time.',false)}${practiceError?`<div class="notice practice-error" role="alert">${icon('shield')} ${esc(practiceError)}</div>`:''}<div class="notice">${icon('shield')} Only selected modules are sent for generation. The skin example is best for a one-question trial.</div>${generationStatus.enabled===true?`<p class="pipeline-status">Ready: Hikari will run <strong>structured chunks</strong> → <strong>parallel batches</strong> → <strong>source and duplicate checks</strong>.</p>`:''}<section class="panel quiz-setup"><div class="section-title"><div><h2>Choose your quiz material</h2><p>Select one module for focused practice, or several for a combined quiz.</p></div><button class="button secondary" data-create>${icon('plus')} Add study material</button></div><div class="module-choices">${allModules().map(m=>`<label class="module-choice"><input type="checkbox" data-quiz-source="${esc(m.id)}" ${selectedModuleIds.has(m.id)?'checked':''}><span><strong>${esc(m.name)}</strong><small>${m.demo?'Demo notes':'Shared material'} · ${m.source.split(/\s+/).length} words</small></span></label>`).join('')}</div><div class="quiz-setup-actions"><label for="questionCount">Questions <select id="questionCount"><option value="1" ${demoOnly()||questionCount===1?'selected':''}>1</option>${demoOnly()?'':`<option value="10" ${questionCount===10?'selected':''}>10</option><option value="25" ${questionCount===25?'selected':''}>25</option><option value="50" ${questionCount===50?'selected':''}>50</option><option value="100" ${questionCount===100?'selected':''}>100</option>`}</select></label><button class="button primary" id="generateSelected">Generate quiz ${icon('arrow')}</button></div><p class="form-note">Choose up to five modules, totaling 100,000 characters. Questions use section-aware batches and only those modules. A 100-question request uses more API credits and may return fewer questions when the selected material cannot support 100 distinct, defensible items.</p></section>`;const q=quiz.questions[quiz.index];if(quiz.done)return`${heading('Practice complete','Progress happens one thoughtful question at a time.',false)}<section class="quiz-card result"><div class="result-icon">✓</div><h2>You showed up for your learning.</h2><div class="big-score">${Math.round(quiz.answers.filter(a=>a.correct).length/quiz.answers.length*100)}%</div><p>${quiz.answers.filter(a=>a.correct).length} of ${quiz.answers.length} correct · ${esc(quiz.module.name)}<br>Your session has been saved to your progress.</p><div class="buttons"><button class="button primary" data-repeat>Practice again</button><button class="button secondary" data-page="progress">View my progress</button></div></section><section class="activity"><h2>Review your session</h2>${quiz.answers.map((a,i)=>`<details class="rationale"><summary>${a.correct?'✓':'○'} Question ${i+1} · ${esc(a.term)}</summary><p>${esc(quiz.questions[i].prompt)}</p><p><strong>Your answer:</strong> ${esc(quiz.questions[i].options[a.selected])}</p><p><strong>Correct answer:</strong> ${esc(quiz.questions[i].options[quiz.questions[i].correct])}</p><p>${esc(quiz.questions[i].correctRationale||'')}</p>${(quiz.questions[i].optionRationales||[]).filter(r=>r.optionIndex!==quiz.questions[i].correct).map(r=>`<p><strong>${'ABCD'[r.optionIndex]}.</strong> ${esc(r.explanation)}</p>`).join('')}<blockquote class="source-quote">${esc(quiz.questions[i].fact.quote)}</blockquote></details>`).join('')}</section>`;return`${heading('Your focus, one question at a time.','Read carefully. Trust what you’ve learned. Review the why.',false)}${quiz.requestedCount&&quiz.questions.length<quiz.requestedCount?`<div class="notice" role="status">${icon('shield')} ${quiz.questions.length} of ${quiz.requestedCount} requested questions passed the clinical and source checks. Add more detailed material for a longer quiz.</div>`:''}<div class="toolbar"><span class="badge">${esc(quiz.modules.map(m=>m.name).join(' + '))}</span><button class="text-button" data-new-quiz>New quiz</button><span class="badge">${quiz.module.demo?'Demo notes':'Your material'} · Clinical reasoning</span>${generationPipeline?`<span class="badge pipeline-badge">Hikari · chunks → batches</span>`:''}</div><div class="quiz-layout"><section class="quiz-card"><div class="quiz-meta"><span>Question ${quiz.index+1} of ${quiz.questions.length}</span><span>Single best answer</span></div><div class="progress-track"><span style="width:${quiz.index/quiz.questions.length*100}%"></span></div><span class="eyebrow">${esc(quiz.module.name)}</span><h2 class="question">${esc(q.prompt)}</h2><div role="radiogroup" aria-label="Choose an answer">${q.options.map((o,i)=>`<label class="option ${quiz.selected===i?'selected':''} ${quiz.checked?(i===q.correct?'correct':quiz.selected===i?'incorrect':''):''}"><input type="radio" name="answer" value="${i}" ${quiz.selected===i?'checked':''} ${quiz.checked?'disabled':''}><span class="letter">${'ABCD'[i]}</span><span>${esc(o)}</span></label>`).join('')}</div>${quiz.answerError?'<p class="answer-hint" role="alert">Choose one answer above, then check it.</p>':''}${quiz.checked?rationale(q):''}<div class="quiz-actions"><button class="text-button" data-source="${esc(quiz.module.id)}">${icon('file')} View material</button>${quiz.checked?`<button class="button primary" id="nextQuestion">${quiz.index===quiz.questions.length-1?'Finish quiz':'Next question'} ${icon('arrow')}</button>`:`<button class="button primary" id="checkAnswer">Check answer ${icon('arrow')}</button>`}</div></section><aside><section class="panel"><h2>This practice session</h2><p class="small-copy" style="margin-top:5px">${quiz.questions.length} question${quiz.questions.length===1?'':'s'} · Untimed</p><div class="quiz-map">${quiz.questions.map((_,i)=>`<span class="${i===quiz.index?'current':quiz.answers[i]?(quiz.answers[i].correct?'right':'wrong'):''}">${i+1}</span>`).join('')}</div><p class="small-copy">Take your time. Your rationale appears after you check each answer.</p></section><section class="tip" style="margin-top:20px">${icon('shield')}<h3>Your source stays in sight.</h3>Each explanation links directly to a passage in your study material. Questions are checked against your selected material before they appear.</section></aside></div>`}
function rationale(q){
  if(q.mode!=='clinical')return '';
  const evidence=(items)=>items.map(e=>`<blockquote class="source-quote"><span class="eyebrow" style="display:block;margin-bottom:7px">${esc((quiz.modules.find(m=>m.id===e.sourceId)?.sourceName||quiz.module.sourceName))}</span>“${esc(e.quote)}”</blockquote>`).join('');
  const optionRationales=Array.isArray(q.optionRationales)?q.optionRationales:[];
  const correctOptionRationale=optionRationales.find(r=>r.optionIndex===q.correct);
  const distractorRationales=optionRationales.filter(r=>r.optionIndex!==q.correct);
  const sourceEvidence=[...(Array.isArray(q.stemEvidence)?q.stemEvidence:[]),...(correctOptionRationale?.evidence||[])].filter((e,i,all)=>all.findIndex(other=>other.sourceId===e.sourceId&&other.quote===e.quote)===i);
  const distractorDetails=distractorRationales.length?`<details open><summary>Why the other choices don’t fit</summary><ul>${distractorRationales.map(r=>`<li><strong>${'ABCD'[r.optionIndex]}. ${esc(q.options[r.optionIndex])}</strong><p>${esc(r.explanation)}</p>${evidence(r.evidence||[])}</li>`).join('')}</ul></details>`:'';
  return `<section class="rationale" aria-live="polite"><h3>${quiz.selected===q.correct?'✓ That’s correct.':'Let’s work through this.'}</h3><p><strong>Correct answer: ${'ABCD'[q.correct]}</strong> · ${esc(q.options[q.correct])}</p><p>${esc(q.correctRationale||'')}</p>${evidence(sourceEvidence)}${distractorDetails}<p class="form-note">${q.demo?'Authored practice example from the skin-condition text you supplied.':'AI-generated study practice. Review the cited material when checking clinical details.'}</p></section>`;
}
function progressPage(){const max=Math.max(1,...Array.from({length:7},(_,i)=>dayCount(i)));return`${heading('Your progress, made visible.','Every session is another step toward confidence.',false)}${stats()}<div class="progress-view"><section class="panel"><div class="section-title"><h2>Your last 7 days</h2><span class="badge">Questions practiced</span></div><div class="chart" role="img" aria-label="Questions practiced each day over the last week">${Array.from({length:7},(_,i)=>{const d=new Date();d.setDate(d.getDate()-6+i);const n=dayCount(i);return`<div class="chart-col"><small>${n}</small><div class="chart-bar" style="height:${n/max*140}px"></div><span>${d.toLocaleDateString('en',{weekday:'short'})}</span></div>`}).join('')}</div><p class="small-copy">${attempts.length?'Consistency creates room for confidence. Keep going.':'Your chart will grow as you complete practice sessions.'}</p></section>${weekPanel()}<section class="panel"><h2>Sessions by module</h2>${allModules().map(m=>`<div class="module-report"><div class="progress-label"><span>${esc(m.name)}</span><span>${attemptsForModule(m.id).length} completed</span></div></div>`).join('')}<p class="small-copy">Completed sessions track practice, not a prediction of NCLEX readiness.</p></section><section class="panel"><h2>Practice history</h2><div style="margin-top:17px">${activities(20)}</div></section></div>`}
function dayCount(i){const d=new Date();d.setDate(d.getDate()-6+i);return attempts.filter(a=>new Date(a.date).toDateString()===d.toDateString()).reduce((n,a)=>n+a.answers.length,0)}
function materialsPage(){return`${heading('Your material is the starting point.','Keep the source close. Make your practice personal.')}<div class="notice">${icon('lock')} Shared modules and their extracted text are saved in Supabase. Uploaded files are stored securely in the shared document bucket. Quiz generation sends only selected modules.</div><div class="source-list">${allModules().map(m=>`<article class="source-item"><div style="display:flex;gap:15px;align-items:center"><div class="module-icon ${m.color||''}">${icon('file')}</div><div><h3>${esc(m.sourceName)}</h3><p>${esc(m.name)} · ${m.demo?'Built-in example':'Shared material'} · ${m.source.split(/\s+/).length} words</p></div></div><div class="source-actions"><button class="button secondary" data-source="${esc(m.id)}">Read source</button><button class="text-button" data-edit="${esc(m.id)}">Edit</button><button class="text-button delete-link" data-delete="${esc(m.id)}">Delete</button></div></article>`).join('')}</div>`}
function bind(){document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>go(b.dataset.page));document.querySelectorAll('[data-create]').forEach(b=>b.onclick=openCreate);document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>openEdit(b.dataset.edit));document.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>openDelete(b.dataset.delete));const restore=document.getElementById('restoreDemo');if(restore)restore.onclick=()=>{hiddenDemo=false;selectedModuleIds.add('skin');save();render()};document.querySelectorAll('[data-start]').forEach(b=>{b.disabled=generationBusy;b.title=generationStatus.enabled===null?'Checking quiz availability…':'';b.onclick=()=>start(b.dataset.start)});document.querySelectorAll('[data-source]').forEach(b=>b.onclick=()=>{const m=allModules().find(m=>m.id===b.dataset.source)||(quiz?.module.id===b.dataset.source?quiz.module:null);if(!m)return;activeSourceId=m.id;viewOriginal=false;document.getElementById('sourceTitle').textContent=m.sourceName;showSource();document.getElementById('sourceDialog').showModal();if(!m.demo&&!m.formattedSource&&!formatStates.has(m.id))void formatModule(m.id)});const mf=document.getElementById('moduleFilter');if(mf)mf.onchange=()=>{filter=mf.value;render()};document.querySelectorAll('[data-new-quiz]').forEach(b=>b.onclick=()=>{quiz=null;practiceError='';go('practice')});document.querySelectorAll('[data-repeat]').forEach(b=>b.onclick=()=>start());document.querySelectorAll('[data-quiz-source]').forEach(b=>b.onchange=()=>{if(b.checked)selectedModuleIds.add(b.dataset.quizSource);else selectedModuleIds.delete(b.dataset.quizSource);render()});const qc=document.getElementById('questionCount');if(qc)qc.onchange=()=>{questionCount=Number(qc.value)};const gs=document.getElementById('generateSelected');if(gs)gs.onclick=()=>start();document.querySelectorAll('input[name=answer]').forEach(el=>el.onchange=()=>{quiz.selected=Number(el.value);document.querySelectorAll('.option').forEach(x=>x.classList.remove('selected'));el.closest('.option').classList.add('selected');quiz.answerError=false;document.querySelector('.answer-hint')?.remove()});const ca=document.getElementById('checkAnswer');if(ca)ca.onclick=()=>{if(quiz.selected===null){quiz.answerError=true;render();return}quiz.checked=true;const q=quiz.questions[quiz.index];quiz.answers.push({term:q.fact.term,correct:quiz.selected===q.correct,selected:quiz.selected});render();document.querySelector('.rationale')?.scrollIntoView({behavior:'smooth',block:'nearest'})};const nq=document.getElementById('nextQuestion');if(nq)nq.onclick=()=>{if(quiz.index===quiz.questions.length-1){quiz.done=true;attempts.push({moduleId:quiz.module.id,moduleIds:quiz.modules.map(m=>m.id),name:quiz.module.name,date:Date.now(),answers:quiz.answers});save()}else{quiz.index++;quiz.selected=null;quiz.checked=false}render();window.scrollTo({top:0,behavior:'instant'})}}
function openCreate(){
 editingModuleId=null;
 document.getElementById('createForm').reset();
 draftOriginalFilename='';draftStoragePath='';
 resetDraft();
 document.getElementById('createTitle').textContent='Create a study module';
 document.getElementById('saveModuleButton').innerHTML='Save module <span>↗</span>';
 document.getElementById('formModeNote').textContent='';
 document.getElementById('importStatus').textContent='';
 document.getElementById('formError').textContent='';
 document.getElementById('createDialog').showModal();
}
function openEdit(id){
 const m=allModules().find(module=>module.id===id);if(!m)return;
 editingModuleId=id;
 document.getElementById('createTitle').textContent='Edit study module';
 document.getElementById('saveModuleButton').innerHTML='Save changes <span>↗</span>';
 document.getElementById('moduleName').value=m.name;
 document.getElementById('sourceText').value=m.source;
 draftOriginalFilename=m.originalFilename||'';draftStoragePath=m.storagePath||'';
 const sections=m.structuredSections||[];
 resetDraft(m.source,sections.length?m.formattedSource||'':'',sections);
 document.getElementById('formModeNote').textContent=m.demo?'Editing the built-in example saves it as your own module. Future practice with the edited material uses AI generation.':'New quizzes will use your updated material. Completed quiz history stays as it was.';
 document.getElementById('importStatus').textContent='';
 document.getElementById('formError').textContent='';
 document.getElementById('createDialog').showModal();
 if(!sections.length&&m.source.length>=80)queueDraftFormat(true);
}
function openDelete(id){
 const m=allModules().find(module=>module.id===id);if(!m)return;
 deletingModuleId=id;
 document.getElementById('deleteDescription').textContent=`Delete “${m.name}” and its saved study material from this browser?`;
 document.getElementById('deleteDialog').showModal();
}
document.getElementById('confirmDelete').onclick=async()=>{
 const id=deletingModuleId,m=allModules().find(module=>module.id===id);if(!m)return;
  if(!m.demo&&modulesFromServer){
   try{await moduleRequest(`/api/modules/${encodeURIComponent(id)}`,{method:'DELETE',body:JSON.stringify({storage_path:m.storagePath||''})});}
   catch(error){toast(error.message||'The shared module could not be deleted.');return;}
  }
  if(m.demo)hiddenDemo=true;
  else custom=custom.filter(module=>module.id!==id);
 selectedModuleIds.delete(id);
 if(!selectedModuleIds.size&&allModules().length)selectedModuleIds.add(allModules()[0].id);
 if(quiz?.modules.some(module=>module.id===id))quiz=null;
 deletingModuleId=null;save();document.getElementById('deleteDialog').close();toast('Module deleted.');render();
};
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());document.querySelectorAll('dialog').forEach(d=>d.addEventListener('click',e=>{if(e.target===d){const r=d.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)d.close()}}));
let importBusy=false;
async function importFile(file){
  if(!file||importBusy)return;
  const err=document.getElementById('formError'),status=document.getElementById('importStatus');
  const submit=document.querySelector('#createForm button[type=submit]'),fileInput=document.getElementById('fileInput');
  importBusy=true;submit.disabled=true;fileInput.disabled=true;err.textContent='';status.textContent='Processing document layout…';
  try{
    if(file.size>32*1024*1024)throw new Error('Please use a file no larger than 32 MB.');
    const bytes=new Uint8Array(await file.arrayBuffer());
    let binary='';for(let i=0;i<bytes.length;i+=0x8000)binary+=String.fromCharCode(...bytes.subarray(i,i+0x8000));
    const response=await fetch('/api/document/extract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:file.name,content_base64:btoa(binary)})});
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||'Document extraction failed.');
    const structured=sectionsAsText(result.sections);
    document.getElementById('sourceText').value=structured;
    draftOriginalFilename=result.original_filename||file.name;
    draftStoragePath=result.storage_path||'';
    draftSource=structured;draftFormatted=structured;draftStructuredSections=result.sections||[];draftError=(result.warnings||[]).join(' ');
    showDraftPreview();
    const mode=result.extraction_mode==='vision'?'Multimodal vision extraction':'Local structural extraction';
    const costNote=result.extraction_mode==='vision'?' · image processing may use model tokens':'';
    document.getElementById('draftFormatStatus').textContent=`${mode} · ${result.raw_metadata?.page_count||result.sections.length} page(s) · ${result.sections.length} section(s) · confidence ${Math.round((result.confidence||0)*100)}%${costNote}`;
    if(!document.getElementById('moduleName').value)document.getElementById('moduleName').value=file.name.replace(/\.[^.]+$/,'').slice(0,70);
    status.textContent=file.name+' · '+(result.raw_metadata?.page_count||result.sections.length)+' page(s) processed with structured extraction. Review the sections below.';
    toast('Material imported. Review your notes below.');
  }catch(error){err.textContent=error.message||'This file could not be read. Please paste your notes instead.';status.textContent='';}
  finally{importBusy=false;submit.disabled=false;fileInput.disabled=false;fileInput.value='';}
}
document.getElementById('sourceToggle').onclick=()=>{viewOriginal=!viewOriginal;showSource()};
document.getElementById('sourceFormat').onclick=()=>{if(activeSourceId)void formatModule(activeSourceId)};
document.getElementById('sourceText').oninput=()=>queueDraftFormat();
document.getElementById('retryDraftFormat').onclick=()=>void formatDraft();
document.getElementById('fileInput').onchange=e=>importFile(e.target.files[0]);const dz=document.getElementById('dropZone');dz.ondragover=e=>{e.preventDefault();dz.style.background='#eaf3fc'};dz.ondragleave=()=>dz.style.background='';dz.ondrop=e=>{e.preventDefault();dz.style.background='';importFile(e.dataTransfer.files[0])};
document.getElementById('createForm').onsubmit=async e=>{
 e.preventDefault();if(importBusy)return;
 const name=document.getElementById('moduleName').value.trim(),source=document.getElementById('sourceText').value.trim(),err=document.getElementById('formError');
 if(!name){err.textContent='Give your module a name.';return}
 if(source.length>100000){err.textContent='Please use up to 100,000 characters per module.';return}
 if(source.length<80){err.textContent='Add at least 80 characters of clinical study material, including findings, conditions, or nursing actions.';return}
 const original=editingModuleId?allModules().find(m=>m.id===editingModuleId):null;
 if(editingModuleId&&!original){err.textContent='This module is no longer available. Please close and try again.';return}
 let id=original?.id||'module-'+Date.now().toString(36);
 if(original?.demo){hiddenDemo=true;id='module-'+Date.now().toString(36)}
 const m={...original,id,name,source,formattedSource:draftSource===source&&draftFormatted?draftFormatted:original?.source===source?original.formattedSource:null,structuredSections:draftSource===source?draftStructuredSections:original?.structuredSections||[],sourceName:name+' · Study notes',originalFilename:draftOriginalFilename||original?.originalFilename||'',storagePath:draftStoragePath||original?.storagePath||'',icon:original?.icon||'book',color:original?.color||'',demo:false};
 if(original?.source!==source)formatStates.delete(id);
 try{
  const saved=modulesFromServer?applyServerModule(await persistModule(m,Boolean(original&&!original.demo))):m;
  if(original&&!original.demo)custom=custom.map(module=>module.id===id?{...module,...saved}:module);
  else custom.push(saved);
 }catch(error){err.textContent=error.message||'The shared module could not be saved.';return;}
 if(original?.demo)selectedModuleIds.delete(original.id);
 selectedModuleIds.add(id);
 if(original&&quiz?.modules.some(module=>module.id===original.id))quiz=null;
 editingModuleId=null;save();document.getElementById('createDialog').close();document.getElementById('createForm').reset();document.getElementById('importStatus').textContent='';resetDraft();toast(original?'Module updated. Future quizzes will use the new source.':'Your study module is saved.');go('modules');activeSourceId=id;viewOriginal=false;document.getElementById('sourceTitle').textContent=m.sourceName;showSource();document.getElementById('sourceDialog').showModal();if(!m.formattedSource)void formatModule(id);
};
document.addEventListener('click',event=>{
 const navBank=event.target.closest('[data-page=\"bank\"]');
 if(navBank){event.preventDefault();page='bank';const first=allModules()[0];if(first)void loadQuestionBank(first.id);return;}
 const bank=event.target.closest('[data-bank-select]');
 if(bank){event.preventDefault();page='bank';void loadQuestionBank(bank.dataset.bankSelect);return;}
 const practiceBankButton=event.target.closest('[data-bank-practice]');
 if(practiceBankButton){event.preventDefault();practiceQuestionBank();return;}
 const generateBankButton=event.target.closest('[data-bank-generate]');
 if(generateBankButton){event.preventDefault();void generateMoreBank();}
});
document.addEventListener('change',event=>{if(event.target.id==='bankModule')void loadQuestionBank(event.target.value)});
render();
const uploadIntro=document.querySelector('#dropZone > span:nth-of-type(2)');
if(uploadIntro)uploadIntro.firstChild.textContent='Drop a PDF, Word, PowerPoint, or image file here, or ';

async function refreshGenerationStatus(){
 const requestId=++statusRequestId;
 try{const res=await fetch('/api/quiz/status',{cache:'no-store'});if(!res.ok)throw Error();const status=await res.json();if(requestId===statusRequestId)generationStatus=status;}
 catch{if(requestId===statusRequestId)generationStatus={enabled:false,message:'Clinical generation is temporarily unavailable. Please try again.'};}
 if(requestId===statusRequestId)render();
 return generationStatus;
}
refreshGenerationStatus();
loadSharedModules();
