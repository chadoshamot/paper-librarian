const $ = s => document.querySelector(s);
let META = {categories:[], areas:[], work_slugs:[]};
let LIB = null, DAILY = [];
let hist = [];
let sessionId = null;
let chatBusy = false, PROFILE = null, profileRawDirty = false;
const FILTER = {cat:null, field:null};
const KIND = {arxiv:'arXiv', doi:'DOI', cloud:'云', zotero:'Zotero', local:'预览'};

function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtMD(s){ return esc(s)
  .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>')
  .replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')
  .replace(/\n/g,'<br>'); }

function toast(msg){
  let t = $('#toast');
  if(!t){ t=document.createElement('div'); t.id='toast'; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._tm); t._tm = setTimeout(()=>t.classList.remove('show'), 2600);
}
async function postFetch(url, body){
  try {
    return await (await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body||{})})).json();
  } catch(e){ return {error:'连接失败，请检查本地服务是否运行'}; }
}
function pollJob(jobId, onDone, onProgress){
  async function poll(){
    let s;
    try { s = await (await fetch('/api/job/'+jobId)).json(); }
    catch(e){ onDone({status:'error', error:'连接中断，可从历史恢复已保存的对话'}); return; }
    if(s.error && !s.status){ onDone({status:'error',error:s.error}); return; }
    if(onProgress) onProgress(s);
    if(s.status!=='running'){ onDone(s); return; }
    setTimeout(poll,1200);
  }
  poll();
}

// ── 导航 / 元数据 ──
async function loadMeta(){
  META = await (await fetch('/api/meta')).json();
  $('#stat').textContent = `已收录 ${META.library_size} 篇论文`;
}
function switchTab(name){
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on', b.dataset.tab===name));
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('on', p.id==='tab-'+name));
  if(name==='library'){ loadMeta(); if(!LIB) loadLibrary(); }
  if(name==='daily') loadDaily();
  if(name==='settings') loadSettings();
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));

// ── 跳转 / 详情 ──
function jumpButtons(targets){
  return (targets||[]).map(t=>{
    if(t.kind==='local') return `<a class="jump" href="${esc(t.url)}" target="_blank" rel="noopener">📄 预览</a>`;
    return `<a class="jump" href="${esc(t.url)}" target="_blank" rel="noopener">${KIND[t.kind]||t.kind}</a>`;
  }).join('');
}
function readTag(d){
  return d.read ? '<span class="tag read">已读</span>' : '<span class="tag unread">未读</span>';
}
function markRead(pid, read){
  postFetch('/api/read', {pid, read}).then(r=>{
    if(r.error){ toast(r.error); return; }
    toast('标记中…');
    pollJob(r.job_id, s=>{
      if(s.status==='error') toast('标记失败：'+s.error);
      else toast(read?'已标记为已读':'已标记为未读');
      loadMeta(); loadLibrary();
      const q = $('#q').value.trim(); if(q) doSearch();
    });
  });
}
function openOverlay(html){ $('#modal').innerHTML = html; $('#overlay').classList.add('on'); }
function closeOverlay(){ $('#overlay').classList.remove('on'); $('#modal').innerHTML=''; }
async function showPaper(pid){
  const r = await (await fetch('/api/paper/'+encodeURIComponent(pid))).json();
  if(r.error){ toast(r.error); return; }
  openOverlay(detailHTML(r.doc, r.targets));
}
function detailHTML(d, targets){
  const cats = META.categories.map(c=>`<option ${c===d.category?'selected':''}>${esc(c)}</option>`).join('');
  const areas = META.areas.map(a=>`<option value="${esc(a)}" ${a.split('::').pop()===d.area?'selected':''}>${esc(a)}</option>`).join('');
  const slugs = META.work_slugs.map(w=>`<option value="${esc(w)}">`).join('');
  return `
    <div class="d-head">
      <div>
        <h3>${esc(d.title_en)}</h3>
        ${d.title_zh && d.title_zh!==d.title_en ? `<div class="d-zh">${esc(d.title_zh)}</div>`:''}
        <div class="d-meta">${readTag(d)}<span class="tag">${esc(d.category)}</span><span class="tag">${esc(d.area)}</span><span class="tag">${esc(d.work)}</span><span class="tag">${esc(d.year)}</span> ${esc(d.venue||'')}<button class="btn small" onclick="markRead('${esc(d.pid)}',${!d.read})">${d.read?'标未读':'标已读'}</button></div>
      </div>
      <button class="btn ghost" onclick="closeOverlay()">✕</button>
    </div>
    <div class="jumps">${jumpButtons(targets)}</div>
    <div class="row"><button class="btn primary" data-paper-pid="${esc(d.pid)}" onclick="quickChat('请深读这篇论文，解释问题、方法和实验，并说明与我课题的关系。论文 pid：'+this.dataset.paperPid)">与馆长讨论这篇论文</button></div>
    <div class="sec"><b>核心内容（中文）</b><p>${esc(d.core_zh)}</p></div>
    <div class="sec"><b>Core idea (English)</b><p>${esc(d.core_en)}</p></div>
    <div class="sec"><b>意义（中文）</b><p>${esc(d.sig_zh)}</p></div>
    <div class="sec"><b>Significance (English)</b><p>${esc(d.sig_en)}</p></div>
    <div class="sec">
      <button class="btn small" onclick="togglePreview('${esc(d.pid)}')">📄 站内预览 PDF</button>
      <div id="preview-wrap" style="display:none;margin-top:8px">
        <iframe id="preview-frame" src="" style="width:100%;height:520px;border:1px solid var(--ink);border-radius:0;background:#fff"></iframe>
      </div>
    </div>
    <div class="sec"><b>纠正分类（重分类）</b>
      <div class="form-grid">
        <select id="rc-cat">${cats}</select>
        <select id="rc-area">${areas}</select>
        <input id="rc-work" list="slug-list" value="${esc(d.work)}" placeholder="work slug">
        <datalist id="slug-list">${slugs}</datalist>
        <input id="rc-year" type="number" value="${esc(d.year)}" placeholder="年份">
        <button class="btn primary" onclick="doReclassify('${esc(d.pid)}')">提交重分类</button>
        <button class="btn danger" onclick="doDelete('${esc(d.pid)}')">删除此论文</button>
      </div>
    </div>`;
}
function togglePreview(pid){
  const w = $('#preview-wrap'), f = $('#preview-frame');
  if(w.style.display==='none'){ f.src = '/pdf/'+encodeURIComponent(pid); w.style.display='block'; }
  else { f.src=''; w.style.display='none'; }
}
function doReclassify(pid){
  const body = {pid, category:$('#rc-cat').value, area:$('#rc-area').value,
                work:$('#rc-work').value.trim(), year:parseInt($('#rc-year').value||'0')};
  if(!body.work || !body.year){ toast('请填 work slug 与年份'); return; }
  postFetch('/api/reclassify', body).then(r=>{
    if(r.error){ toast(r.error); return; }
    toast('重分类中…');
    pollJob(r.job_id, s=>{
      if(s.status==='error') toast('重分类失败：'+s.error);
      else { toast('已重分类并同步 ModelScope'); closeOverlay(); loadMeta(); loadLibrary(); }
    });
  });
}
function doDelete(pid){
  if(!confirm('确认删除这篇论文？将同时删除 Zotero 条目、本地缓存与知识库卡片，并同步到 ModelScope。')) return;
  postFetch('/api/library/delete', {pid}).then(r=>{
    if(r.error){ toast(r.error); return; }
    toast('删除中…');
    pollJob(r.job_id, s=>{
      if(s.status==='error') toast('删除失败：'+s.error);
      else { toast('已删除并同步 ModelScope'); closeOverlay(); loadMeta(); loadLibrary(); }
    });
  });
}

// ── 检索 ──
async function doSearch(){
  const q = $('#q').value.trim(); if(!q) return;
  const p = new URLSearchParams({q, mode:$('#mode').value, engine:$('#engine').value, top:$('#top').value});
  const r = await (await fetch('/api/search?'+p)).json();
  $('#search-note').textContent = r.note || '';
  const box = $('#results'); box.innerHTML='';
  if(!r.results.length){ box.innerHTML='<div class="card empty">无结果（可能无相关论文，或语义相似度低于阈值）</div>'; return; }
  const wrap = document.createElement('div'); wrap.className='card';
  r.results.forEach((d,i)=>{
    const el = document.createElement('div'); el.className='result';
    el.innerHTML = `<div class="idx">${i+1}</div><div class="r-body">
      <div class="t-en" onclick="showPaper('${esc(d.pid)}')">${esc(d.title_en)}</div>
      <div class="t-zh">${esc(d.title_zh)}</div>
      <div class="r-tags">${readTag(d)}<span class="tag">${esc(d.category)}</span><span class="tag">${esc(d.area)}</span><span class="tag">${esc(d.work)}</span><span class="tag">${esc(d.year)}</span><button class="btn small" onclick="markRead('${esc(d.pid)}',${!d.read})">${d.read?'标未读':'标已读'}</button></div>
      <div class="jumps">${jumpButtons(d.targets)}</div>
    </div>`;
    wrap.appendChild(el);
  });
  box.appendChild(wrap);
}

// ── 论文库 ──
async function loadLibrary(){
  const r = await (await fetch('/api/library/tree')).json();
  LIB = r; renderFacets(); renderTree();
}
function renderFacets(){
  const catBox = $('#facet-cat'); catBox.innerHTML='';
  Object.entries(LIB.category_counts||{}).forEach(([c,n])=>{
    catBox.insertAdjacentHTML('beforeend', `<button class="chip ${FILTER.cat===c?'on':''}" onclick="setFilter('cat','${esc(c)}')">${esc(c)} <span>${n}</span></button>`);
  });
  const fBox = $('#facet-field'); fBox.innerHTML='';
  Object.entries(LIB.field_counts||{}).forEach(([f,n])=>{
    fBox.insertAdjacentHTML('beforeend', `<button class="chip ${FILTER.field===f?'on':''}" onclick="setFilter('field','${esc(f)}')">${esc(f)} <span>${n}</span></button>`);
  });
}
function setFilter(kind, val){ FILTER[kind] = FILTER[kind]===val ? null : val; renderFacets(); renderTree(); }
function renderTree(){
  const box = $('#tree'); box.innerHTML='';
  if(!LIB || !LIB.total){ box.innerHTML='<div class="empty">论文库为空，先去「上传论文」或「每日简报」录入几篇</div>'; return; }
  let html='';
  for(const f of LIB.fields){
    if(FILTER.field && f.field!==FILTER.field) continue;
    let fhtml='';
    for(const a of f.areas){
      let ahtml='';
      for(const w of a.works){
        const query = $('#library-query').value.trim().toLowerCase();
        const state = $('#library-read').value;
        const papers = w.papers.filter(p=>(!FILTER.cat || p.category===FILTER.cat)
          && (!state || Boolean(p.read)===(state==='read'))
          && (!query || [p.title_en,p.title_zh,p.area,p.work].join(' ').toLowerCase().includes(query)));
        if(!papers.length) continue;
        ahtml += `<details><summary>${esc(w.work)} <span class="cnt">${papers.length}</span></summary><div class="papers">${papers.map(paperRow).join('')}</div></details>`;
      }
      if(ahtml) fhtml += `<details><summary>${esc(a.area)} <span class="cnt">${a.count}</span></summary>${ahtml}</details>`;
    }
    if(fhtml) html += `<details class="field" open><summary><span class="dot"></span>${esc(f.field)} <span class="cnt">${f.count}</span></summary>${fhtml}</details>`;
  }
  box.innerHTML = html || '<div class="empty">当前筛选下无论文</div>';
}
function paperRow(p){
  return `<div class="paper">
    <div class="p-title" onclick="showPaper('${esc(p.pid)}')">${esc(p.title_en)}</div>
    ${p.title_zh && p.title_zh!==p.title_en ? `<div class="p-zh">${esc(p.title_zh)}</div>`:''}
    <div class="r-tags">${readTag(p)}<span class="tag">${esc(p.category)}</span><span class="tag">${esc(p.year)}</span>${p.venue?`<span class="tag muted">${esc(p.venue)}</span>`:''}<button class="btn small" onclick="markRead('${esc(p.pid)}',${!p.read})">${p.read?'标未读':'标已读'}</button></div>
    <div class="jumps">${jumpButtons(p.targets)}</div>
  </div>`;
}
function librarySync(){
  postFetch('/api/library/sync').then(r=>{
    if(r.error){ toast(r.error); return; }
    $('#sync-status').textContent='同步中（pull + push）…';
    pollJob(r.job_id, s=>{
      $('#sync-status').textContent='';
      if(s.status==='error') toast('同步失败：'+s.error);
      else { toast('已同步 ModelScope'); loadLibrary(); loadMeta(); }
    });
  });
}

// ── 每日简报 ──
async function loadDaily(){
  const r = await (await fetch('/api/daily/latest')).json();
  DAILY = r.candidates || [];
  $('#daily-date').textContent = r.date ? '最近简报：'+r.date : '尚无简报';
  renderDaily();
}
function renderDaily(){
  const box = $('#daily-list'); box.innerHTML='';
  if(!DAILY.length){ box.innerHTML='<div class="empty">暂无推荐结果，可编辑论文需求或调整检索时间范围，再运行一次。</div>'; return; }
  DAILY.forEach((c,i)=>{
    const el = document.createElement('div'); el.className='cand';
    el.innerHTML = `
      <div class="cand-head"><input type="checkbox" class="cand-check" data-i="${i}" checked>
        <div class="cand-title">${esc(c.title)}</div><span class="score">${Number(c.score).toFixed(3)}</span></div>
      <div class="cand-meta"><span class="tag unread">未读</span>${esc(c.year||'?')} · ${esc(c.venue||'未知')} · 被引 ${c.citations||0}</div>
      ${c.llm_reason?`<div class="cand-reason">${esc(c.llm_reason)}</div>`:''}
      ${c.abstract?`<div class="cand-abs">${esc(c.abstract.slice(0,240))}…</div>`:''}
      <div class="cand-actions">${c.url?`<a class="jump" href="${esc(c.url)}" target="_blank" rel="noopener">原文</a>`:''}<button class="btn small primary" onclick="addToLib(${i}, this)">＋ 推入论文库</button></div>`;
    box.appendChild(el);
  });
}
function runDaily(){
  postFetch('/api/daily/run').then(r=>{
    if(r.error){ toast(r.error); return; }
    $('#daily-status').textContent='运行中（抓取+评分约 30–90 秒）…';
    pollJob(r.job_id, s=>{
      $('#daily-status').textContent='';
      if(s.status==='error') toast('运行失败：'+s.error);
      else { loadDaily(); toast('已生成今日简报'); }
    });
  });
}
function addToLib(i, btn){
  const cand = DAILY[i];
  btn.disabled = true; btn.textContent = '下载入库中…';
  postFetch('/api/daily/add', {cand}).then(r=>{
    if(r.error){ toast(r.error); btn.disabled=false; btn.textContent='＋ 推入论文库'; return; }
    pollJob(r.job_id, s=>{
      if(s.status==='error'){ toast('入库失败：'+s.error); btn.disabled=false; btn.textContent='＋ 推入论文库'; }
      else { toast('已推入论文库并同步 ModelScope'); btn.textContent='✅ 已入库'; loadMeta(); loadLibrary(); }
    });
  });
}
function sendEmail(){
  const cands = DAILY.filter((_,i)=>document.querySelector(`.cand-check[data-i="${i}"]`)?.checked);
  if(!cands.length){ toast('请勾选要发送的论文'); return; }
  const to = $('#email-to').value.trim();
  postFetch('/api/daily/email', {to, cands}).then(r=>{
    if(r.error){ toast(r.error); return; }
    toast('发送中…');
    pollJob(r.job_id, s=>{
      if(s.status==='error') toast('发送失败：'+s.error);
      else toast('已发送到邮箱');
    });
  });
}

// ── 上传论文 ──
async function ingest(){
  const inp = $('#file');
  const files = Array.from(inp.files || []);
  if(!files.length){ toast('请先选择 PDF 文件'); return; }
  const log = $('#job-log'), btn = $('#upload-btn');
  log.innerHTML = '';
  const rows = files.map(f => {
    const el = document.createElement('div');
    el.className = 'muted';
    el.textContent = f.name + ' — 排队中…';
    log.appendChild(el);
    return {f, el};
  });
  btn.disabled = true;
  let ok = 0, fail = 0;
  // 逐个顺序入库：避免并发打 DeepSeek/Zotero，也避免 ModelScope git 同步竞争
  for(const {f, el} of rows){
    el.textContent = f.name + ' — 上传中…';
    let r;
    try {
      const dataUrl = await new Promise((res, rej) => {
        const rd = new FileReader();
        rd.onload = () => res(rd.result);
        rd.onerror = () => rej(rd.error);
        rd.readAsDataURL(f);
      });
      r = await postFetch('/api/ingest', {filename:f.name, data:dataUrl.split(',')[1]});
    } catch(e){
      el.textContent = f.name + ' — ❌ 读取/上传失败：' + e;
      fail++; continue;
    }
    if(r.error){ el.textContent = f.name + ' — ❌ ' + r.error; fail++; continue; }
    el.textContent = f.name + ' — 入库中（分类→Zotero→双语卡，约 15–30 秒）…';
    const s = await new Promise(res => pollJob(r.job_id, res));
    if(s.status === 'error'){
      el.textContent = f.name + ' — ❌ ' + s.error;
      fail++;
    } else {
      el.textContent = f.name + ' — ✅ 已入库并同步 ModelScope';
      ok++;
    }
  }
  btn.disabled = false;
  inp.value = '';
  toast(`批量上传完成：成功 ${ok} 篇，失败 ${fail} 篇`);
  loadMeta(); loadLibrary();
}

// ── 对话（右侧常驻栏：馆长 agent，DeepSeek 总控 + Kimi 联网检索）──
function activeHist(){ return hist; }
function linkPid(html){
  return html.replace(/`(local:[A-Za-z0-9_.-]+|\d{4}\.\d{4,5})`/g,
    (m,pid)=>`<a class="jump" href="javascript:void(0)" onclick="showPaper('${pid}')">${pid}</a>`);
}
function appendMsg(role, content, citations){
  const box = $('#chat-box');
  const el = document.createElement('div');
  el.className = 'msg '+role;
  let inner = role==='assistant' ? linkPid(fmtMD(content)) : esc(content);
  if(role==='assistant' && citations && citations.length){
    inner += '<div class="cites">' + citations.map(c=>
      `<span class="cite" title="${esc(c.title_en)}" onclick="showPaper('${esc(c.pid)}')">📄 ${esc(c.pid)}</span>`).join('') + '</div>';
  }
  el.innerHTML = inner;
  box.appendChild(el); box.scrollTop = box.scrollHeight;
}
function addChatMsg(role, content, citations){
  activeHist().push({role, content, citations: citations||undefined});
  appendMsg(role, content, citations);
}
function renderChat(){
  const box = $('#chat-box'); box.innerHTML='';
  activeHist().forEach(m=>{appendMsg(m.role, m.content, m.citations); if(m.tool_events)renderToolEvents(m.tool_events);});
  box.scrollTop = box.scrollHeight;
}
function typing(bool){
  let el = $('#chat-typing');
  if(bool && !el){ el=document.createElement('div'); el.id='chat-typing'; el.className='msg assistant typing'; el.textContent = '馆长处理中…'; $('#chat-box').appendChild(el); $('#chat-box').scrollTop=$('#chat-box').scrollHeight; }
  if(!bool && el) el.remove();
}
function newSessionId(){ return 's' + Date.now().toString(36) + Math.random().toString(36).slice(2,8); }
async function sendChat(){
  if(chatBusy) return;
  const inp = $('#chat-input'); const msg = inp.value.trim(); if(!msg) return;
  chatBusy=true; $('#chat-send').disabled=true;
  inp.value='';
  if(!sessionId) sessionId = newSessionId();
  addChatMsg('user', msg);
  const history = activeHist().slice(0, -1).map(m=>({role:m.role, content:m.content,
    ...(m.citations?{citations:m.citations}:{}),...(m.tool_events?{tool_events:m.tool_events}:{})}));
  const r = await postFetch('/api/librarian', {message:msg, history, session_id: sessionId});
  if(r.error){ addChatMsg('assistant', '❌ '+r.error); chatBusy=false; $('#chat-send').disabled=false; return; }
  typing(true);
  pollJob(r.job_id, s=>{
    typing(false);
    chatBusy=false; $('#chat-send').disabled=false;
    if(s.status==='error') addChatMsg('assistant', '❌ '+s.error);
    else {
      const res = s.result || {};
      addChatMsg('assistant', res.answer || '(空回答)', res.citations);
      activeHist()[activeHist().length-1].tool_events=res.tool_events;
      renderToolEvents(res.tool_events);
      if(res.pending_action) renderPending(res.pending_action);
      if(res.saved === true) toast('已自动保存到私有云端');
      loadMeta(); loadLibrary();
    }
  }, s=>{
    const events=s.events||[]; const latest=events[events.length-1];
    const el=$('#chat-typing'); if(el && latest)el.textContent=latest.label||'馆长处理中…';
  });
}
function renderToolEvents(events){
  const completed=(events||[]).filter(e=>e.tool && e.status!=='running');
  if(!completed.length)return;
  const el=document.createElement('details'); el.className='tool-trace';
  const summary=document.createElement('summary'); summary.textContent='执行记录 · '+completed.length+' 步'; el.appendChild(summary);
  completed.forEach(e=>{const row=document.createElement('div'); row.textContent=e.label||(e.status==='error'?'失败：':'完成：')+e.tool; el.appendChild(row);});
  $('#chat-box').appendChild(el);
}
function quickChat(message){
  if(chatBusy){toast('馆长正在处理当前任务，请稍后继续');return;}
  closeOverlay(); $('#chat-input').value=message; sendChat();
}
function renderPending(pa){
  const box = $('#chat-box');
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.innerHTML = `<div class="pending-box">⚠️ 计划待确认：${esc(pa.summary)}</div>
    <div class="pending-btns">
      <button class="btn primary" onclick="confirmAction('${esc(pa.action_id)}', true, this)">确认执行</button>
      <button class="btn" onclick="confirmAction('${esc(pa.action_id)}', false, this)">取消</button>
    </div>`;
  box.appendChild(el); box.scrollTop = box.scrollHeight;
}
async function confirmAction(actionId, approve, btn){
  const buttons=btn.parentElement.querySelectorAll('button'); buttons.forEach(el=>el.disabled=true);
  const r = await postFetch('/api/librarian/confirm', {action_id: actionId, approve});
  if(r.error){ toast('❌ '+r.error); buttons.forEach(el=>el.disabled=false); return; }
  toast(approve ? '执行中…' : '已取消');
  pollJob(r.job_id, s=>{
    if(s.status==='error'){ toast('❌ '+s.error); buttons.forEach(el=>el.disabled=false); return; }
    const res = s.result || {};
    if(res.cancelled) toast('已取消');
    else if(res.error) toast('❌ '+res.error);
    else if(res.executed){ toast('已执行：'+res.action); addChatMsg('assistant','已执行：'+res.action); loadMeta(); loadLibrary(); }
    else toast('完成');
  });
}
// ── 历史对话 ──
async function openHistory(){
  const r = await (await fetch('/api/chat/history')).json();
  const sessions = r.sessions || [];
  let html = `<div class="d-head"><div><h3>历史对话</h3><div class="d-zh">点击继续；每次对话已自动保存到私有云端</div></div>
    <button class="btn ghost" onclick="closeOverlay()">✕</button></div>`;
  if(!sessions.length){
    html += '<div class="hist-empty">还没有历史对话。<br>发一条消息后会自动保存并上云。</div>';
  } else {
    html += '<div style="max-height:62vh;overflow-y:auto;margin-top:6px">';
    sessions.forEach(s=>{
      const date = s.updated_at ? s.updated_at.slice(0,16).replace('T',' ') : '';
      html += `<div class="hist-item" onclick="loadSession('${esc(s.id)}')">
        <div class="hist-title">${esc(s.title)}</div>
        <div class="hist-meta">${esc(date)} · ${s.turns} 轮 · ${esc(s.model||'')}</div>
        <div class="hist-actions">
          <button class="btn primary" onclick="event.stopPropagation();loadSession('${esc(s.id)}')">继续</button>
          <button class="btn danger" onclick="event.stopPropagation();deleteSession('${esc(s.id)}')">删除</button>
        </div>
      </div>`;
    });
    html += '</div>';
  }
  openOverlay(html);
}
async function loadSession(id){
  if(chatBusy){toast('请等待当前任务完成后切换对话');return;}
  const r = await (await fetch('/api/chat/history/'+encodeURIComponent(id))).json();
  if(r.error){ toast(r.error); return; }
  sessionId = r.id || id;
  hist = (r.messages || []).map(m=>({role:m.role, content:m.content, citations:m.citations,tool_events:m.tool_events}));
  renderChat();
  closeOverlay();
  toast('已载入历史对话，可继续提问');
}
async function deleteSession(id){
  if(!confirm('删除这条历史对话？（也会从私有云端删除）')) return;
  const r = await postFetch('/api/chat/history/delete', {session_id: id});
  if(r.error){ toast(r.error); return; }
  toast('删除中…');
  pollJob(r.job_id, s=>{
    if(s.status==='error') toast('删除失败：'+s.error);
    else { toast('已删除'); openHistory(); }
  });
}
function newChat(){
  if(chatBusy){toast('请等待当前任务完成后开启新对话');return;}
  if(hist.length && !confirm('开启新对话？当前对话已自动保存，可随时从历史继续。')) return;
  hist = [];
  sessionId = null;
  renderChat();
  toast('已开启新对话');
}
// ── 侧栏拖拽调宽 ──
(function(){
  const rail = $('#chat-rail'), rz = $('#rail-resizer');
  if(!rail || !rz) return;
  const saved = localStorage.getItem('railW');
  if(saved) rail.style.width = saved;
  rz.addEventListener('pointerdown', e=>{
    e.preventDefault();
    rz.setPointerCapture(e.pointerId);
    rz.classList.add('drag');
    const startX = e.clientX, startW = rail.getBoundingClientRect().width;
    const onMove = ev=>{
      const w = Math.max(280, Math.min(window.innerWidth*0.7, startW + (startX - ev.clientX)));
      rail.style.width = w + 'px';
    };
    const onUp = ()=>{
      rz.classList.remove('drag');
      rz.removeEventListener('pointermove', onMove);
      rz.removeEventListener('pointerup', onUp);
      rz.removeEventListener('pointercancel', onUp);
      document.body.style.cursor=''; document.body.style.userSelect='';
      localStorage.setItem('railW', rail.style.width);
    };
    document.body.style.cursor='col-resize'; document.body.style.userSelect='none';
    rz.addEventListener('pointermove', onMove);
    rz.addEventListener('pointerup', onUp);
    rz.addEventListener('pointercancel', onUp);
  });
})();

// ── 输入框拖拽调高 ──
(function(){
  const ta = $('#chat-input'), rz = $('#input-resizer');
  if(!ta || !rz) return;
  const saved = localStorage.getItem('inputH');
  if(saved) ta.style.height = saved;
  rz.addEventListener('pointerdown', e=>{
    e.preventDefault();
    rz.setPointerCapture(e.pointerId);
    rz.classList.add('drag');
    const startY = e.clientY, startH = ta.getBoundingClientRect().height;
    const onMove = ev=>{
      const maxH = Math.max(120, window.innerHeight*0.4);
      const h = Math.max(36, Math.min(maxH, startH + (startY - ev.clientY)));
      ta.style.height = h + 'px';
    };
    const onUp = ()=>{
      rz.classList.remove('drag');
      rz.removeEventListener('pointermove', onMove);
      rz.removeEventListener('pointerup', onUp);
      rz.removeEventListener('pointercancel', onUp);
      document.body.style.cursor=''; document.body.style.userSelect='';
      localStorage.setItem('inputH', ta.style.height);
    };
    document.body.style.cursor='row-resize'; document.body.style.userSelect='none';
    rz.addEventListener('pointermove', onMove);
    rz.addEventListener('pointerup', onUp);
    rz.addEventListener('pointercancel', onUp);
  });
})();

// ── 设置 ──
function cfgFieldHTML(f){
  const id = 'cfg-'+f.path.replace(/\./g,'-');
  let input;
  if(f.type==='list'){
    input = `<textarea id="${id}" data-path="${esc(f.path)}" data-type="list">${esc(f.value)}</textarea>`;
  } else {
    const t = f.type==='number' ? 'number' : 'text';
    input = `<input id="${id}" type="${t}" data-path="${esc(f.path)}" data-type="${f.type}" value="${esc(f.value)}">`;
  }
  return `<div class="field"><label for="${id}">${esc(f.label)}</label>${input}</div>`;
}
function envFieldHTML(f){
  const id = 'env-'+f.key;
  const input = `<input id="${id}" type="${f.secret?'password':'text'}" data-key="${esc(f.key)}" placeholder="${f.set?'••••••（已配置，留空保持不变）':'（未配置）'}">`;
  return `<div class="env-row"><label for="${id}">${esc(f.label)}</label>${input}<span class="clear-hint"><input type="checkbox" data-clear="${esc(f.key)}"> 清除</span></div>`;
}
async function loadSettings(){
  const s = await (await fetch('/api/settings')).json();
  $('#cfg-fields').innerHTML = (s.config_fields||[]).map(cfgFieldHTML).join('');
  $('#env-fields').innerHTML = (s.env_fields||[]).map(envFieldHTML).join('');
}
async function saveSettings(){
  const cfg = {};
  document.querySelectorAll('#cfg-fields [data-path]').forEach(el=>{
    const p = el.dataset.path, t = el.dataset.type;
    if(t==='list'){ cfg[p] = el.value.split('\n').map(x=>x.trim()).filter(Boolean); }
    else if(t==='number'){ if(el.value.trim()!=='') cfg[p] = Number(el.value); }
    else { if(el.value!=='') cfg[p] = el.value; }
  });
  const env = {};
  document.querySelectorAll('#env-fields [data-key]').forEach(el=>{
    if(el.value.trim()) env[el.dataset.key] = el.value.trim();
  });
  document.querySelectorAll('#env-fields [data-clear]').forEach(el=>{
    if(el.checked) env[el.dataset.clear] = '';
  });
  const r = await postFetch('/api/settings', {config: cfg, env: env});
  const st = $('#settings-status');
  if(r.error){ st.textContent = '保存失败：'+r.error; st.style.color='#c0392b'; return; }
  st.textContent = '✓ 已保存 '+(r.saved||[]).length+' 项（立即生效）';
  st.style.color='#1a7f37';
  toast('设置已保存');
  loadSettings(); loadMeta();
}

async function loadProfile(){
  try {
    const r=await (await fetch('/api/research-profile')).json();
    if(r.error){$('#profile-status').textContent=r.error;return;}
    PROFILE=r;
    profileRawDirty=false;
    $('#profile-queries').value=r.options.queries.join('\n');
    $('#profile-body').value=r.requirements;
    $('#profile-top').value=r.options.top_k;
    $('#profile-days').value=r.options.recent_days;
    $('#profile-per').value=r.options.per_query;
    $('#profile-relevance').value=r.options.min_relevance;
    document.querySelectorAll('#profile-sources input').forEach(el=>el.checked=r.options.sources.includes(el.value));
    $('#profile-raw').value=r.content;
    $('#profile-status').textContent='已加载 '+r.filename;
  }catch(e){$('#profile-status').textContent='加载失败，请重试';}
}
function profileFormContent(){
  const options={...PROFILE.options,
    queries:$('#profile-queries').value.split('\n').map(s=>s.trim()).filter(Boolean),
    sources:[...document.querySelectorAll('#profile-sources input:checked')].map(el=>el.value),
    top_k:Number($('#profile-top').value),recent_days:Number($('#profile-days').value),
    per_query:Number($('#profile-per').value),min_relevance:Number($('#profile-relevance').value)};
  return '---\n'+JSON.stringify(options,null,2)+'\n---\n\n'+$('#profile-body').value+'\n';
}
function syncProfileRaw(){if(PROFILE && !profileRawDirty)$('#profile-raw').value=profileFormContent();}
async function saveProfile(){
  if(!PROFILE){toast('请先加载需求');return;}
  const content=profileRawDirty || $('#profile-raw-mode').open?$('#profile-raw').value:profileFormContent();
  const r=await postFetch('/api/research-profile',{content,revision:PROFILE.revision});
  if(r.error){$('#profile-status').textContent=r.error;return;}
  PROFILE=r; await loadProfile(); $('#profile-status').textContent='已保存，后续推荐与对话立即使用';
}

$('#profile-raw').addEventListener('input',()=>{profileRawDirty=true; $('#profile-status').textContent='保存将使用直接编辑的 Markdown 内容';});
loadMeta();
