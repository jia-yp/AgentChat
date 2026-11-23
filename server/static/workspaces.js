/* Workspace settings are owner-only and don't replace an in-progress draft. */
let workspaceRoom='',workspaceData=null,workspaceLoading='',workspaceRequest=0;
async function loadWorkspacePolicies(force=false) {
  if(state.view!=='manage'||!roomOwner()||workspaceLoading===state.room||(!force&&workspaceRoom===state.room))return;
  const room=state.room,request=++workspaceRequest;workspaceLoading=room;
  if(workspaceRoom!==room){
    workspaceData=null;$('workspace-policy-select').replaceChildren();
    $('workspace-policy-form').hidden=true;$('workspace-policy-empty').hidden=false;
    $('workspace-policy-empty').textContent='正在载入…';
  }
  try {
    const data=await api(`/rooms/${room}/workspaces`);
    if(state.room!==room||request!==workspaceRequest)return;
    workspaceRoom=room;workspaceData=data;
    const select=$('workspace-policy-select'),selected=select.value;
    select.innerHTML=data.workspaces.map(w=>`<option value="${esc(w.id)}">${esc(w.project||w.name)} · ${esc(w.name)}</option>`).join('');
    if(data.workspaces.some(w=>w.id===selected))select.value=selected;
    renderWorkspacePolicy();
  } finally {if(request===workspaceRequest)workspaceLoading='';}
}
window.loadWorkspacePolicies=loadWorkspacePolicies;
function renderWorkspacePolicy() {
  const w=workspaceData?.workspaces.find(w=>w.id===$('workspace-policy-select').value);
  $('workspace-policy-form').hidden=!w;$('workspace-policy-empty').hidden=!!w;
  $('workspace-policy-empty').textContent='接入 gateway 后可设置。';
  if(!w)return;
  $('workspace-policy-path').textContent=w.workspace||'等待本地上报路径';
  $('workspace-manager-sessions').checked=!!w.manager_sessions;
  $('workspace-policy-fields').innerHTML=Object.entries(w.modes).map(([harness,mode])=>`<label>${esc(harness)}<select data-workspace-harness="${esc(harness)}">${(workspaceData.options[harness]||[]).map(o=>`<option value="${esc(o.id)}" ${o.id===mode?'selected':''}>${esc(o.label)}</option>`).join('')}</select></label>`).join('');
  $('workspace-policy-form').querySelectorAll('input,select,button').forEach(el=>el.disabled=!w.workspace_supported);
  $('workspace-policy-status').textContent=w.workspace_supported?'新 session 默认继承；已有 session 保留原权限，可单独选择跟随。':'需更新并重启此 workspace 的 gateway。';
}
$('workspace-policy-select').onchange=renderWorkspacePolicy;
$('workspace-policy-reload').onclick=safely(()=>loadWorkspacePolicies(true));
$('workspace-policies').ontoggle=()=>{if($('workspace-policies').open)loadWorkspacePolicies().catch(e=>toast(e.message));};
$('workspace-policy-form').onsubmit=safely(async e=>{
  e.preventDefault();
  const room=state.room,w=workspaceData.workspaces.find(w=>w.id===$('workspace-policy-select').value),button=$('workspace-policy-save');
  const modes=Object.fromEntries([...$('workspace-policy-fields').querySelectorAll('select')].map(el=>[el.dataset.workspaceHarness,el.value]));
  button.disabled=true;
  try {
    const saved=await post(`/rooms/${room}/workspaces/${w.id}`,{modes,revision:w.revision,manager_sessions:$('workspace-manager-sessions').checked});
    Object.assign(w,saved);toast('Workspace 默认权限已保存');if(state.room===room)await refresh();
  } finally {if(state.room===room)button.disabled=!w.workspace_supported;}
});

const lifecycleNonces=new Map();
async function transitionSession(member,action) {
  const room=state.room,id=member.id,life=member.lifecycle;
  let immediate=false;
  if(action==='leave'){
    const pending=life?.state==='leaving';
    const choice=await choose(pending?`${member.name} 正在退出`:`退出 ${member.name}？`,pending?'当前任务完成后退出。也可立即停止，或取消退出。':'停止接收新任务，保留历史、文件和原生会话。',pending?
      [{id:'immediate',label:'立即退出'},{id:'cancel-leave',label:'取消退出'},{id:'cancel',label:'返回'}]:
      [{id:'finish',label:'完成当前任务后退出'},{id:'immediate',label:'立即退出'},{id:'cancel',label:'取消'}]);
    if(choice.action==='cancel')return;
    if(choice.action==='cancel-leave')action='cancel-leave';
    immediate=choice.action==='immediate';
  }
  const key=`${room}:${id}:${action}`;
  if(!lifecycleNonces.has(key))lifecycleNonces.set(key,crypto.randomUUID());
  const nonce=action==='leave'&&life?.state==='leaving'?life.nonce:lifecycleNonces.get(key);
  state.sessionActions.add(id);renderManagement();
  try {
    const result=await post(`/rooms/${room}/members/${id}/lifecycle`,{action,nonce,immediate});
    lifecycleNonces.delete(key);
    toast(action==='cancel-leave'?'已取消退出；已取消的任务不会重放':result.state==='left'?'已退出托管':action==='rejoin'?'等待本地重新接入':'已停止接收新任务，等待本地退出确认');
  } finally {state.sessionActions.delete(id);if(state.room===room)await refresh();}
}
