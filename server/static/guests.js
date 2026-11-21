/* Human room membership. Server checks remain authoritative on every request. */
function roomOwner(){return state.snapshot?.viewer?.role==='owner';}
function canSpeak(){return ['owner','participant'].includes(state.snapshot?.viewer?.role);}
function humanRole(member){return {owner:'创建者',participant:'参与者',observer:'观察者'}[member?.human_role]||'成员';}
function canEditAppearance(member){return roomOwner()||member.id===state.snapshot?.viewer?.member_id;}
function guestMemberActions(member){
  if(member.kind!=='user')return '';
  const own=member.id===state.snapshot.viewer.member_id,editable=roomOwner()||own;
  const role=roomOwner()&&member.human_role!=='owner'?`<label class="guest-role-label">权限<select data-guest-role="${esc(member.id)}" aria-label="${esc(member.name)} 的权限"><option value="observer" ${member.human_role==='observer'?'selected':''}>观察者</option><option value="participant" ${member.human_role==='participant'?'selected':''}>参与者</option></select></label>`:'';
  return role+`<div class="member-actions">${canSpeak()&&!own?`<button type="button" class="text-button" data-mention="${esc(member.name)}">发给 TA</button>`:''}${editable?`<button type="button" class="text-button" data-guest-edit="${esc(member.id)}">修改名字</button>`:''}${roomOwner()&&member.human_role!=='owner'?`<button type="button" class="text-button danger" data-guest-kick="${esc(member.id)}">移出群聊</button>`:''}</div>`;
}
function applyGuestPermissions(){
  const creator=roomOwner();
  for(const id of ['manage-open','room-pause','setup-open','invite-open','stop-all','manage-sessions'])$(id).hidden=!creator;
  $('workspace').classList.toggle('guest-room',!creator);
  $('composer').hidden=!canSpeak();$('observer-notice').hidden=canSpeak();
  $('history-search-scope').querySelector('[value="private"]').hidden=!creator;
  if(!creator&&$('history-search-scope').value==='private')$('history-search-scope').value='all';
  document.querySelector('.roster-title h2').title=creator?'点击成员管理资料、权限和 session':'点击自己的成员卡片修改名字和外观';
  document.querySelector('.roster-hint').textContent=creator?'点击成员查看详情和操作。':'点击自己的卡片修改名字和外观。';
  if(!creator&&state.view==='manage')showView('chat');
  $('send').disabled=!canSpeak()||!!state.sending;
}
function rememberInvitation(){
  const match=location.hash.match(/^#join\/([A-Za-z0-9_-]{32,128})$/);
  if(match){state.guestInviteToken=match[1];sessionStorage.setItem('agentchat-invitation',match[1]);}
  else if(!state.guestInviteToken)state.guestInviteToken=sessionStorage.getItem('agentchat-invitation')||'';
}
function clearInvitation(){state.guestInviteToken='';sessionStorage.removeItem('agentchat-invitation');}
async function showGuestJoin(){
  rememberInvitation();if(!state.guestInviteToken)return false;
  $('guest-join-name').value=state.me.name;$('guest-join-submit').disabled=true;$('guest-join-name-label').hidden=false;
  $('guest-join-title').textContent='加入群聊';$('guest-join-info').textContent='正在读取邀请…';
  if(!$('guest-join-dialog').open)$('guest-join-dialog').showModal();
  try{
    const info=await post('/guest-invitations/preview',{token:state.guestInviteToken});
    $('guest-join-title').textContent=info.name;
    $('guest-join-info').textContent=info.joined?'你已加入这个群聊。':info.role==='observer'?'以观察者加入 · 可阅读公开讨论':'以参与者加入 · 可阅读和发起公开讨论';
    $('guest-join-name').value=info.member_name;$('guest-join-name-label').hidden=info.joined;
    $('guest-join-submit').textContent=info.joined?'进入群聊':'加入群聊';$('guest-join-submit').disabled=false;
  }catch(error){$('guest-join-info').textContent=error.message;}
  return true;
}
function renderGuestInvitation(link){
  state.guestLink=link;
  $('guest-link').value=link?`${location.origin}${location.pathname}#join/${link.token}`:'';
  $('guest-copy').disabled=!link;$('guest-rotate').hidden=!link;$('guest-create').hidden=!!link;
  if(link)$('guest-link-role').value=link.role;
}
async function openGuestInvitation(){
  if(!roomOwner())return;
  state.invitationRoom=state.room;renderGuestInvitation(null);$('guest-link-role').value='observer';
  $('guest-create').disabled=true;$('invite-dialog').showModal();
  try{const link=await api(`/rooms/${state.invitationRoom}/guest-invitation`);renderGuestInvitation(link);}
  finally{$('guest-create').disabled=false;}
}
async function saveGuestInvitation(rotate=false){
  if(!roomOwner()||state.invitationRoom!==state.room)return;
  const link=await post(`/rooms/${state.invitationRoom}/guest-invitation`,{role:$('guest-link-role').value,rotate});
  renderGuestInvitation(link);toast(rotate?'链接已刷新，旧链接已失效':'邀请链接已保存');
}
async function leaveUnavailableRoom(){
  state.room=null;state.snapshot=null;state.generation++;state.messages.clear();state.quote=null;state.channel='';state.channelDrafts.clear();
  resetReading();closeMentions();$('messages').replaceChildren();$('members').replaceChildren();$('manage-agents').replaceChildren();
  $('history-results').replaceChildren();$('history-search-panel').hidden=true;$('history-preview').hidden=true;
  $('workspace').hidden=true;$('manage-page').hidden=true;$('empty').hidden=false;
  for(const dialog of document.querySelectorAll('dialog[open]'))dialog.close();
  history.replaceState(null,'',location.pathname+location.search);
  await listRooms();toast('你已不在这个群聊中；可使用有效邀请链接重新加入');
}
function initGuestUI(){
  rememberInvitation();
  $('invite-open').onclick=safely(openGuestInvitation);
  $('guest-create').onclick=safely(()=>saveGuestInvitation());
  $('guest-rotate').onclick=safely(()=>saveGuestInvitation(true));
  $('guest-copy').onclick=safely(()=>copy($('guest-link').value));
  $('guest-link').onclick=e=>e.target.select();
  $('guest-link-role').onchange=safely(async()=>{if(state.guestLink)await saveGuestInvitation();});
  $('guest-join-form').onsubmit=safely(async e=>{
    e.preventDefault();$('guest-join-submit').disabled=true;
    try{
      const joined=await post('/guest-invitations/join',{token:state.guestInviteToken,name:$('guest-join-name').value});
      clearInvitation();$('guest-join-dialog').close();await selectRoom(joined.room);toast('已加入群聊');
    }finally{$('guest-join-submit').disabled=false;}
  });
  $('guest-join-dialog').addEventListener('close',safely(async()=>{if(state.guestInviteToken){clearInvitation();history.replaceState(null,'',location.pathname+location.search);await listRooms();}}));
  $('members').addEventListener('click',safely(async e=>{
    const edit=e.target.closest('[data-guest-edit]'),kick=e.target.closest('[data-guest-kick]');
    if(edit){const member=state.snapshot.members.find(m=>m.id===edit.dataset.guestEdit);if(!member)return;
      state.guestEditing={room:state.room,id:member.id};$('guest-name').value=member.name;$('guest-profile-dialog').showModal();$('guest-name').focus();}
    if(kick){const member=state.snapshot.members.find(m=>m.id===kick.dataset.guestKick);if(!member)return;
      if((await choose('移出群聊',`将 ${member.name} 移出？对方可凭有效邀请链接重新加入。`,[{id:'remove',label:'移出'},{id:'cancel',label:'取消'}])).action!=='remove')return;
      await api(`/rooms/${state.room}/guests/${member.id}`,{method:'PATCH',body:JSON.stringify({kick:true})});await refresh();toast('已移出群聊');}
  }));
  $('members').addEventListener('change',safely(async e=>{
    const field=e.target.closest('[data-guest-role]');if(!field)return;
    field.disabled=true;
    try{await api(`/rooms/${state.room}/guests/${field.dataset.guestRole}`,{method:'PATCH',body:JSON.stringify({role:field.value})});await refresh();toast('成员权限已更新');}
    finally{field.disabled=false;}
  }));
  $('guest-profile-form').onsubmit=safely(async e=>{
    e.preventDefault();const edit=state.guestEditing;
    await api(`/rooms/${edit.room}/guests/${edit.id}`,{method:'PATCH',body:JSON.stringify({name:$('guest-name').value})});
    $('guest-profile-dialog').close();await refresh();toast('群聊名字已保存');
  });
}
