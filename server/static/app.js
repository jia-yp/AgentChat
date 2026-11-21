const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {me:null, room:null, snapshot:null, messages:new Map(), quote:null, initialized:true, polling:false, generation:0, pendingSend:null, view:'chat', modelDrafts:new Map(), templates:[], templateSaving:false,templateSelected:'',targets:new Set(['all']),roundsDirty:false,setupRoom:null,setupRevision:0,setupLoading:false,setupSaving:false,setupLink:null,setupPending:null};
state.channel='';state.channelDrafts=new Map();state.sessionRounds={};
state.sessionSpeaks={};state.syncBusy=false;
state.steering=new Set();
state.recalling=new Set();
state.permissionDrafts=new Map();
state.sessionActions=new Set();
const steerNames={pending:'待注入',injecting:'注入中',injected:'已注入当前任务',rejected:'无法注入',unknown:'注入结果未确认'};
const statusNames = {dormant:'等待首次 @',idle:'待命',running:'正在工作',error:'需要处理',online:'在线',revoked:'已撤销'};
const deliveryNames = {pending:'排队中',round_waiting:'等待下一轮',bundled:'合并到本轮任务',running:'执行中',done:'已完成',failed:'失败',skipped:'未触发',cancelled:'已取消'};
let toastTimer,toastHideTimer,roundSaveTimer,roundSaveQueue=Promise.resolve();
function clearFeedback() {
  clearTimeout(toastTimer);clearTimeout(toastHideTimer);
  const target=$('toast');target.classList.remove('visible');
  if(target.hidePopover&&target.matches(':popover-open'))target.hidePopover();
  target.hidden=true;
}
function toast(text) {
  clearFeedback();
  const target=$('toast');
  if(!target.showPopover)(document.querySelector('dialog[open]')||document.body).appendChild(target);
  target.textContent=text;target.hidden=false;
  if(target.showPopover)target.showPopover();
  requestAnimationFrame(()=>target.classList.add('visible'));
  toastTimer=setTimeout(()=>{target.classList.remove('visible');toastHideTimer=setTimeout(clearFeedback,220);},4000);
}

function choose(title,description,actions,input=false) {
  const dialog=$('choice-dialog'),previous=document.activeElement;
  $('choice-title').textContent=title;$('choice-description').textContent=description;
  $('choice-input-label').hidden=!input;$('choice-input').value='';
  $('choice-actions').replaceChildren(...actions.map(({id,label})=>{const button=document.createElement('button');button.type='button';button.className=id==='cancel'?'secondary':'primary';button.textContent=label;button.onclick=()=>{if(input&&id!=='cancel'&&!$('choice-input').value.trim()){toast('请填写模板名称');return;}dialog.close(id);};return button;}));
  return new Promise(resolve=>{dialog.onclose=()=>{const result={action:dialog.returnValue||'cancel',value:$('choice-input').value.trim()};previous?.focus({preventScroll:true});resolve(result);};dialog.returnValue='cancel';dialog.showModal();if(input)$('choice-input').focus({preventScroll:true});});
}
$('choice-input').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();$('choice-actions').querySelector('button')?.click();}});
async function api(path, options={}) {
  const response = await fetch('/api'+path,{credentials:'same-origin',headers:{'Content-Type':'application/json'},...options});
  const data=await response.json();
  if(!response.ok) throw new Error(data.error || (Array.isArray(data.detail)?data.detail.map(x=>x.msg).join('; '):'请求失败'));
  return data;
}
const post=(path,body={})=>api(path,{method:'POST',body:JSON.stringify(body)});
function safely(fn) { return async event => { clearFeedback();try { await fn(event); } catch(error) {toast(error.message);} }; }
function displayAuth() {
  const mode=!state.initialized?'bootstrap':state.registrationOpen&&location.hash==='#register'?'register':'login',register=mode==='register';
  state.authMode=mode;
  $('auth').hidden=false; $('shell').hidden=true;
  $('setup-code-label').hidden=state.initialized; $('bootstrap-hint').hidden=state.initialized;
  $('auth-title').textContent=register?'创建你的账号':state.initialized?'登录控制室':'创建你的管理员账户';
  $('auth-subtitle').textContent=register?'拥有自己的群聊，接入你的本地 agent。':state.initialized?'继续你的跨 harness 协作。':'设置完成后，即可创建你的第一个群聊。';
  $('password').autocomplete=mode==='login'?'current-password':'new-password';$('password').minLength=mode==='login'?0:12;
  $('auth-submit').textContent=register?'注册并进入 →':state.initialized?'进入控制室 →':'创建管理员账户 →';
  $('registration-notice').hidden=!register;$('username-hint').hidden=mode==='login';
  $('confirm-password-label').hidden=!register;$('confirm-password').disabled=!register;$('confirm-password').required=register;
  $('auth-switch').hidden=!state.initialized||!state.registrationOpen;
  $('auth-switch-label').textContent=register?'已有账号？':'还没有账号？';$('auth-toggle').textContent=register?'返回登录':'注册账号';
}
async function boot() {
  try {const config=await api('/status');state.initialized=config.initialized;state.registrationOpen=config.registration_open;state.publicApiUrl=config.public_api_url||'';state.clientVersion=config.client_version||'';state.me=await api('/me');await loggedIn();}
  catch {displayAuth();}
}
async function loggedIn() {
  $('auth').hidden=true; $('shell').hidden=false; $('user-name').textContent=state.me.name;
  document.querySelector('.user-row .avatar').textContent=state.me.name.slice(0,1).toUpperCase();
  await listRooms();
}
async function listRooms() {
  const rooms=await api('/rooms');
  $('rooms').innerHTML=rooms.map(r=>`<button class="room-button ${state.room===r.id?'active':''}" data-room="${esc(r.id)}"><span>◌</span>${esc(r.name)}</button>`).join('');
  if(!state.room && rooms.length) {const route=readRoute();await selectRoom(rooms.some(r=>r.id===route?.id)?route.id:rooms[0].id,route?.view||'chat');}
}
function readRoute() {const m=location.hash.match(/^#room\/([a-f0-9]+)(\/manage)?$/);return m?{id:m[1],view:m[2]?'manage':'chat'}:null;}
function showView(view,writeRoute=true) {
  closeMentions();
  state.view=view;
  window.AgentChatMobile?.reset();
  $('workspace').hidden=view!=='chat';$('manage-page').hidden=view!=='manage';
  $('roster').classList.remove('open');
  const hash=`#room/${state.room}${view==='manage'?'/manage':''}`;
  if(writeRoute && location.hash!==hash)history.pushState(null,'',hash);
  if(view==='manage'&&state.snapshot)renderManagement();
}
async function selectRoom(id,view='chat',writeRoute=true) {
  if(state.room===id) {showView(view,writeRoute);return;}
  state.room=id; state.generation++; state.messages.clear(); state.quote=null; state.pendingSend=null; state.snapshot=null;
  state.roundsDirty=false;state.templateSelected='';
  state.channel='';state.channelDrafts.clear();state.sessionRounds={};state.sessionSpeaks={};resetReading();
  $('session-search').value='';$('session-filter').value='current';
  clearTimeout(roundSaveTimer);
  try {const saved=JSON.parse(localStorage.getItem(`agentchat-targets:${state.me.id}:${id}`));state.targets=new Set(Array.isArray(saved)&&saved.length?saved:['all']);}catch{state.targets=new Set(['all']);}
  $('messages').replaceChildren();
  $('quote-preview').hidden=true; $('message-input').value=''; $('sidebar').classList.remove('open');
  $('empty').hidden=true; $('manage-agents').replaceChildren();showView(view,writeRoute);
  await refresh(); await listRooms();
}
function memberStatus(m) {
  if(m.deleted_at)return '已删除';
  if(m.revoked) return '已撤销';
  if(m.paused) return m.disabled_reason==='rate_limit'?'已自动关闭 · rate limit':'已关闭';
  if(state.snapshot.room.paused)return '群聊已暂停';
  if(m.kind==='user') return '管理员';
  if(!m.online) return m.enabled?'bridge 离线':'等待首次 @ · bridge 离线';
  return statusNames[m.status] || m.status;
}
function renderMembers() {
  const s=state.snapshot, members=s.members.filter(m=>!m.revoked);
  $('room-title').textContent=s.room.name; $('breadcrumb-room').textContent=s.room.name;
  const agents=members.filter(m=>m.kind==='agent');
  $('room-summary').textContent=`${agents.length} 个 session · ${agents.filter(m=>m.enabled).length} 个已启用${s.room.paused?' · 协作已暂停':''}`;
  $('room-pause').textContent=s.room.paused?'恢复协作':'暂停协作';
  $('room-pause').dataset.short=s.room.paused?'恢复':'暂停';
  $('room-pause').setAttribute('aria-label',$('room-pause').textContent);
  $('member-count').textContent=members.length;
  const box=$('members'),ids=new Set(members.map(m=>m.id));
  for(const card of [...box.children])if(!ids.has(card.dataset.memberId))card.remove();
  let previous=null;
  for(const m of members) {
    let card=[...box.children].find(el=>el.dataset.memberId===m.id);
    if(!card){card=document.createElement('div');card.className='member';card.dataset.memberId=m.id;card.innerHTML=`<button type="button" class="member-toggle" aria-expanded="false" aria-label="${esc(m.name)}：展开详情"></button><div class="member-main"></div>`;}
    if((previous?previous.nextElementSibling:box.firstElementChild)!==card){if(previous)previous.after(card);else box.prepend(card);}previous=card;
    const main=card.querySelector('.member-main'),html=`<div class="member-top">${avatarHTML(m)}<div class="member-info"><strong>${esc(m.name)}</strong><small>${esc(m.harness || (m.kind==='bridge'?'本地 bridge':'YOU'))} · ${esc(memberStatus(m))}</small></div><i class="status-dot ${m.online?'online':''} ${m.status==='running'?'running':''}"></i></div><p class="member-role">${esc(m.role)}</p>${m.kind==='agent'?`<label class="session-switch roster-switch"><input type="checkbox" role="switch" data-session-switch="${esc(m.id)}" aria-label="启用 ${esc(m.name)}" ${m.paused?'':'checked'}><span>${m.paused?'已关闭':'允许触发'}</span></label>`:''}${m.detail && m.kind==='agent'?`<p class="member-detail">${esc(m.detail)}</p>`:''}${m.kind!=='user'?`<div class="member-actions"><button class="text-button" data-mention="${esc(m.name)}">发给 TA</button>${m.kind==='bridge'?`<button class="text-button" data-member="${esc(m.id)}" data-action="${m.paused?'resume':'pause'}">${m.paused?'恢复':'暂停'}</button>`:''}<button class="text-button" data-member="${esc(m.id)}" data-action="${m.kind==='agent'?'delete':'revoke'}">${m.kind==='agent'?'删除':'撤销'}</button></div>`:''}`;
    if(main.innerHTML!==html)main.innerHTML=html;
    renderMemberColors(card,m);
    if(m.kind==='user'&&m.name===state.me.name)paintAvatar(document.querySelector('.user-row .avatar'),m);
  }
  renderTargets(agents);
}

function renderTargets(agents=state.snapshot.members.filter(m=>m.kind==='agent'&&!m.revoked)) {
  const allowed=['all',...state.snapshot.members.filter(m=>!m.revoked).map(m=>m.name)];
  state.targets=new Set([...state.targets].filter(n=>allowed.includes(n)));
  if(!state.targets.size)state.targets.add('all');
  const names=[...new Set(['all',...agents.map(m=>m.name),...state.targets])];
  const box=$('target-chips');
  for(const button of [...box.children])if(!names.includes(button.dataset.mention))button.remove();
  for(const name of names) {
    let button=[...box.children].find(b=>b.dataset.mention===name);
    if(!button){button=document.createElement('button');button.type='button';button.className='chip';button.dataset.mention=name;box.appendChild(button);}
    button.classList.toggle('selected',state.targets.has(name));button.setAttribute('aria-pressed',String(state.targets.has(name)));
    const member=agents.find(m=>m.name===name);button.disabled=!!member?.paused;
    button.textContent=name==='all'?'所有人':name+(member?.session_type==='manager'?' · 私有':'')+(member?.paused?' · 关闭':'');
    button.setAttribute('aria-label','收件人：'+button.textContent);
    button.title=member?.disabled_reason==='rate_limit'?'此 session 遇到 rate limit 已自动关闭，可在成员面板重新开启':'';
  }
}

function selectedManager() {return state.snapshot?.members.find(m=>m.session_type==='manager'&&state.targets.has(m.name)&&!m.revoked);}
function switchChannel(channel) {
  if(channel===state.channel)return;
  closeMentions();
  state.channelDrafts.set(state.channel,{text:$('message-input').value,quote:state.quote});
  state.channel=channel;state.pendingSend=null;
  const draft=state.channelDrafts.get(channel);$('message-input').value=draft?.text||'';window.AgentChatMobile?.fitInput();state.quote=null;$('quote-preview').hidden=true;
}

function selectTarget(name,toggle=true) {
  const manager=state.snapshot.members.find(m=>m.name===name&&m.session_type==='manager');
  if(name==='all')state.targets=new Set(['all']);
  else if(manager)state.targets=new Set([name]);
  else {for(const m of state.snapshot.members.filter(m=>m.session_type==='manager'))state.targets.delete(m.name);state.targets.delete('all');if(toggle&&state.targets.has(name))state.targets.delete(name);else state.targets.add(name);}
  if(!state.targets.size)state.targets.add('all');
  const channel=selectedManager()?.id||'',changed=channel!==state.channel;switchChannel(channel);
  localStorage.setItem(`agentchat-targets:${state.me.id}:${state.room}`,JSON.stringify([...state.targets]));renderTargets();
  if(changed)refresh().catch(error=>toast(error.message));
}
function modelLabel(value) {return value || '由 harness 决定';}
function modelSync(member) {
  const s=member.model_settings;
  if(!s?.supported)return {text:member.harness==='command'?'此命令未启用模型管理':'需要更新本地 bridge',tone:'muted'};
  if(s.applied_revision!==s.revision) return {text:!member.online?'等待 bridge 上线':member.status==='running'?'当前任务完成后同步':'等待本地同步',tone:'pending'};
  return {text:member.online?'已同步':'已同步 · bridge 离线',tone:'synced'};
}
function renderPermissions(card,member) {
  const s=member.permission_settings||{mode:'inherit',revision:0,supported:false,detail:'请更新并重启本地 bridge'},choices=s.options||[];
  let draft=state.permissionDrafts.get(member.id);
  if(!draft||(!draft.dirty&&!draft.saving)){draft={mode:s.mode,revision:s.revision,dirty:false,saving:false};state.permissionDrafts.set(member.id,draft);}
  let form=card.querySelector('[data-permission-form]');
  if(!form){
    form=document.createElement('form');form.dataset.permissionForm=member.id;form.className='permission-form';
    form.innerHTML=`<div class="permission-heading"><label for="permissions-${esc(member.id)}">执行权限</label><span class="permission-sync"></span></div><select id="permissions-${esc(member.id)}" class="permission-input" aria-describedby="permission-help-${esc(member.id)}"></select><p id="permission-help-${esc(member.id)}" class="permission-description"></p><p class="permission-applied"></p><div class="permission-actions"><button type="button" class="secondary permission-reload">载入已保存权限</button><button type="submit" class="primary permission-save">保存权限</button></div>`;
    card.appendChild(form);
  }
  const field=form.querySelector('.permission-input'),html=(choices.length?choices:[{id:s.mode,label:'暂不支持网页调整'}]).map(x=>`<option value="${esc(x.id)}">${esc(x.label)}</option>`).join('');
  if(field.innerHTML!==html)field.innerHTML=html;
  if(field.value!==draft.mode)field.value=draft.mode;
  field.disabled=!s.supported||draft.saving;
  const synced=s.applied_revision===s.revision;
  form.querySelector('.permission-sync').textContent=!s.supported?'未支持':draft.dirty?'未保存':synced?'已同步':!member.online?'等待 bridge 上线':member.status==='running'?'当前任务后同步':'等待本地同步';
  form.querySelector('.permission-description').textContent=choices.find(x=>x.id===draft.mode)?.description||s.detail;
  form.querySelector('.permission-applied').textContent=s.supported?`本地权限：${s.detail||'等待同步'}。${synced?'下次任务按此执行。':'已保存的修改将在空闲时同步。'}`:s.detail;
  form.querySelector('.permission-save').disabled=!s.supported||!draft.dirty||draft.saving;
  form.querySelector('.permission-save').textContent=draft.saving?'保存中…':'保存权限';
  form.querySelector('.permission-reload').disabled=draft.saving;
}
function sessionFooter(m) {
  return `<div class="session-card-footer"><details class="session-details"><summary>会话信息</summary><dl><dt>创建时间</dt><dd>${m.created?esc(new Date(m.created*1000).toLocaleString('zh-CN')):'—'}</dd><dt>最近连接</dt><dd class="session-last-seen">${m.last_seen?esc(new Date(m.last_seen*1000).toLocaleString('zh-CN')):'尚未连接'}</dd><dt>原生 session ID</dt><dd class="session-native">${esc(m.native_id||'等待首次任务创建')}</dd></dl></details><button type="button" class="text-button session-delete" data-session-action="delete">删除 session</button></div>`;
}
function renderManagement() {
  const all=state.snapshot.members.filter(m=>m.kind==='agent'),filter=$('session-filter').value,query=$('session-search').value.trim().toLocaleLowerCase(),container=$('manage-agents');
  const agents=all.filter(m=>(filter==='all'||(filter==='deleted'?m.revoked:!m.revoked&&(filter==='current'||(filter==='online'?m.online:!m.online))))&&(!query||`${m.name} ${m.harness} ${m.role}`.toLocaleLowerCase().includes(query)));
  $('manage-room-name').textContent=state.snapshot.room.name;
  $('manage-count').textContent=`${all.filter(m=>!m.revoked).length} 个当前 session · ${all.filter(m=>m.revoked).length} 个已删除`;
  $('manage-empty').hidden=all.length>0;$('manage-no-results').hidden=!all.length||!!agents.length;
  for(const card of container.children)if(!agents.some(m=>m.id===card.dataset.agent))card.remove();
  for(const m of agents) {
    const s=m.model_settings||{model:'',revision:0,supported:0,applied_model:''};
    let draft=state.modelDrafts.get(m.id);
    if(!draft||(!draft.dirty&&!draft.saving)) {draft={model:s.model,revision:s.revision,dirty:false,saving:false};state.modelDrafts.set(m.id,draft);}
    let card=[...container.children].find(c=>c.dataset.agent===m.id);
    if(card&&card.dataset.archived!==String(!!m.revoked)){card.remove();card=null;}
    if(m.revoked){
      if(!card){card=document.createElement('article');card.className='model-card archived-session';card.dataset.agent=m.id;card.dataset.archived='true';
        card.innerHTML=`<div class="model-card-head"><div class="avatar agent">${esc(m.name.slice(0,2).toUpperCase())}</div><div><h3>${esc(m.name)}</h3><span class="harness-name">${esc(m.harness)}${m.session_type==='manager'?' · 私有 manager':''}</span></div><span class="model-sync">${esc(memberStatus(m))}</span></div><p class="model-role">${esc(m.role)}</p><p class="archived-info"></p>${sessionFooter(m)}`;
        const button=card.querySelector('[data-session-action]');button.dataset.sessionAction='restore';button.className='secondary';button.textContent='恢复 session';container.appendChild(card);}
      paintAvatar(card.querySelector('.model-card-head .avatar'),m);
      card.querySelector('[data-session-action]').disabled=!m.restorable||state.sessionActions.has(m.id);
      card.querySelector('.archived-info').textContent=m.restorable?`删除于 ${formatTime(m.deleted_at)}。恢复后等待你再次 @；此前的任务不会重放。`:'原接入凭证已撤销，保留历史记录；需要重新接入新的 session。';
      continue;
    }
    if(!card) {
      card=document.createElement('article');card.className='model-card';card.dataset.agent=m.id;card.dataset.archived='false';
      card.innerHTML=`<div class="model-card-head"><div class="avatar agent">${esc(m.name.slice(0,2).toUpperCase())}</div><div><h3>${esc(m.name)}</h3><span class="harness-name">${esc(m.harness)}${m.session_type==='manager'?' · 私有 manager':''}</span></div><span class="model-sync"></span></div><div class="session-switch-row"><label class="session-switch"><input type="checkbox" role="switch" data-session-switch="${esc(m.id)}" aria-label="启用 ${esc(m.name)}"><span>允许触发</span></label><small class="switch-reason"></small></div><p class="model-role"></p><div class="model-meta"></div><form data-model-form="${esc(m.id)}"><label for="model-${esc(m.id)}">指定模型</label><div class="model-picker"><select id="model-${esc(m.id)}" class="model-input"></select><button type="button" class="secondary model-refresh">刷新列表</button></div><p class="model-catalog-source muted small"></p><details class="custom-model-details"><summary>自定义模型 ID</summary><input class="custom-model" aria-label="${esc(m.name)} 自定义模型 ID" maxlength="200" autocomplete="off" spellcheck="false"></details><p class="model-applied"></p><p class="model-error" role="status"></p><div class="model-actions"><button type="button" class="text-button model-auto">由 harness 决定</button><button type="button" class="secondary model-reload">载入已保存配置</button><button type="submit" class="primary model-save">保存模型</button></div></form>`;
      container.appendChild(card);
    }
    paintAvatar(card.querySelector('.model-card-head .avatar'),m);
    const sync=modelSync(m),field=card.querySelector('.model-input');
    const choices=[{id:'',label:'由 harness 决定'},...(s.catalog||[])];
    if(draft.model&&!choices.some(x=>x.id===draft.model))choices.push({id:draft.model,label:draft.model+'（已设置）'});
    const choicesHtml=choices.map(x=>`<option value="${esc(x.id)}">${esc(x.label)}${x.id&&x.label!==x.id?' · '+esc(x.id):''}</option>`).join('');
    if(field.innerHTML!==choicesHtml)field.innerHTML=choicesHtml;
    card.querySelector('[data-session-switch]').checked=!m.paused;
    card.querySelector('.switch-reason').textContent=m.disabled_reason==='rate_limit'?'rate limit 后已自动关闭':'';
    card.querySelector('.model-catalog-source').textContent=s.catalog_source||'正在等待本机 harness 提供模型列表';
    card.querySelector('.model-sync').textContent=sync.text;card.querySelector('.model-sync').className='model-sync '+sync.tone;
    card.querySelector('.model-role').textContent=m.role;
    const bridge=state.snapshot.members.find(x=>x.id===m.bridge);
    card.querySelector('.model-meta').textContent=`${memberStatus(m)} · ${bridge?.name||'本地 bridge'}`;
    if(field.value!==draft.model)field.value=draft.model;
    field.disabled=!s.supported||draft.saving;
    card.querySelector('.model-applied').textContent=s.supported?`本地配置：${modelLabel(s.applied_model)}${s.applied_revision!==s.revision?' → '+modelLabel(s.model):''}`:'请更新并重启本地 bridge。自定义命令还需在 command 中使用 {model} 占位符。';
    card.querySelector('.model-error').textContent='';
    card.querySelector('.model-save').disabled=!s.supported||!draft.dirty||draft.saving;
    card.querySelector('.model-save').textContent=draft.saving?'保存中…':'保存模型';
    card.querySelector('.model-auto').disabled=!s.supported||draft.saving;
    card.querySelector('.model-reload').disabled=draft.saving;
    renderPermissions(card,m);
    if(!card.querySelector('.session-card-footer'))card.insertAdjacentHTML('beforeend',sessionFooter(m));
    card.querySelector('.session-last-seen').textContent=m.last_seen?new Date(m.last_seen*1000).toLocaleString('zh-CN'):'尚未连接';
    card.querySelector('.session-native').textContent=m.native_id||'等待首次任务创建';
    card.querySelector('[data-session-action]').disabled=state.sessionActions.has(m.id);
  }
}
$('session-search').addEventListener('input',renderManagement);
$('session-filter').addEventListener('change',renderManagement);
async function manageSession(id,action) {
  if(state.sessionActions.has(id))return;
  const room=state.room,member=state.snapshot.members.find(m=>m.id===id);
  if(!member)return;
  const deleting=action==='delete',label=deleting?'删除':'恢复';
  if(deleting&&member.session_type!=='manager'&&await openSessionRemoval(member))return;
  const choice=await choose(`${label} ${member.name}？`,deleting?'停止此 session 的运行和排队任务，并移入「已删除」列表。历史消息和本地文件保留，以后可以恢复。':'保留原生会话和模型设置，恢复后等待你再次 @。此前取消的任务不会重新执行。',[{id:action,label:label+' session'},{id:'cancel',label:'取消'}]);
  if(choice.action!==action)return;
  state.sessionActions.add(id);if(state.room===room&&state.view==='manage')renderManagement();
  try {
    const result=await post(`/rooms/${room}/members/${id}/control`,{action});
    state.modelDrafts.delete(id);state.permissionDrafts.delete(id);
    toast(deleting?'已移入「已删除」列表，停止接收新任务':result.requires_bridge_restart?'已恢复。此 session 使用旧版 bridge，请更新并重启本地 bridge 后继续。':'已恢复，等待你再次 @');
  } finally {state.sessionActions.delete(id);if(state.room===room)await refresh();}
}
function formatTime(ts) {return new Date(ts*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
function mentionText(text) {return esc(text).replace(/@([a-zA-Z\u3400-\u4dbf\u4e00-\u9fff][a-zA-Z0-9_\-\u3400-\u4dbf\u4e00-\u9fff]{0,39})/g,'<span class="mention">@$1</span>');}
const markdownRenderer=new marked.Renderer();
markdownRenderer.html=token=>esc(token.text);
markdownRenderer.image=token=>esc(token.text||'图片');
function markdown(text) {
  const container=document.createElement('div');
  container.innerHTML=DOMPurify.sanitize(marked.parse(text,{renderer:markdownRenderer,async:false,gfm:true,breaks:true}),{
    ALLOWED_TAGS:['p','br','strong','em','del','code','pre','a','ul','ol','li','blockquote','h1','h2','h3','h4','h5','h6','hr','table','thead','tbody','tr','th','td'],
    ALLOWED_ATTR:['href','title','class','start','align'],ALLOW_DATA_ATTR:false,ALLOW_ARIA_ATTR:false
  });
  for(const link of container.querySelectorAll('a')) {
    if(!/^(https?:|mailto:)/i.test(link.getAttribute('href')||''))link.removeAttribute('href');
    else {link.target='_blank';link.rel='noopener noreferrer';}
  }
  const walker=document.createTreeWalker(container,NodeFilter.SHOW_TEXT),nodes=[];
  while(walker.nextNode())if(!walker.currentNode.parentElement.closest('code,pre,a'))nodes.push(walker.currentNode);
  for(const node of nodes) {
    const matches=[...node.textContent.matchAll(/@[a-zA-Z\u3400-\u4dbf\u4e00-\u9fff][a-zA-Z0-9_\-\u3400-\u4dbf\u4e00-\u9fff]{0,39}/g)];
    if(!matches.length)continue;
    const fragment=document.createDocumentFragment();let offset=0;
    for(const match of matches){fragment.append(document.createTextNode(node.textContent.slice(offset,match.index)));const span=document.createElement('span');span.className='mention';span.textContent=match[0];fragment.append(span);offset=match.index+match[0].length;}
    fragment.append(document.createTextNode(node.textContent.slice(offset)));node.replaceWith(fragment);
  }
  return container.innerHTML;
}
function messageRuntime(m) {
  const run=m.runtime||{};
  return m.sender_kind==='agent'?`${run.harness||'未记录'} - ${run.model||'未报告'} - ${run.think_level||'未报告'}`:m.sender_kind==='bridge'?'bridge - — - —':'人类 - — - —';
}
function deliveryControl(m,d) {
  if(d.status==='steered')return '';
  let action=d.status==='failed'?(d.retryable===false?'<span class="muted small" title="失败记录已保留；可重新向该 session 发送后续指令">本轮已结束</span>':`<button class="text-button" data-retry="${esc(d.id)}">重试</button>`):'';
  if(d.steer_option) {
    const option=d.steer_option,busy=state.steering.has(d.id);
    const title=option.reason||`将这条排队消息补充给 @${d.name} 的当前任务 #${option.task_message}，不再另行执行`;
    action+=`<button class="text-button steer-queued" data-steer="${esc(d.id)}" data-task-ref="${esc(option.task_ref||'')}" aria-label="将消息 #${m.id} 给 ${esc(d.name)} 转为 Steer" title="${esc(title)}" ${!option.task_ref||busy?'disabled':''}>${busy?'提交中…':'Steer'}</button>`;
  }
  const label=d.status==='round_waiting'&&activeSync()?.recovering?.length?'等待 manager 处理':deliveryNames[d.status]||d.status;
  return `<span class="delivery-group"><span class="delivery ${d.status==='failed'?'error':''}" title="${esc(d.error)}">${esc(d.name)} · ${esc(label)}${d.error?'（'+esc(d.error)+'）':''}</span>${action}</span>`;
}
function activeSync() {const run=state.snapshot?.synchronous;return run&&['running','blocked'].includes(run.status)?run:null;}
function renderSynchronous() {
  const run=activeSync();
  const panel=$('sync-panel');panel.hidden=!run;
  if(panel.hidden)return;
  const labels={running:'进行中',blocked:'等待处理',completed:'已结束',stopped:'已停止'};
  $('sync-title').textContent=`第 ${run.round}/${run.maximum||'∞'} 轮 · ${labels[run.status]}`;
  const waiting=(run?.participants||[]).filter(p=>['pending','running'].includes(p.status)).map(p=>p.name);
  const text=run.recovering?.length?run.recovering.map(r=>`${r.manager} 正在处理 @${r.member} 的异常`).join('；'):run.reason||(waiting.length?'等待：'+waiting.join('、'):'本轮已完成');
  $('sync-summary').textContent=text;$('sync-summary').title=text;
  $('sync-continue').hidden=run.status!=='blocked'&&!run.recovering?.length;$('sync-continue').disabled=state.syncBusy;
  $('sync-continue').textContent='直接进入下一轮';
  const names={pending:'待开始',running:'工作中',done:'已完成',failed:'失败',skipped:'已跳过'};
  const html=(run?.participants||[]).map(p=>`<div class="sync-person ${esc(p.status)}" title="${esc(p.error||'')}"><strong>${esc(p.name)}</strong><span>${p.used}/${p.quota} 次 · ${names[p.status]}</span>${p.status==='failed'?`<button type="button" class="text-button" data-sync-retry="${esc(p.delivery)}" ${state.syncBusy?'disabled':''}>重试</button>`:''}</div>`).join('');
  if($('sync-participants').innerHTML!==html)$('sync-participants').innerHTML=html;
}
function messageNode(m,article=null) {
  if(!article){
    article=document.createElement('article');article.className='message'+(m.sender_name===state.me.name?' self':'');article.dataset.message=m.id;
    article.innerHTML=`<div class="avatar"></div><div class="message-content"><div class="message-meta"><div class="sender-block"><strong>${esc(m.sender_name)}</strong><small class="runtime-meta"></small></div><span class="kind">${m.private_to?'私有 · 仅你与 manager':m.sender_kind==='user'?'管理员':m.sender_kind==='bridge'?'副管理员':'AGENT'}</span><time>${formatTime(m.created)}</time><span class="message-number">#${m.id}</span><button type="button" class="message-excerpt" data-fold-message="${m.id}" title="展开这条消息" hidden></button><span class="message-actions"></span></div><div class="message-body"></div><div class="message-controls"></div></div>`;
  }
  article.classList.toggle('private-message',!!m.private_to);
  paintAvatar(article.querySelector(':scope>.avatar'),state.snapshot?.members.find(member=>member.id===m.sender),{kind:m.sender_kind,harness:m.runtime?.harness,name:m.sender_name});
  const body=article.querySelector('.message-body'),bodyHTML=m.recalled_at?'<span class="recalled-notice">消息已撤回 · 排队任务已取消</span>':`${m.quote?`<blockquote class="quote"><div class="quote-author">${esc(m.quote.name)} · ${formatTime(m.quote.created)}</div>${markdown(m.quote.body.slice(0,700))}</blockquote>`:''}${markdown(m.body)}`;
  if(body.dataset.content!==bodyHTML){body.innerHTML=bodyHTML;body.dataset.content=bodyHTML;}
  article.classList.toggle('recalled-message',!!m.recalled_at);
  article.dataset.sender=m.sender;applyBubbleColor(article,m.recalled_at?'':memberBubbleColor(state.snapshot?.members.find(member=>member.id===m.sender)));
  article.querySelector('.runtime-meta').textContent=messageRuntime(m);
  if(['recovery_request','recovery_followup','session_change_request','session_change_result'].includes(m.message_kind)) {
    article.querySelector('.sender-block strong').textContent='系统调度';
    article.querySelector('.runtime-meta').textContent=m.message_kind.startsWith('session_change_')?'session 变更':'异常恢复';
    article.querySelector('.kind').textContent=m.private_to?'私有 · 仅你与 manager':'继续原任务';
  }
  article.querySelector('.message-number').textContent=`#${m.id}`+(m.round_mode==='synchronous'&&m.sync_round?` · 第 ${m.sync_round} 轮`:'');
  const controls=article.querySelector('.message-controls');
  const recall=m.recall_option?`<button type="button" class="text-button recall-message" data-recall="${m.id}" aria-label="撤回消息 #${m.id}" title="${esc(m.recall_option.reason)}" ${!m.recall_option.available||state.recalling.has(m.id)?'disabled':''}>${state.recalling.has(m.id)?'撤回中…':'撤回'}</button>`:'';
  // Single-message folding is a per-browser reading choice, stored with the block folds.
  const folded=!m.recalled_at&&state.readingFolds?.['m'+m.id]===false;
  article.classList.toggle('folded',folded);
  const excerpt=article.querySelector('.message-excerpt');excerpt.hidden=!folded;
  if(folded){const text=m.body.replace(/[#>*_`~|]|\[|\]\([^)]*\)/g,'').replace(/\s+/g,' ').trim().slice(0,200);if(excerpt.textContent!==text)excerpt.textContent=text;}
  const fold=m.recalled_at?'':`<button type="button" class="text-button message-fold" data-fold-message="${m.id}" aria-expanded="${!folded}" aria-label="${folded?'展开':'折叠'}消息 #${m.id}">${folded?'展开':'折叠'}</button>`;
  const actions=article.querySelector('.message-actions'),actionsHTML=m.recalled_at?'':`<button class="text-button" data-quote="${m.id}">引用</button>`+recall+fold;
  if(actions.innerHTML!==actionsHTML)actions.innerHTML=actionsHTML;
  const html=m.recalled_at?'':m.deliveries.map(d=>deliveryControl(m,d)).join('')+(m.steers||[]).map(d=>`<span class="delivery steer-receipt ${['rejected','unknown'].includes(d.status)?'error':''}" title="${esc(d.error||'仅确认 harness 接受输入，不代表模型已读或理解')}">Steer → ${esc(d.name)} · #${d.task_message} · ${steerNames[d.status]||esc(d.status)}${d.error?'（'+esc(d.error)+'）':''}</span>`).join('');
  if(controls.innerHTML!==html)controls.innerHTML=html;
  return article;
}
function renderMessages(forceBottom=false) {renderReading(forceBottom);}
async function refresh() {
  if(!state.room || !state.me) return;
  const room=state.room;let generation=state.generation;
  const snap=await api(`/rooms/${room}`);
  if(generation!==state.generation) return;
  const changed=JSON.stringify(snap)!==JSON.stringify(state.snapshot);
  state.snapshot=snap;
  if(!state.roundsDirty&&!$('rounds-dialog').open&&document.activeElement!==$('max-rounds')){$('max-rounds').value=snap.room.max_rounds??8;$('round-mode').value=snap.room.round_mode||'global';state.sessionRounds={...snap.room.session_rounds};state.sessionSpeaks={...snap.room.session_speaks};}
  if(activeSync()){$('max-rounds').value=activeSync().maximum;$('round-mode').value='synchronous';state.sessionSpeaks={...activeSync().allowances};}
  renderRoundMode();
  if(state.setupRoom===room)renderSetupStatus(snap.setup);
  if(changed) {renderMembers();if(state.view==='manage')renderManagement();}
  const manager=selectedManager();
  if((manager?.id||'')!==state.channel){switchChannel(manager?.id||'');generation=state.generation;}
  $('conversation-scope').textContent=manager?`统一消息流 · 正在私信 @${manager.name}`:'统一消息流';
  renderRoundMode();renderSynchronous();
  $('routing-hint').textContent=manager?'仅你与 manager 可见 · 正文 @ 仅提及':activeSync()?'补充在下一轮处理 · 正文 @ 仅提及':'Enter 发送 · Shift+Enter 换行 · 正文 @ 仅提及';
  await refreshReading(room,generation);
  if(generation!==state.generation)return;
  setConnection(true);
}
function setConnection(ok) {
  for(const box of [$('connection'),$('room-connection')]){box.classList.toggle('lost',!ok);box.innerHTML=`<i></i><span>${ok?'已连接':'连接中断 · 正在重试'}</span>`;box.title=ok?'已连接':'连接中断 · 正在重试';}
}
function insertMention(name) {
  selectTarget(name,false);$('message-input').focus({preventScroll:true});
}
function clearSetupLink() {
  state.setupLink=null;state.setupPending=null;
  $('setup-preview').open=false;$('setup-text').value='';
}
function renderProjectChoice() {
  const inherited=$('same-as-manager').checked;
  $('manual-project').hidden=inherited;
  $('workspace-path').disabled=inherited;$('project-name').disabled=inherited;
  $('manager-project-hint').textContent=inherited?'接入时自动沿用 manager 的工作目录和所属 project 名称。':'为这次接入指定工作目录和 project 名称。';
}
function renderSetupStatus(setup) {
  if(state.sessionChangeMode)return;
  // A poll started before link creation must not discard the new clipboard value.
  const previous=state.setupSummary;
  if(previous&&(!setup||setup.created<previous.created||(setup.id===previous.id&&previous.status==='complete'&&setup.status!=='complete')))return;
  state.setupSummary=setup;
  const labels={pending:'链接已生成，等待本地 setup agent 接入',running:'本地 setup 正在进行',complete:'接入已完成，本次链接已失效，接入 token 已刷新',superseded:'已有新链接，请使用最新复制的链接',expired:'链接已过期，可重新复制生成'};
  $('setup-status').textContent=setup?(labels[setup.status]||setup.status)+(setup.detail?'：'+setup.detail:''):'尚未生成接入链接';
  const project=setup?.resolved_workspace?`项目：${setup.resolved_project_name} · ${setup.resolved_workspace}`:'';
  $('setup-project').textContent=project;$('setup-project').title=project;
  if(state.setupLink&&setup&&(setup.id!==state.setupLink.id||!['pending','running'].includes(setup.status)))clearSetupLink();
}
async function loadSetupProfile() {
  if(state.sessionChangeMode)return loadSessionChanges(true);
  const room=state.room,sequence=state.setupLoad=(state.setupLoad||0)+1;state.setupLoading=true;$('copy-setup').disabled=true;$('save-setup').disabled=true;
  try {
    const profile=await api(`/rooms/${room}/setup-profile`);
    if(state.room!==room||state.setupLoad!==sequence)return;
    state.setupRoom=room;state.setupRevision=profile.revision;clearSetupLink();
    $('workspace-path').value=profile.workspace;
    $('project-name').value=profile.project_name||'';
    $('same-as-manager').checked=profile.inherit_manager??!profile.revision;renderProjectChoice();
    if(profile.roles.length)loadRoles(profile.roles);
    else {$('roles').replaceChildren();$('no-roles').hidden=false;}
    renderSetupStatus(profile.setup);
    $('setup-saved').textContent=profile.revision?'已从服务器载入群聊接入配置':'填写协作角色后保存，或直接复制接入链接';
  } finally {if(state.setupLoad===sequence){state.setupLoading=false;$('copy-setup').disabled=false;$('save-setup').disabled=false;}}
}
async function showSetup(forceLink=false) {
  clearFeedback();
  try {if(forceLink!==true&&await openSessionChanges())return;}
  catch(error){toast(error.message);return;}
  setSessionChangeMode(false);
  if(state.setupRoom!==state.room){state.setupRevision=0;state.setupSummary=null;clearSetupLink();$('roles').replaceChildren();$('no-roles').hidden=false;$('workspace-path').value='';$('project-name').value='';$('same-as-manager').checked=true;renderProjectChoice();$('setup-project').textContent='';$('setup-project').title='';}
  $('api-url').value=state.publicApiUrl||localStorage.getItem('agentchat-api-url')||(location.protocol==='https:'?location.origin:`${location.protocol}//${location.hostname.includes(':')?'['+location.hostname+']':location.hostname}:8781`);
  $('setup-dialog').showModal();
  try {await Promise.all([state.setupRoom!==state.room?loadSetupProfile():Promise.resolve(),loadTemplates()]);}
  catch(error){toast(error.message);}
}
function validateRoles(roles,allowEmpty=false) {
  if(!Array.isArray(roles)||roles.length>101)throw new Error('角色清单必须是 JSON 数组，最多 100 个协作 session');
  const normalized=roles.map(x=>{
    if(!x||typeof x!=='object'||Array.isArray(x))throw new Error('每个角色必须是 JSON 对象');
    if(typeof x.name!=='string'||typeof x.role!=='string'||typeof x.harness!=='string')throw new Error('每个角色都需要 name、harness 和 role 文本字段');
    return {...x,name:x.name.trim().toLowerCase(),role:x.role.trim()};
    }).filter(x=>x.session_type!=='manager').map(x=>{if(x.session_type&&x.session_type!=='worker')throw new Error('无效的 session 类型');const {session_type,...worker}=x;return worker;});
  if((!allowEmpty&&!normalized.length)||normalized.length>100)throw new Error('请配置 1–100 个协作 session；manager 由 setup agent 自动接入');
  if(normalized.some(x=>!x.name||!x.role||!x.harness))throw new Error('请完整填写至少一个 session 的名称、harness 和分工');
  if(normalized.some(x=>x.role.length>4000))throw new Error('每个 session 的分工最多 4000 字');
  if(normalized.some(x=>!['codex','claude','antigravity','deepseek','command'].includes(x.harness)))throw new Error('harness 需为 codex、claude、antigravity、deepseek 或 command');
  if(normalized.some(x=>!/^[a-z\u3400-\u4dbf\u4e00-\u9fff][a-z0-9_\-\u3400-\u4dbf\u4e00-\u9fff]{0,39}$/.test(x.name)))throw new Error('Session 名称需以中文或英文字母开头，最多 40 个字符，可含数字、_、-');
  if(new Set(normalized.map(x=>x.name)).size!==normalized.length)throw new Error('Session 名称不能重复（英文字母不区分大小写）');
  if(normalized.some(x=>['all','everyone','所有人','全体','管理员','admin','system'].includes(x.name)))throw new Error('Session 名称不能使用 all、everyone、所有人、全体、管理员、admin、system 等保留名称');
  return normalized;
}
function readRoles(allowEmpty=false) {
  const roles=[...document.querySelectorAll('.role-row')].map(row=>{let extra={};try{extra=JSON.parse(row.querySelector('.role-extra').value||'{}');}catch{throw new Error('适配参数必须是合法 JSON 对象');}if(!extra||typeof extra!=='object'||Array.isArray(extra))throw new Error('适配参数必须是 JSON 对象');if(extra.session_type&&extra.session_type!=='worker')throw new Error('manager 由 setup agent 自动接入，不需要在角色参数中重复配置');delete extra.session_type;return {...extra,name:row.querySelector('.role-name').value.trim().toLowerCase(),harness:row.querySelector('.role-harness').value,role:row.querySelector('.role-task').value.trim()};});
  return validateRoles(roles,allowEmpty);
}
function loadRoles(roles) {
  const valid=validateRoles(roles,true);
  $('roles').replaceChildren(...valid.map(makeRoleRow));$('roles').scrollTop=0;$('no-roles').hidden=!!valid.length;
  clearSetupLink();
}
async function loadTemplates(selected=state.templateSelected) {
  const templates=await api('/role-templates');state.templates=templates;
  $('role-template').innerHTML='<option value="">选择角色模板</option><optgroup label="服务器模板">'+templates.map(t=>`<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('')+'</optgroup><optgroup label="JSON 文件"><option value="__import__">导入 JSON 文件…</option><option value="__export__">导出当前 JSON</option></optgroup>';
  state.templateSelected=templates.some(t=>t.id===selected)?selected:'';$('role-template').value=state.templateSelected;templateButtons();
}
function templateButtons() {
  const selected=state.templates.some(t=>t.id===state.templateSelected);
  $('overwrite-template').disabled=!selected||state.templateSaving;
  $('delete-template').disabled=!selected||state.templateSaving;
  $('new-template').disabled=state.templateSaving;$('role-template').disabled=state.templateSaving;
}
function hasRoleContent() {return [...document.querySelectorAll('.role-row input,.role-row select,.role-extra')].some(field=>field.value.trim());}
async function importRoleSet(roles,label) {
  const valid=validateRoles(roles,true),legacyManager=roles.some(x=>x.session_type==='manager');
  if(!valid.length){toast('模板中只有旧 manager 配置；manager 由 setup agent 接入，请添加协作角色');return false;}
  const choice=hasRoleContent()?await choose('载入角色',`「${label}」包含 ${valid.length} 个角色。当前已有内容，要如何载入？`,[{id:'replace',label:'覆盖'},{id:'append',label:'追加（append）'},{id:'cancel',label:'取消'}]):{action:'replace'};
  if(choice.action==='cancel')return false;
  if(choice.action==='append'){$('roles').append(...valid.map(makeRoleRow));$('no-roles').hidden=true;clearSetupLink();}
  else loadRoles(valid);
  toast(`已${choice.action==='append'?'追加':'载入'} ${valid.length} 个协作角色`+(legacyManager?'；manager 由 setup agent 接入，无需重复配置':''));return true;
}
async function copy(text) {
  const previous=document.activeElement,area=document.createElement('textarea');
  area.className='clipboard-target';area.value=text;area.readOnly=true;
  (document.querySelector('dialog[open]')||document.body).appendChild(area);
  let ok=false;
  try {area.focus({preventScroll:true});area.select();ok=document.execCommand('copy');} catch { /* Use the async API only if native selection copy failed. */ }
  finally {area.remove();previous?.focus({preventScroll:true});}
  if(!ok&&navigator.clipboard&&window.isSecureContext) {try {await navigator.clipboard.writeText(text);ok=true;} catch {}}
  if(!ok)throw new Error('浏览器未允许复制；可展开下方接入链接，选中后按 Ctrl+C（Mac 为 ⌘C）');
  toast('已复制');
}
$('auth-form').addEventListener('submit',async e=>{
  e.preventDefault();$('auth-submit').disabled=true;$('auth-error').textContent='';
  try {
    if(state.authMode==='register'&&$('password').value!==$('confirm-password').value)throw new Error('两次输入的密码不一致');
    state.me=await post(state.authMode==='register'?'/register':state.initialized?'/login':'/bootstrap',{username:$('username').value,password:$('password').value,setup_code:$('setup-code').value});
    $('password').value='';$('confirm-password').value='';$('setup-code').value='';state.initialized=true;
    if(['#register','#login'].includes(location.hash))history.replaceState(null,'',location.pathname+location.search);
    await loggedIn();
  }
  catch(error){toast(error.message);} finally{$('auth-submit').disabled=false;}
});
$('auth-toggle').onclick=()=>{clearFeedback();history.replaceState(null,'',state.authMode==='register'?'#login':'#register');$('password').value='';$('confirm-password').value='';displayAuth();};
['new-room','empty-new'].forEach(id=>$(id).onclick=()=>{$('create-dialog').showModal();$('room-name').focus();});
document.querySelectorAll('.close-dialog').forEach(b=>b.onclick=()=>b.closest('dialog').close());
document.querySelectorAll('dialog').forEach(dialog=>dialog.addEventListener('close',clearFeedback));
$('create-form').addEventListener('submit',safely(async e=>{e.preventDefault();const room=await post('/rooms',{name:$('room-name').value});$('create-dialog').close();$('room-name').value='';await selectRoom(room.id);await showSetup();}));
$('rooms').onclick=safely(async e=>{const b=e.target.closest('[data-room]');if(b)await selectRoom(b.dataset.room);});
$('menu').onclick=()=>$('sidebar').classList.toggle('open');
$('logout').onclick=safely(async()=>{await post('/logout');state.me=null;state.room=null;state.messages.clear();clearSetupLink();location.reload();});
$('setup-open').onclick=showSetup;
$('manage-open').onclick=()=>showView('manage');
$('manage-back').onclick=()=>showView('chat');
$('manage-setup').onclick=showSetup;
window.addEventListener('hashchange',safely(async()=>{const route=readRoute();if(state.me&&route)await selectRoom(route.id,route.view,false);else if(!state.me)displayAuth();}));
$('manage-agents').addEventListener('input',e=>{
  if(e.target.matches('.permission-input')){
    const draft=state.permissionDrafts.get(e.target.closest('[data-agent]').dataset.agent);draft.mode=e.target.value;draft.dirty=true;renderManagement();return;
  }
  if(!e.target.matches('.model-input,.custom-model'))return;
  const id=e.target.closest('[data-agent]').dataset.agent,draft=state.modelDrafts.get(id);
  draft.model=e.target.value;draft.dirty=true;draft.error='';renderManagement();
});
$('manage-agents').addEventListener('click',safely(async e=>{
  const card=e.target.closest('[data-agent]');if(!card)return;
  const lifecycle=e.target.closest('[data-session-action]');if(lifecycle)return manageSession(card.dataset.agent,lifecycle.dataset.sessionAction);
  if(e.target.closest('.model-auto')){const draft=state.modelDrafts.get(card.dataset.agent);draft.model='';draft.dirty=true;draft.error='';renderManagement();}
  if(e.target.closest('.model-reload')){state.modelDrafts.delete(card.dataset.agent);renderManagement();}
  if(e.target.closest('.permission-reload')){state.permissionDrafts.delete(card.dataset.agent);renderManagement();}
  if(e.target.closest('.model-refresh')){await post(`/rooms/${state.room}/members/${card.dataset.agent}/models/refresh`);toast('已请求本机刷新模型列表，稍后自动更新');}
}));
document.addEventListener('change',safely(async e=>{
  if(!e.target.matches('[data-session-switch]'))return;
  const field=e.target,room=state.room;field.disabled=true;
  try {await post(`/rooms/${room}/members/${field.dataset.sessionSwitch}/control`,{action:field.checked?'enable':'disable'});toast(field.checked?'已开启 session；等待点名':'已关闭 session，并取消其运行与排队任务');}
  finally {field.disabled=false;if(state.room===room)await refresh();}
}));
$('manage-agents').addEventListener('submit',safely(async e=>{
  e.preventDefault();
  const permissionForm=e.target.closest('[data-permission-form]');
  if(permissionForm){
    const id=permissionForm.dataset.permissionForm,room=state.room,draft=state.permissionDrafts.get(id);
    if(draft.saving)return;draft.saving=true;renderManagement();
    try {const saved=await post(`/rooms/${room}/members/${id}/permissions`,{mode:draft.mode,revision:draft.revision});Object.assign(draft,saved,{dirty:false});toast('权限已保存；本地同步后从下一项任务生效');}
    finally {draft.saving=false;if(state.room===room){await refresh();if(state.view==='manage')renderManagement();}}
    return;
  }
  const id=e.target.closest('[data-model-form]').dataset.modelForm,room=state.room,draft=state.modelDrafts.get(id);
  if(draft.saving)return;
  draft.saving=true;draft.error='';renderManagement();
  try {const saved=await post(`/rooms/${room}/members/${id}/model`,{model:draft.model.trim(),revision:draft.revision});draft.model=saved.model;draft.revision=saved.revision;draft.dirty=false;toast('模型已保存；同步后从下一项任务使用');}
  catch(error){draft.error=error.message;toast(error.message);}
  finally {draft.saving=false;if(state.room===room){await refresh();if(state.view==='manage')renderManagement();}}
}));
$('members-toggle').onclick=()=>$('roster').classList.toggle('open');
$('close-roster').onclick=()=>$('roster').classList.remove('open');
$('room-pause').onclick=safely(async()=>{const pause=!state.snapshot.room.paused;if(pause&&!confirm('暂停会取消排队任务，并通知 bridge 停止正在运行的任务。继续？'))return;await post(`/rooms/${state.room}/control`,{action:pause?'pause':'resume'});await refresh();});
$('members').onclick=safely(async e=>{
  const toggle=e.target.closest('.member-toggle');
  if(toggle){const open=toggle.closest('.member').classList.toggle('expanded');toggle.setAttribute('aria-expanded',String(open));return;}
  const mention=e.target.closest('[data-mention]');if(mention) return insertMention(mention.dataset.mention);
  const b=e.target.closest('[data-member]');if(!b)return;
  if(b.dataset.action==='delete')return manageSession(b.dataset.member,'delete');
  if(b.dataset.action==='revoke'&&!confirm('撤销该成员的 token？撤销 bridge 会同时撤销它的 session。'))return;
  await post(`/rooms/${state.room}/members/${b.dataset.member}/control`,{action:b.dataset.action});await refresh();
});
$('target-chips').onclick=e=>{const b=e.target.closest('[data-mention]');if(b)selectTarget(b.dataset.mention);};
$('messages').onclick=safely(async e=>{
  const recall=e.target.closest('[data-recall]');
  if(recall){
    const id=Number(recall.dataset.recall),room=state.room;if(recall.disabled||state.recalling.has(id))return;
    state.recalling.add(id);recall.disabled=true;
    try {const message=await post(`/rooms/${room}/messages/${id}/recall`);if(state.room===room)state.messages.set(id,message);toast('消息已撤回，排队任务已取消');}
    finally {state.recalling.delete(id);if(state.room===room)await refresh();}
    return;
  }
  const steer=e.target.closest('[data-steer]');
  if(steer) {
    const id=steer.dataset.steer,task_ref=steer.dataset.taskRef,room=state.room;
    if(!task_ref||steer.disabled||state.steering.has(id))return;
    state.steering.add(id);steer.disabled=true;
    try {await post(`/rooms/${room}/deliveries/${id}/steer`,{task_ref});toast('已转为 Steer，原排队任务不会再单独执行');}
    finally {state.steering.delete(id);if(state.room===room)await refresh();}
    return;
  }
  const quote=e.target.closest('[data-quote]');
  if(quote){const quoted=state.messages.get(Number(quote.dataset.quote));if(quoted.private_to){const manager=state.snapshot.members.find(m=>m.id===quoted.private_to);if(!manager||manager.revoked)throw new Error('该 manager 已删除，不能回复私有消息');selectTarget(manager.name,false);}state.quote=quoted;$('quote-preview').hidden=false;$('quote-preview').innerHTML=`<span>引用 ${esc(state.quote.sender_name)}：${esc(state.quote.body.slice(0,90))}</span><button type="button" id="clear-quote" class="text-button">取消</button>`;$('clear-quote').onclick=()=>{state.quote=null;$('quote-preview').hidden=true;};$('message-input').focus({preventScroll:true});}
  const retry=e.target.closest('[data-retry]');if(retry && confirm('任务可能已产生部分文件修改。检查后重新执行？')){await post(`/rooms/${state.room}/deliveries/${retry.dataset.retry}/retry`);await refresh();}
});
function roundLimit() {
  const raw=$('max-rounds').value,value=Number(raw);
  if(!raw.trim()||!Number.isInteger(value)||value<0||value>10000)throw new Error('最大轮数需为 0–10000 的整数，0 为无限');
  return value;
}
function renderRoundMode() {
  const mode=$('round-mode').value,individual=mode==='individual',sync=mode==='synchronous',manager=!!selectedManager();
  $('global-round-limit').hidden=individual||manager;$('round-details').hidden=(!individual&&!sync)||manager;$('round-mode').hidden=manager;
  $('round-budget-controls').hidden=manager;
  const description={global:'被 @ 后独立执行；按接力链计算轮数，达到上限后收尾。',individual:'各自设置执行次数；用完自己的额度后停止，其他人继续。',synchronous:'每轮等待所有参与者完成，再一起推进；可分别限制每轮发言次数。'}[mode];
  if($('round-mode-description').textContent!==description)$('round-mode-description').textContent=description;
  $('round-mode-description').classList.toggle('is-private',manager);
  $('round-mode').disabled=!!activeSync();$('max-rounds').disabled=!!activeSync();
  $('round-details').textContent=sync?'每轮发言次数':'设置个人额度';
  $('global-round-limit').title=sync?'全群共同推进的轮数；0 为无限。最后一轮只做决定与总结。':'每条新任务的接力上限；0 为无限。';
  if(state.snapshot)renderSynchronous();
  window.AgentChatMobile?.update();
}
function saveRoundLimit(room,value,mode=$('round-mode').value,limits={...state.sessionRounds},speaks={...state.sessionSpeaks}) {
  roundSaveQueue=roundSaveQueue.catch(()=>{}).then(async()=>{
    await api(`/rooms/${room}/round-limit`,{method:'PUT',body:JSON.stringify({max_rounds:value,round_mode:mode,session_rounds:limits,session_speaks:speaks})});
    if(state.room===room&&Number($('max-rounds').value)===value&&$('round-mode').value===mode){state.roundsDirty=false;Object.assign(state.snapshot.room,{max_rounds:value,round_mode:mode,session_rounds:limits,session_speaks:speaks});}
    if(state.room===room)toast(mode==='synchronous'?`同步讨论已保存：${value||'无限'} 轮，等待所有本轮参与者完成后推进`:mode==='individual'?'个人配额已保存：新任务按各 session 的执行次数分别计数':value?`自由接力已保存：最多 ${value} 轮，最后一轮收尾`:'自由接力已保存：不限制轮数，可随时停止所有');
  });
  return roundSaveQueue;
}
$('max-rounds').addEventListener('input',()=>{
  state.roundsDirty=true;clearTimeout(roundSaveTimer);
  try {const room=state.room,value=roundLimit();roundSaveTimer=setTimeout(()=>saveRoundLimit(room,value).catch(error=>toast(error.message)),400);}catch{}
});
$('round-mode').addEventListener('change',safely(async()=>{state.roundsDirty=true;renderRoundMode();clearTimeout(roundSaveTimer);await saveRoundLimit(state.room,roundLimit());}));
$('round-details').onclick=()=>{
  const sync=$('round-mode').value==='synchronous',locked=sync&&!!activeSync();$('rounds-dialog').dataset.mode=sync?'synchronous':'individual';
  $('rounds-title').textContent=sync?'每个 session 每轮发言次数':'个人执行配额';
  $('rounds-description').textContent=sync?'默认每人每轮 1 次，可分别设置 1–100 次。最终回复即完成本轮，不必用满额度；同轮点名合并，下一轮再触发。'+(locked?'当前对话预算已固定。':''):'每条新任务中，各 session 的执行次数分别计数。用完个人额度后不再接收该任务的点名；最后一次只做总结。0 表示无限。';
  $('save-session-rounds').disabled=locked;$('save-session-rounds').textContent=locked?'当前对话预算已固定':sync?'保存发言次数':'保存个人额度';
  $('session-round-fields').replaceChildren(...state.snapshot.members.filter(m=>m.kind==='agent'&&!m.revoked&&(!sync||m.session_type!=='manager')).map(m=>{
    const label=document.createElement('label');label.className='session-round-row';label.innerHTML=`<span>${esc(m.name)}<small>${esc(m.harness)}${m.session_type==='manager'?' · 私有 manager':''}</small></span><input type="number" min="${sync?1:0}" max="${sync?100:10000}" step="1" inputmode="numeric" data-round-member="${esc(m.id)}" aria-label="${esc(m.name)} ${sync?'每轮发言次数':'执行次数'}" ${locked?'disabled':''}><span class="muted small">${sync?'次 / 轮':'0 = 无限'}</span>`;
    label.querySelector('input').value=sync?(state.sessionSpeaks[m.id]??1):(state.sessionRounds[m.id]??roundLimit());return label;
  }));$('rounds-dialog').showModal();
};
$('save-session-rounds').onclick=safely(async()=>{
  const sync=$('rounds-dialog').dataset.mode==='synchronous';
  const limits={};for(const field of document.querySelectorAll('[data-round-member]')){const value=Number(field.value);if(!field.value.trim()||!Number.isInteger(value)||value<(sync?1:0)||value>(sync?100:10000))throw new Error(sync?'每轮发言次数需为 1–100 的整数':'每个阈值需为 0–10000 的整数');limits[field.dataset.roundMember]=value;}
  if(sync)state.sessionSpeaks=limits;else state.sessionRounds=limits;state.roundsDirty=true;await saveRoundLimit(state.room,roundLimit());$('rounds-dialog').close();toast(sync?'已保存每轮发言次数':'已保存每个 session 的执行配额');
});
$('max-rounds').addEventListener('change',safely(async()=>{
  if(!state.roundsDirty)return;clearTimeout(roundSaveTimer);await saveRoundLimit(state.room,roundLimit());
}));
$('stop-all').onclick=safely(async()=>{
  const room=state.room;$('stop-all').disabled=true;
  try {const result=await post(`/rooms/${room}/control`,{action:'stop-all'});toast(`已停止本群协作，取消 ${result.cancelled} 项执行或排队任务`);if(state.room===room)await refresh();}
  finally {$('stop-all').disabled=false;}
});
$('sync-panel').onclick=safely(async e=>{
  const retry=e.target.closest('[data-sync-retry]'),proceed=e.target.closest('#sync-continue');if(!retry&&!proceed)return;
  const run=activeSync(),room=state.room;if(!run||state.syncBusy)return;
  if(retry&&!confirm('任务可能已产生部分文件修改。检查后重新执行本轮任务？'))return;
  state.syncBusy=true;renderSynchronous();
  try {if(retry)await post(`/rooms/${room}/deliveries/${retry.dataset.syncRetry}/retry`);else await post(`/rooms/${room}/synchronous/continue`,{root:run.root,round:run.round});toast(retry?'已重试本轮任务':'已跳过失败，等待其他参与者完成后继续');}
  finally {state.syncBusy=false;if(state.room===room)await refresh();}
});
$('composer').addEventListener('submit',safely(async e=>{
  closeMentions();
  e.preventDefault();if($('send').disabled)return;const room=state.room,channel=state.channel,text=$('message-input').value.trim(),reply_to=state.quote?.id||null,max_rounds=channel?0:roundLimit(),round_mode=channel?'global':$('round-mode').value,session_rounds=channel?{}:{...state.sessionRounds},session_speaks=channel?{}:{...state.sessionSpeaks},to=[...state.targets];
  if(!text)throw new Error('请填写消息正文');
  const body=to.map(n=>'@'+n).join(' ')+'\n'+text;
  const payload={body,to,reply_to,max_rounds,round_mode,session_rounds,session_speaks};
  if(!state.pendingSend||JSON.stringify({...state.pendingSend,nonce:undefined})!==JSON.stringify(payload))state.pendingSend={...payload,nonce:crypto.randomUUID?crypto.randomUUID():`${Date.now()}-${Math.random()}`};
  const pending=state.pendingSend;$('send').disabled=true;
  try {
    await post(`/rooms/${room}/messages`,pending);
    if(state.room===room&&state.channel===channel){if(state.pendingSend===pending)state.pendingSend=null;if($('message-input').value.trim()===text)$('message-input').value='';if(state.quote?.id===reply_to){state.quote=null;$('quote-preview').hidden=true;}await refresh();renderMessages(true);}
  } finally {$('send').disabled=false;window.AgentChatMobile?.fitInput();}
}));
$('message-input').addEventListener('keydown',e=>{if(!e.defaultPrevented&&e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();if(!e.repeat&&!$('send').disabled)$('composer').requestSubmit();}});
$('max-rounds').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();e.target.blur();}});
$('copy-api-url').onclick=safely(async()=>{if(!$('api-url').value)throw new Error('请先填写 API 地址');await copy($('api-url').value.trim());});
function makeRoleRow(definition={}) {
  const row=document.createElement('div');row.className='role-row';row.innerHTML='<input class="role-name" placeholder="session 名称" aria-label="session 名称"><select class="role-harness" aria-label="harness"><option value="">选择 harness</option><option value="codex">Codex</option><option value="claude">Claude Code</option><option value="antigravity">Antigravity</option><option value="deepseek">DeepSeek Harness</option><option value="command">自定义命令</option></select><button class="icon-button" aria-label="移除 session">×</button><input class="role-task" placeholder="这个 session 的具体分工" aria-label="分工"><details><summary>适配参数（可选 JSON）</summary><textarea class="role-extra" rows="3" placeholder="{}" aria-label="适配参数"></textarea></details>';
  const {name='',harness='',role='',session_type,...extra}=definition;
  row.querySelector('.role-name').value=name;row.querySelector('.role-harness').value=harness;row.querySelector('.role-task').value=role;
  row.querySelector('.role-extra').value=Object.keys(extra).length?JSON.stringify(extra,null,2):'';
  row.querySelector('button').onclick=()=>{row.remove();$('no-roles').hidden=!!$('roles').children.length;clearSetupLink();};
  return row;
}
$('add-role').onclick=()=>{const row=makeRoleRow();$('roles').appendChild(row);$('no-roles').hidden=true;clearSetupLink();$('roles').scrollTop=$('roles').scrollHeight;row.querySelector('.role-name').focus({preventScroll:true});};
$('roles-file').addEventListener('change',safely(async()=>{
  const file=$('roles-file').files[0];$('roles-file').value='';if(!file)return;$('json-dialog').close();
  if(file.size>128*1024)throw new Error('角色文件不能超过 128 KB');
  let roles;try {roles=JSON.parse((await file.text()).replace(/^\uFEFF/,''));} catch {throw new Error('文件不是合法 JSON；当前角色未更改');}
  if(await importRoleSet(roles,file.name)){state.templateSelected='';$('role-template').value='';templateButtons();}
}));
$('role-template').addEventListener('change',safely(async()=>{
  const selected=$('role-template').value,previous=state.templateSelected;
  if(selected==='__import__'||selected==='__export__'){$('role-template').value=previous;if(selected==='__import__')$('json-dialog').showModal();else $('download-roles').click();return;}
  if(!selected){state.templateSelected='';templateButtons();return;}
  const template=state.templates.find(t=>t.id===selected);if(!template)return;
  state.templateSaving=true;templateButtons();
  try {state.templateSelected=await importRoleSet(template.roles,template.name)?selected:previous;}
  finally {$('role-template').value=state.templateSelected;state.templateSaving=false;templateButtons();}
}));
$('overwrite-template').onclick=safely(async()=>{
  const selected=state.templates.find(t=>t.id===state.templateSelected);if(!selected)return;
  const roles=readRoles();state.templateSaving=true;templateButtons();
  try {const saved=await api('/role-templates/'+selected.id,{method:'PUT',body:JSON.stringify({name:selected.name,roles,revision:selected.revision})});await loadTemplates(saved.id);toast(`已覆盖模板「${saved.name}」`);}
  finally {state.templateSaving=false;templateButtons();}
});
$('new-template').onclick=safely(async()=>{
  const roles=readRoles(),answer=await choose('新建角色模板','将当前角色保存为可跨群复用的模板。',[{id:'create',label:'新建'},{id:'cancel',label:'取消'}],true);
  if(answer.action==='cancel')return;
  state.templateSaving=true;templateButtons();
  try {const saved=await post('/role-templates',{name:answer.value,roles});await loadTemplates(saved.id);toast(`已新建模板「${saved.name}」`);}
  finally {state.templateSaving=false;templateButtons();}
});
$('delete-template').onclick=safely(async()=>{
  const selected=state.templates.find(t=>t.id===state.templateSelected);if(!selected)return;
  if((await choose('删除角色模板',`删除「${selected.name}」？当前角色和已保存的群聊配置会保留。`,[{id:'delete',label:'删除'},{id:'cancel',label:'取消'}])).action==='cancel')return;
  state.templateSaving=true;templateButtons();
  try {await api('/role-templates/'+selected.id+'?revision='+selected.revision,{method:'DELETE'});await loadTemplates('');toast('模板已删除');}
  finally {state.templateSaving=false;templateButtons();}
});
$('download-roles').onclick=event=>{
  clearFeedback();
  try {
    const roles=readRoles(),url=URL.createObjectURL(new Blob([JSON.stringify(roles,null,2)],{type:'application/json'}));
    event.currentTarget.href=url;
    setTimeout(()=>URL.revokeObjectURL(url),60000);
    toast('已发起 roles.json 下载，请在浏览器下载列表查看');
  } catch(error) {event.preventDefault();toast(error.message);}
};
function setupPayload() {
  const inherit_manager=$('same-as-manager').checked,roles=readRoles();
  const workspace=inherit_manager?'':$('workspace-path').value.trim(),project_name=inherit_manager?'':$('project-name').value.trim();
  if(!inherit_manager&&!workspace)throw new Error('请填写本地 workspace 路径');
  if(!inherit_manager&&!project_name)throw new Error('请填写 project 名称');
  return {workspace,project_name,inherit_manager,roles,revision:state.setupRevision};
}
$('same-as-manager').onchange=()=>{renderProjectChoice();clearSetupLink();};
$('reload-setup').onclick=safely(loadSetupProfile);
$('save-setup').onclick=safely(async()=>{
  if(state.sessionChangeMode)return submitSessionChanges();
  if(state.setupSaving||state.setupLoading)return;
  const room=state.room,payload=setupPayload();state.setupSaving=true;$('save-setup').disabled=true;$('copy-setup').disabled=true;
  try {
    const saved=await api(`/rooms/${room}/setup-profile`,{method:'PUT',body:JSON.stringify(payload)});
    if(room===state.room){state.setupRevision=saved.revision;$('setup-saved').textContent='接入配置已保存到服务器';toast('已保存群聊接入配置');}
  } finally {state.setupSaving=false;$('save-setup').disabled=false;$('copy-setup').disabled=false;}
});
$('copy-setup').onclick=safely(async()=>{
  if(state.setupSaving||state.setupLoading)return;
  const room=state.room,payload=setupPayload(),url=$('api-url').value.trim().replace(/\/+$/,'');
  let parsed;try{parsed=new URL(url);}catch{throw new Error('请填写有效的 Agent API 地址');}
  if(!['http:','https:'].includes(parsed.protocol)||parsed.username||parsed.password||parsed.search||parsed.hash)throw new Error('API 地址必须是没有凭证、查询或 # 的 HTTP(S) 地址');
  if(location.protocol==='https:'&&parsed.protocol!=='https:')throw new Error('公网接入地址请使用 HTTPS');
  const key=JSON.stringify({room,url,workspace:payload.workspace,project_name:payload.project_name,inherit_manager:payload.inherit_manager,roles:payload.roles});
  if(state.setupLink?.key===key){await copy(state.setupLink.text);return;}
  if(state.setupPending?.key!==key)state.setupPending={key,nonce:crypto.randomUUID?crypto.randomUUID():`${Date.now()}-${Math.random()}`,credential:Array.from(crypto.getRandomValues(new Uint8Array(32)),x=>x.toString(16).padStart(2,'0')).join('')};
  const pending=state.setupPending;state.setupSaving=true;$('copy-setup').disabled=true;$('save-setup').disabled=true;
  try {
    const issued=await post(`/rooms/${room}/setup-invitations`,{...payload,nonce:pending.nonce,credential:pending.credential});
    if(room!==state.room)return;
    state.setupRevision=issued.revision;localStorage.setItem('agentchat-api-url',url);
    const text=`请按此链接完成本机接入，并在完成后回报、刷新接入 token：\n${url}/setup/${issued.id}#token=${pending.credential}`;
    state.setupLink={id:issued.id,key,text};state.setupPending=null;
    $('setup-text').value=text;$('setup-preview').hidden=false;$('setup-saved').textContent='接入配置已保存到服务器';
    renderSetupStatus(issued);
    await copy(text);
  } finally {state.setupSaving=false;$('copy-setup').disabled=false;$('save-setup').disabled=false;}
});
$('select-setup-text').onclick=()=>{$('setup-text').focus();$('setup-text').select();toast('文本已选中，请按 Ctrl+C（Mac 为 ⌘C）复制');};
$('setup-dialog').addEventListener('input',event=>{if(event.target.matches('#workspace-path,#project-name,#same-as-manager,#api-url')||event.target.closest('.role-row'))clearSetupLink();});
setInterval(async()=>{if(state.polling || !state.me || document.hidden)return;state.polling=true;try{await refresh();}catch{setConnection(false);}finally{state.polling=false;}},2500);
initAppearance();
initMentions();
initReading();
boot();
