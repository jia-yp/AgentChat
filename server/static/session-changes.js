/* Owner-authored changes: the manager approves, its existing bridge applies. */
function setSessionChangeMode(enabled) {
  if(state.sessionChangeMode!==enabled){state.setupRoom=null;state.setupSummary=null;clearSetupLink();}
  state.sessionChangeMode=enabled;
  $('setup-title').textContent=enabled?'增减 session':'把本地 session 带进来';
  $('role-heading-text').textContent=enabled?'新增角色与分工':'协作角色与分工';
  $('no-roles').textContent=enabled?'添加新角色，或选择模板；只移除时留空即可。':'还没有角色。添加角色，或从上方菜单选择模板／导入 JSON。';
  document.querySelectorAll('[data-initial-setup]').forEach(el=>el.hidden=enabled);
  $('session-change-editor').hidden=!enabled;
  $('session-change-jobs').hidden=!enabled;
  $('connect-other').hidden=!enabled;
  $('copy-setup').hidden=enabled;$('setup-preview').hidden=enabled;
  $('save-setup').textContent=enabled?'交给 manager 处理':'保存接入配置';
  $('save-setup').className=enabled?'primary':'secondary';
  $('setup-dialog').classList.toggle('connected-setup',enabled);
}
async function openSessionChanges(data=null) {
  const room=state.room;
  data=data||await api(`/rooms/${room}/session-changes`);
  if(room!==state.room)return true;
  if(!data.managers.length)return false;
  const reset=!state.sessionChangeMode||state.setupRoom!==room;
  setSessionChangeMode(true);state.setupRoom=room;state.changeInventory=data;
  if(reset){loadRoles([]);state.changeRemovals=new Set();state.changePending=null;state.changeManager='';}
  renderSessionManagers();renderSessionChanges();
  if(!$('setup-dialog').open)$('setup-dialog').showModal();
  await loadTemplates();return true;
}
async function loadSessionChanges(reset=false) {
  const room=state.room;
  const data=await api(`/rooms/${room}/session-changes`);
  if(room!==state.room||!state.sessionChangeMode)return;
  state.changeInventory=data;
  if(reset){loadRoles([]);state.changeRemovals=new Set();state.changePending=null;$('session-change-current').dataset.signature='';}
  renderSessionManagers();renderSessionChanges();
}
function renderSessionManagers() {
  const managers=state.changeInventory.managers,select=$('session-change-manager');
  if(!managers.some(m=>m.id===state.changeManager))state.changeManager=managers.length===1?managers[0].id:'';
  const options='<option value="">选择 manager 与项目</option>'+managers.map(m=>`<option value="${esc(m.id)}">${esc(m.name)} · ${esc(m.project||m.bridge_name)}</option>`).join('');
  if(select.innerHTML!==options)select.innerHTML=options;
  select.value=state.changeManager;
  const manager=managers.find(m=>m.id===select.value);
  const available=manager?.supported&&!manager.paused&&!manager.bridge_paused;
  $('session-change-project').textContent=manager?`${manager.project||manager.bridge_name} · ${manager.workspace||'尚未回报路径'}`:'';
  $('session-change-help').textContent=!manager?'选择负责本次变更的 manager。':!manager.supported?'此 bridge 需要更新并重启，才能接收 session 变更。':manager.paused||manager.bridge_paused?'请先启用 manager 和 bridge。':manager.online?'沿用当前项目。新增 session 等待你首次点名；移除保留历史和文件。':'bridge 暂时离线；提交会保存，重新连接后由 manager 处理。';
  const active=state.changeInventory.changes.some(c=>c.bridge===manager?.bridge&&['pending','running','applying'].includes(c.status));
  $('save-setup').disabled=!available||active||state.setupSaving;
  const html=(manager?.sessions||[]).map(m=>`<label class="session-change-member"><span><strong>${esc(m.name)}</strong><small>${esc(m.harness)} · ${esc(m.role)}</small></span><span class="session-change-remove"><input type="checkbox" data-remove-session="${esc(m.id)}" ${state.changeRemovals?.has(m.id)?'checked':''}>移除</span></label>`).join('')||'<p class="muted small">当前没有协作 session。</p>';
  // Reconcile only when membership changes; preserve focus and scroll during polling.
  const list=$('session-change-current');
  const signature=JSON.stringify([manager?.id,manager?.sessions]);
  if(list.dataset.signature!==signature){list.innerHTML=html;list.dataset.signature=signature;}
}
function renderSessionChanges() {
  const labels={pending:'等待 manager',running:'manager 正在处理',applying:'本地应用中',complete:'已完成',failed:'未完成',cancelled:'已取消'};
  const jobs=state.changeInventory.changes.slice(0,8);
  const html=jobs.map(c=>`<div class="session-change-job"><span><strong>${esc(labels[c.status]||c.status)}</strong> · ${esc(c.manager_name)} · ${formatTime(c.created)}</span><small>${esc([c.additions.length?'新增 '+c.additions.map(x=>x.name).join('、'):'',c.removals.length?'移除 '+c.removals.map(x=>x.name).join('、'):''].filter(Boolean).join('；'))}</small>${c.error?`<small class="error">${esc(c.error)}</small>`:''}<div>${c.status==='failed'?`<button class="text-button" data-change="${esc(c.id)}" data-action="retry">重试</button>`:''}${['pending','running','applying'].includes(c.status)?`<button class="text-button" data-change="${esc(c.id)}" data-action="cancel">取消未完成部分</button>`:''}</div></div>`).join('')||'<p class="muted small">提交后的进度会显示在这里。</p>';
  const box=$('session-change-jobs');if(box.dataset.content!==html){box.innerHTML=html;box.dataset.content=html;}
}
async function submitSessionChanges() {
  if(state.setupSaving)return;
  const manager=state.changeInventory.managers.find(m=>m.id===state.changeManager);
  if(!manager)throw new Error('请选择 manager');
  const additions=readRoles(true),removals=[...(state.changeRemovals||[])];
  if(!additions.length&&!removals.length)throw new Error('请添加角色，或勾选要移除的 session');
  const names=new Set((state.snapshot?.members||[]).map(m=>m.name));
  if(additions.some(r=>names.has(r.name)))throw new Error('新增名称与已有 session 重复；已删除的 session 请在群聊管理中恢复');
  const room=state.room,payload={manager:manager.id,additions,removals,expected:manager.revision};
  const key=JSON.stringify(payload);
  if(state.changePending?.key!==key)state.changePending={key,nonce:crypto.randomUUID()};
  state.setupSaving=true;$('save-setup').disabled=true;
  try {
    await post(`/rooms/${room}/session-changes`,{...payload,nonce:state.changePending.nonce});
    if(room===state.room){toast('已交给 manager；完成后会收到私有回执');await loadSessionChanges(true);}
  } finally {state.setupSaving=false;if(room===state.room)renderSessionManagers();}
}
async function openSessionRemoval(member) {
  const data=await api(`/rooms/${state.room}/session-changes`);
  const manager=data.managers.find(m=>m.bridge===member.bridge&&m.supported&&!m.paused&&!m.bridge_paused);
  if(!manager)return false;
  await openSessionChanges(data);state.changeManager=manager.id;state.changeRemovals.add(member.id);
  $('session-change-current').dataset.signature='';renderSessionManagers();
  toast('已勾选移除；点击「交给 manager 处理」提交');return true;
}
$('manage-sessions').onclick=()=>showSetup();
$('connect-other').onclick=()=>showSetup(true);
$('session-change-manager').onchange=()=>{state.changeManager=$('session-change-manager').value;state.changeRemovals=new Set();state.changePending=null;renderSessionManagers();};
$('session-change-current').onchange=e=>{const id=e.target.dataset.removeSession;if(id){e.target.checked?state.changeRemovals.add(id):state.changeRemovals.delete(id);state.changePending=null;}};
$('session-change-jobs').onclick=safely(async e=>{
  const button=e.target.closest('[data-change]');if(!button)return;
  button.disabled=true;
  try {await post(`/rooms/${state.room}/session-changes/${button.dataset.change}/control`,{action:button.dataset.action});await loadSessionChanges();toast(button.dataset.action==='retry'?'已重新交给 manager':'已取消未完成部分');}
  finally {button.disabled=false;}
});
setInterval(()=>{if(state.sessionChangeMode&&$('setup-dialog').open&&!state.changePolling){state.changePolling=true;loadSessionChanges().catch(()=>{}).finally(()=>state.changePolling=false);}},3000);
