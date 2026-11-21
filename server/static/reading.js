/* Reading state belongs to the human browser, never to a worker's context. */
function resetReading() {
  state.timeline=null;state.readingFolds={};state.readingMode='recent';state.readingSelected=new Set();state.readingSelection=false;state.readingLastSelected=null;
  state.readingLoading=null;state.readingActionAnchor=null;state.readingSerial=(state.readingSerial||0)+1;state.searchSerial=(state.searchSerial||0)+1;state.searchResults=[];state.searchNext=null;
  try {const saved=JSON.parse(localStorage.getItem(`agentchat-reading:${state.me.id}:${state.room}`));if(saved){state.readingMode=['recent','expanded','collapsed'].includes(saved.mode)?saved.mode:'recent';state.readingFolds=saved.folds||{};}}catch{}
  $('reading-mode').value=state.readingMode;$('history-search-panel').hidden=true;$('history-query').value='';$('history-results').replaceChildren();$('history-rail').replaceChildren();$('history-preview').hidden=true;
}
function saveReading() {
  localStorage.setItem(`agentchat-reading:${state.me.id}:${state.room}`,JSON.stringify({mode:state.readingMode,folds:state.readingFolds}));
}
function readingTitle(block) {
  if(block.recalled_at)return `${block.senders[0]} · 已撤回`;
  if(block.message_kind==='recovery_request')return '系统调度 · 异常处理（私有）';
  if(block.message_kind==='session_change_request')return 'session 变更 · 已交给 manager（私有）';
  if(block.message_kind==='session_change_result')return 'session 变更 · 本地已完成（私有）';
  if(block.message_kind==='recovery_followup')return '系统调度 · 继续原任务';
  if(block.type==='human')return `${block.senders[0]} · ${block.private_to?'私有消息':'发起 / 补充'}`;
  if(block.type==='private')return `${block.senders[0]} · 私有回复`;
  return `${block.mode==='synchronous'?'同步':'接力'}第 ${block.number||1} 轮`;
}
function readingOpen(block) {
  if(Object.hasOwn(state.readingFolds,block.key))return state.readingFolds[block.key];
  if(state.readingMode!=='recent')return state.readingMode==='expanded';
  const rounds=state.timeline?.rounds||[],latest=rounds.slice().sort((a,b)=>a.last-b.last).slice(-3);
  const cutoff=latest.length?(latest[0].number===1?latest[0].root:latest[0].first):(state.timeline?.blocks.slice(-3)[0]?.first||0);
  return block.type==='round'?latest.some(r=>r.key===block.round_key):block.first>=cutoff;
}
function readingAnchor() {
  const box=$('messages'),top=box.getBoundingClientRect().top;
  const block=[...box.querySelectorAll('.reading-block')].find(x=>x.getBoundingClientRect().bottom>top);
  const el=block?.dataset.open==='true'?[...block.querySelectorAll('.message')].find(x=>x.getBoundingClientRect().bottom>top)||block:block;
  return {el,block,y:el?.getBoundingClientRect().top,bottom:box.scrollHeight-box.scrollTop-box.clientHeight<80};
}
function summaryHTML(block) {
  if(block.type!=='round')return '';
  const summary=state.timeline.summaries[block.round_key];
  if(summary?.status==='done')return `<div class="round-summary"><span>${esc(summary.manager_name||'manager')} 摘要 · 仅你可见</span><p>${esc((summary.body||'').replace(/^@\S+\s*/,''))}</p></div>`;
  if(summary&&['queued','running'].includes(summary.status))return `<div class="summary-status">${esc(summary.manager_name||'manager')} · ${summary.status==='running'?'正在总结':'摘要排队中'}，不阻塞下一轮</div>`;
  if(block.complete&&block.round_count>5)return `<div class="summary-status"><span>${summary?.status==='failed'?'摘要失败：'+esc(summary.error):summary?.status==='cancelled'?'摘要已停止':summary?.status==='waiting'?'等待已启用的 manager / 新版 bridge':'本轮尚无摘要'}</span><button type="button" class="text-button" data-summary-root="${block.root}" data-summary-round="${block.number}">${summary?.status==='failed'?'重试摘要':'生成摘要'}</button></div>`;
  return '';
}
function renderReading(forceBottom=false) {
  if(!state.timeline)return;
  const box=$('messages'),anchor=readingAnchor(),blocks=state.timeline.blocks;
  if(!blocks.length){box.innerHTML='<div class="room-welcome"><div class="empty-mark">⌁</div><h3>等待你的第一个任务。</h3><p>公开讨论和 manager 私聊共用此消息流；标记为私有的消息只有你和对应 manager 可见。</p></div>';renderReadingRail();return;}
  box.querySelector('.room-welcome')?.remove();
  const keys=new Set(blocks.map(b=>b.key));
  for(const el of [...box.children])if(!keys.has(el.dataset.block))el.remove();
  let previous=null;
  for(const block of blocks) {
    let section=[...box.children].find(el=>el.dataset.block===block.key);
    if(!section){section=document.createElement('section');section.className='reading-block';section.dataset.block=block.key;section.innerHTML='<div class="round-header"><label class="round-select"><input type="checkbox"><span class="sr-only">选择</span></label><button type="button" class="round-toggle"><span class="round-chevron"></span><strong></strong><span class="round-count"></span><span class="round-excerpt"></span></button><span class="round-date"></span><div class="segment-actions"></div></div><div class="round-summary-slot"></div><div class="round-body"></div>';}
    if((previous?previous.nextElementSibling:box.firstElementChild)!==section){if(previous)previous.after(section);else box.prepend(section);}previous=section;
    // Keep the expanded group being read stable when another round arrives.
    if(!anchor.bottom&&anchor.block===section&&section.dataset.open==='true'&&!readingOpen(block)&&state.readingMode==='recent'&&!Object.hasOwn(state.readingFolds,block.key))state.readingFolds[block.key]=true;
    const open=readingOpen(block);section.dataset.open=String(open);section.classList.toggle('private-block',!!block.private_to);section.classList.toggle('human-block',block.type==='human');section.classList.toggle('selected-round',state.readingSelected.has(block.key));
    const selector=section.querySelector('.round-select');selector.hidden=block.type!=='round'||!state.readingSelection;
    const check=selector.querySelector('input');check.dataset.selectBlock=block.key;check.checked=state.readingSelected.has(block.key);check.setAttribute('aria-label',`选择 ${readingTitle(block)}，消息 #${block.first}`);
    const toggle=section.querySelector('.round-toggle');toggle.dataset.toggleBlock=block.key;toggle.setAttribute('aria-expanded',String(open));toggle.setAttribute('aria-label',`${open?'折叠':'展开'} ${readingTitle(block)}，消息 #${block.first}`);
    toggle.querySelector('.round-chevron').textContent=open?'⌄':'›';toggle.querySelector('strong').textContent=readingTitle(block);
    toggle.querySelector('.round-count').textContent=block.type==='round'?`${block.ids.length} 条${block.complete?'':' · 进行中'}`:block.private_to?'私有':`#${block.first}`;
    toggle.querySelector('.round-count').title=block.private_to?'仅你与 manager 可见':block.type==='round'?(block.complete?'本轮已结束':'本轮进行中'):'';
    section.querySelector('.round-date').textContent=formatTime(block.created);
    const actions=section.querySelector('.segment-actions'),actionsHTML=block.type==='human'?`<button type="button" class="text-button" data-segment="${block.segment}" data-expand="true" title="展开这条用户消息到下一条用户消息之间的对话">展开本段</button><button type="button" class="text-button" data-segment="${block.segment}" data-expand="false" title="折叠这条用户消息到下一条用户消息之间的对话">折叠本段</button>`:'';
    if(actions.innerHTML!==actionsHTML)actions.innerHTML=actionsHTML;
    const roundSummary=block.type==='round'?state.timeline.summaries[block.round_key]:null;
    const preview=roundSummary?.status==='done'?(roundSummary.body||'').replace(/^@\S+\s*/,''):block.preview;
    const excerpt=section.querySelector('.round-excerpt');excerpt.hidden=open;excerpt.textContent=preview.replace(/\s+/g,' ').slice(0,160);excerpt.title=roundSummary?.status==='done'?'manager 摘要：'+preview:preview;
    const summary=section.querySelector('.round-summary-slot'),summaryText=summaryHTML(block);summary.hidden=!open;if(summary.dataset.content!==summaryText){summary.innerHTML=summaryText;summary.dataset.content=summaryText;}
    const summaryBubble=summary.querySelector('.round-summary');
    if(summaryBubble){const manager=state.snapshot?.members.find(m=>m.name===state.timeline.summaries[block.round_key]?.manager_name);summaryBubble.dataset.colorSender=manager?.id||'';applyBubbleColor(summaryBubble,memberBubbleColor(manager));}
    const body=section.querySelector('.round-body');body.hidden=!open;
    if(open){
      let prev=null,missing=0;
      for(const id of block.ids){const msg=state.messages.get(id);if(!msg){missing++;continue;}const article=messageNode(msg,body.querySelector(`[data-message="${id}"]`));if((prev?prev.nextElementSibling:body.firstElementChild)!==article){if(prev)prev.after(article);else body.prepend(article);}prev=article;}
      let loading=body.querySelector('.reading-loading');if(missing&&!loading){loading=document.createElement('p');loading.className='reading-loading';body.appendChild(loading);}if(loading){loading.textContent=`正在读取 ${missing} 条历史消息…`;if(!missing)loading.remove();}
    }
  }
  const action=state.readingActionAnchor;
  if(action?.el?.isConnected)box.scrollTop+=action.el.getBoundingClientRect().top-action.y;
  else if(forceBottom||anchor.bottom)box.scrollTop=box.scrollHeight;else if(anchor.el?.isConnected)box.scrollTop+=anchor.el.getBoundingClientRect().top-anchor.y;
  $('reading-selection').textContent=state.readingSelection?'完成':'多选';
  document.querySelector('.reading-toolbar').classList.toggle('selecting',state.readingSelection);
  $('reading-selection').setAttribute('aria-pressed',String(state.readingSelection));$('messages').classList.toggle('selecting-rounds',state.readingSelection);
  $('reading-selection-count').textContent=state.readingSelection?`已选 ${state.readingSelected.size} 段 · 拖拽框选 / Shift 连选`:`${state.timeline.rounds.length} 轮 · 私聊仅你可见`;
  $('expand-selected').disabled=!state.readingSelected.size;$('collapse-selected').disabled=!state.readingSelected.size;
  renderReadingRail();
}
async function loadReadingBodies() {
  if(state.readingLoading)return state.readingLoading;
  const generation=state.generation,room=state.room;
  const loading=(async()=>{
    while(state.generation===generation){
      const ids=(state.timeline?.blocks||[]).filter(readingOpen).flatMap(b=>b.ids).filter(id=>!state.messages.has(id)).slice(0,200);if(!ids.length)break;
      const messages=await post(`/rooms/${room}/reading/messages`,{ids});if(state.generation!==generation)return;
      messages.forEach(m=>state.messages.set(m.id,m));renderReading();
      if(!messages.length)break;
    }
  })();state.readingLoading=loading;
  try{await loading;}finally{if(state.readingLoading===loading)state.readingLoading=null;}
}
async function refreshReading(room,generation) {
  const serial=++state.readingSerial;
  const result=await api(`/rooms/${room}/timeline?revision=${encodeURIComponent(state.timeline?.revision||'')}`);if(state.generation!==generation||serial!==state.readingSerial)return;
  if(!result.unchanged)state.timeline=result;
  const messages=await api(`/rooms/${room}/messages?before=9007199254740991&limit=200`);if(state.generation!==generation||serial!==state.readingSerial)return;
  messages.forEach(m=>state.messages.set(m.id,m));applyReadingRecalls();renderReading();await loadReadingBodies();
}
function applyReadingRecalls() {
  const recalled=new Map((state.timeline?.recalls||[]).map(item=>[item.id,item.recalled_at]));
  for(const message of state.messages.values()) {
    if(recalled.has(message.id)){message.recalled_at=recalled.get(message.id);message.body='消息已撤回';message.quote=null;message.targets=[];message.recall_option=null;}
    if(message.quote&&recalled.has(message.quote.id)){message.quote.body='消息已撤回';message.quote.recalled_at=recalled.get(message.quote.id);}
  }
  if(state.quote&&recalled.has(state.quote.id)){state.quote=null;$('quote-preview').hidden=true;}
}
function renderReadingRail() {
  const rail=$('history-rail'),blocks=state.timeline?.blocks||[];
  const ticks=blocks.length>160?blocks.filter((b,i)=>i===blocks.length-1||i%Math.ceil(blocks.length/159)===0):blocks;
  const html=ticks.map(b=>`<button type="button" data-jump-block="${b.key}" class="history-tick ${b.type==='human'?'human-tick':''} ${b.private_to?'private-tick':''}" aria-label="${esc(readingTitle(b))}，${formatTime(b.created)}，${esc(b.preview.slice(0,45))}"><span></span></button>`).join('');
  if(rail.innerHTML!==html)rail.innerHTML=html;
  updateReadingPosition();
}
function updateReadingPosition() {
  const box=$('messages'),top=box.getBoundingClientRect().top;
  const active=[...box.querySelectorAll('.reading-block')].find(el=>el.getBoundingClientRect().bottom>top+12)?.dataset.block;
  for(const tick of $('history-rail').children)tick.classList.toggle('active',tick.dataset.jumpBlock===active);
}
async function jumpReading(blockKey,messageId=null) {
  const room=state.room,block=state.timeline?.blocks.find(b=>b.key===blockKey);if(!block)return;
  // A search hit must show its text, even if that single message was folded.
  state.readingFolds[block.key]=true;if(messageId)delete state.readingFolds['m'+messageId];saveReading();$('history-preview').hidden=true;renderReading();await loadReadingBodies();if(room!==state.room)return;
  const box=$('messages'),section=box.querySelector(`[data-block="${block.key}"]`),target=messageId?section?.querySelector(`[data-message="${messageId}"]`)||section:section;
  if(target){box.scrollTop+=target.getBoundingClientRect().top-box.getBoundingClientRect().top-12;target.classList.remove('reading-highlight');void target.offsetWidth;target.classList.add('reading-highlight');}
  updateReadingPosition();
}
async function searchHistory(more=false) {
  const query=$('history-query').value.trim(),room=state.room,serial=++state.searchSerial;
  if(!query){$('history-search-status').textContent='输入关键词，搜索这个群聊的完整历史。';return;}
  $('history-search-status').textContent='搜索中…';$('history-search-submit').disabled=true;
  try {
    const result=await api(`/rooms/${room}/search?q=${encodeURIComponent(query)}&scope=${$('history-search-scope').value}&sender=${encodeURIComponent($('history-search-sender').value)}&before=${more?state.searchNext||0:0}`);
    if(room!==state.room||serial!==state.searchSerial)return;
    state.searchResults=more?[...state.searchResults,...result.results]:result.results;state.searchNext=result.next;
    $('history-results').innerHTML=state.searchResults.map(r=>`<button type="button" class="search-result" data-search-message="${r.id}"><span><strong>${esc(r.sender)}</strong><time>${formatTime(r.created)}</time></span><small>#${r.id}${r.private_to?' · 仅你与 manager 可见':''}${r.message_kind==='round_summary'?' · 轮次摘要':''}</small><p>${highlightReading(r.excerpt,query)}</p></button>`).join('');
    $('history-search-status').textContent=state.searchResults.length?`已找到 ${state.searchResults.length} 条${result.more?'，可继续加载':''}`:'没有匹配的消息。';$('history-search-more').hidden=!result.more;
  }finally{if(serial===state.searchSerial)$('history-search-submit').disabled=false;}
}
function highlightReading(text,query) {
  let from=0,out='',at;const lower=text.toLowerCase(),needle=query.toLowerCase();
  while((at=lower.indexOf(needle,from))>=0){out+=esc(text.slice(from,at))+'<mark>'+esc(text.slice(at,at+query.length))+'</mark>';from=at+query.length;}
  return out+esc(text.slice(from));
}
function openHistorySearch() {
  $('history-search-panel').hidden=false;$('history-preview').hidden=true;
  const current=$('history-search-sender').value;$('history-search-sender').innerHTML='<option value="">全部发言人</option>'+(state.snapshot?.members||[]).filter(m=>m.kind!=='bridge').map(m=>`<option value="${esc(m.id)}">${esc(m.name)}</option>`).join('');$('history-search-sender').value=current;
  $('history-query').focus({preventScroll:true});
}
function initReading() {
  $('reading-mode').onchange=safely(async()=>{state.readingMode=$('reading-mode').value;state.readingFolds={};saveReading();renderReading();await loadReadingBodies();});
  $('reading-selection').onclick=()=>{state.readingSelection=!state.readingSelection;if(!state.readingSelection)state.readingSelected.clear();renderReading();};
  for(const [id,open] of [['expand-selected',true],['collapse-selected',false]])$(id).onclick=safely(async()=>{for(const key of state.readingSelected)state.readingFolds[key]=open;saveReading();renderReading();await loadReadingBodies();});
  $('messages').addEventListener('click',safely(async e=>{
    const fold=e.target.closest('[data-fold-message]');
    if(fold){
      const article=fold.closest('.message'),key='m'+fold.dataset.foldMessage;
      state.readingActionAnchor={el:article,y:article.getBoundingClientRect().top};
      if(state.readingFolds[key]===false)delete state.readingFolds[key];else state.readingFolds[key]=false;
      try{saveReading();renderReading();}finally{state.readingActionAnchor=null;}
      return;
    }
    const toggle=e.target.closest('[data-toggle-block]'),segment=e.target.closest('[data-segment]'),summary=e.target.closest('[data-summary-root]'),check=e.target.closest('[data-select-block]');
    if(check){const blocks=state.timeline.blocks.filter(b=>b.type==='round'),key=check.dataset.selectBlock;const from=blocks.findIndex(b=>b.key===state.readingLastSelected),to=blocks.findIndex(b=>b.key===key);const chosen=e.shiftKey&&from>=0?blocks.slice(Math.min(from,to),Math.max(from,to)+1):[blocks[to]];for(const b of chosen){if(check.checked)state.readingSelected.add(b.key);else state.readingSelected.delete(b.key);}state.readingLastSelected=key;renderReading();return;}
    if(summary){summary.disabled=true;await post(`/rooms/${state.room}/summaries`,{root:Number(summary.dataset.summaryRoot),round:Number(summary.dataset.summaryRound)});state.timeline.revision='';await refresh();toast('摘要已交给 manager，仅你可见');return;}
    if(toggle||segment){const el=e.target.closest('.reading-block');state.readingActionAnchor={el,y:el.getBoundingClientRect().top};}
    if(toggle){const block=state.timeline.blocks.find(b=>b.key===toggle.dataset.toggleBlock);state.readingFolds[block.key]=!readingOpen(block);}
    if(segment)for(const block of state.timeline.blocks.filter(b=>b.segment===Number(segment.dataset.segment)))state.readingFolds[block.key]=segment.dataset.expand==='true';
    if(toggle||segment){try{saveReading();renderReading();await loadReadingBodies();}finally{state.readingActionAnchor=null;}}
  }));
  $('history-rail').onclick=safely(async e=>{const tick=e.target.closest('[data-jump-block]');if(tick)await jumpReading(tick.dataset.jumpBlock);});
  function preview(e){const tick=e.target.closest('[data-jump-block]');if(!tick)return;const block=state.timeline?.blocks.find(b=>b.key===tick.dataset.jumpBlock);if(!block)return;const card=$('history-preview');card.innerHTML=`<strong>${esc(readingTitle(block))}</strong><time>${formatTime(block.created)} · #${block.first}</time><p>${esc(block.preview.replace(/\s+/g,' '))}</p><small>${block.private_to?'仅你与 manager 可见':'点击刻度跳转'}</small>`;card.hidden=false;const rect=tick.getBoundingClientRect();card.style.top=Math.min(Math.max(12,rect.top-60),window.innerHeight-card.offsetHeight-12)+'px';card.style.left=Math.max(12,rect.left-card.offsetWidth-12)+'px';}
  $('history-rail').onpointerover=preview;$('history-rail').onfocusin=preview;$('history-rail').onpointerleave=()=>{$('history-preview').hidden=true;};$('history-rail').onfocusout=()=>{$('history-preview').hidden=true;};
  $('messages').addEventListener('scroll',updateReadingPosition,{passive:true});
  $('history-search-open').onclick=openHistorySearch;$('history-search-close').onclick=()=>{$('history-search-panel').hidden=true;};
  $('history-search-form').onsubmit=safely(async e=>{e.preventDefault();await searchHistory();});$('history-search-more').onclick=safely(()=>searchHistory(true));
  $('history-results').onclick=safely(async e=>{const button=e.target.closest('[data-search-message]');if(!button)return;const id=Number(button.dataset.searchMessage),result=state.searchResults.find(r=>r.id===id);let block=state.timeline?.blocks.find(b=>b.ids.includes(id));if(!block&&result?.message_kind==='round_summary')block=state.timeline?.blocks.find(b=>b.round_key===`${result.summary_root}:${result.summary_round}`);if(block){$('history-search-panel').hidden=true;await jumpReading(block.key,id);}else toast('消息索引已更新，请刷新搜索。');});
  document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'&&state.room&&state.view==='chat'){e.preventDefault();openHistorySearch();}if(e.key==='Escape'){$('history-search-panel').hidden=true;$('history-preview').hidden=true;}});
  initReadingMarquee();
}
function initReadingMarquee() {
  const box=$('messages'),marquee=$('reading-marquee');let drag=null,frame=0;
  function update(){if(!drag)return;const x=Math.min(drag.x,drag.lastX),y=Math.min(drag.y,drag.lastY),w=Math.abs(drag.lastX-drag.x),h=Math.abs(drag.lastY-drag.y);if(w+h<6)return;drag.moved=true;marquee.hidden=false;Object.assign(marquee.style,{left:x+'px',top:y+'px',width:w+'px',height:h+'px'});state.readingSelected=new Set(drag.base);for(const section of box.querySelectorAll('.reading-block')){const block=state.timeline.blocks.find(b=>b.key===section.dataset.block),r=section.getBoundingClientRect();if(block.type==='round'&&r.right>=x&&r.left<=x+w&&r.bottom>=y&&r.top<=y+h)state.readingSelected.add(block.key);section.classList.toggle('selected-round',state.readingSelected.has(block.key));const check=section.querySelector('[data-select-block]');if(check)check.checked=state.readingSelected.has(block.key);}const bounds=box.getBoundingClientRect();if(drag.lastY>bounds.bottom-35)box.scrollTop+=12;else if(drag.lastY<bounds.top+35)box.scrollTop-=12;frame=requestAnimationFrame(update);}
  box.addEventListener('pointerdown',e=>{if(!state.readingSelection||e.button!==0||e.target.closest('button,input,select,a'))return;e.preventDefault();drag={id:e.pointerId,x:e.clientX,y:e.clientY,lastX:e.clientX,lastY:e.clientY,base:e.ctrlKey||e.metaKey?new Set(state.readingSelected):new Set(),moved:false};box.setPointerCapture(e.pointerId);});
  box.addEventListener('pointermove',e=>{if(!drag)return;drag.lastX=e.clientX;drag.lastY=e.clientY;cancelAnimationFrame(frame);update();});
  function end(){if(!drag)return;drag=null;cancelAnimationFrame(frame);marquee.hidden=true;renderReading();}
  box.addEventListener('pointerup',end);box.addEventListener('pointercancel',end);
}
