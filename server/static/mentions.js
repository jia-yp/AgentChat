/* Completion only edits the body; To remains the source of routing decisions. */
function mentionRange(text,start,end=start) {
  if(start!==end)return null;
  const match=text.slice(0,start).match(/@([a-z0-9_\-\u3400-\u4dbf\u4e00-\u9fff]{0,40})$/i);
  if(!match)return null;
  const at=start-match[0].length;
  if(at&&/[a-z0-9_.%+@\-]/i.test(text[at-1]))return null;
  const tail=text.slice(start).match(/^[a-z0-9_\-\u3400-\u4dbf\u4e00-\u9fff]*/i)[0];
  return {start:at,end:start+tail.length,query:match[1]};
}
function mentionCandidates(members,query) {
  const choices=[{name:'all',label:'所有人',detail:'全部公开角色'},...members.filter(m=>!m.revoked&&!m.deleted_at&&['agent','user'].includes(m.kind)).map(m=>({name:m.name,label:m.name,detail:m.kind==='user'?'管理员':[m.harness,m.session_type==='manager'?'manager':'',m.paused?'已关闭':''].filter(Boolean).join(' · ')}))];
  const needle=query.toLowerCase();
  return choices.filter(c=>`${c.name} ${c.label}`.toLowerCase().includes(needle));
}
let mentionMenu,mentionOptions,mentionInput,mentionItems=[],mentionIndex=0,mentionComposing=false;
function closeMentions() {
  if(!mentionMenu)return;
  if(mentionMenu.hidePopover&&mentionMenu.matches(':popover-open'))mentionMenu.hidePopover();
  mentionMenu.hidden=true;mentionInput.setAttribute('aria-expanded','false');mentionInput.removeAttribute('aria-activedescendant');mentionItems=[];
}
function positionMentions() {
  if(!mentionMenu||mentionMenu.hidden)return;
  const rect=mentionInput.getBoundingClientRect(),viewport=window.visualViewport;
  const left=viewport?.offsetLeft||0,top=viewport?.offsetTop||0,width=viewport?.width||innerWidth,height=viewport?.height||innerHeight;
  mentionMenu.style.width=Math.min(350,rect.width,width-24)+'px';
  mentionMenu.style.maxHeight=Math.max(90,Math.min(300,height-24))+'px';
  mentionMenu.style.left=Math.max(left+12,Math.min(rect.left,left+width-mentionMenu.offsetWidth-12))+'px';
  mentionMenu.style.top=Math.max(top+12,Math.min(rect.top-mentionMenu.offsetHeight-8,top+height-mentionMenu.offsetHeight-12))+'px';
}
function selectMentionOption(index) {
  mentionIndex=index;
  for(const [i,button] of [...mentionOptions.children].entries())button.setAttribute('aria-selected',String(i===index));
  const selected=mentionOptions.children[index];
  if(!selected){mentionInput.removeAttribute('aria-activedescendant');return;}
  mentionInput.setAttribute('aria-activedescendant',selected.id);
  const top=selected.offsetTop;
  if(top<mentionOptions.scrollTop)mentionOptions.scrollTop=top;
  else if(top+selected.offsetHeight>mentionOptions.scrollTop+mentionOptions.clientHeight)mentionOptions.scrollTop=top+selected.offsetHeight-mentionOptions.clientHeight;
}
function updateMentions() {
  if(mentionComposing||!state.room||state.view!=='chat'||document.activeElement!==mentionInput){closeMentions();return;}
  const range=mentionRange(mentionInput.value,mentionInput.selectionStart,mentionInput.selectionEnd);
  if(!range){closeMentions();return;}
  const prior=mentionItems[mentionIndex]?.name,items=mentionCandidates(state.snapshot?.members||[],range.query);
  if(!items.length){closeMentions();return;}
  mentionItems=items;mentionOptions.innerHTML=items.map((item,i)=>`<button type="button" role="option" id="mention-option-${i}" data-mention-option="${i}" aria-selected="false"><strong>@${esc(item.label)}</strong><small>${esc(item.detail)}</small></button>`).join('');
  mentionMenu.hidden=false;if(mentionMenu.showPopover&&!mentionMenu.matches(':popover-open'))mentionMenu.showPopover();
  mentionInput.setAttribute('aria-expanded','true');positionMentions();selectMentionOption(Math.max(0,items.findIndex(item=>item.name===prior)));
}
function acceptMention(index) {
  const item=mentionItems[index],range=mentionRange(mentionInput.value,mentionInput.selectionStart,mentionInput.selectionEnd);
  if(!item||!range){closeMentions();return;}
  if(!mentionCandidates(state.snapshot?.members||[],range.query).some(c=>c.name===item.name)){updateMentions();return;}
  const end=range.end+(mentionInput.value[range.end]===' '?1:0),text='@'+item.name+' ';
  if(mentionInput.maxLength>0&&mentionInput.value.length-(end-range.start)+text.length>mentionInput.maxLength){toast('消息已达到长度上限');closeMentions();return;}
  mentionInput.setRangeText(text,range.start,end,'end');closeMentions();mentionInput.focus({preventScroll:true});
  mentionInput.dispatchEvent(new Event('input',{bubbles:true}));
}
function initMentions() {
  mentionInput=$('message-input');mentionMenu=document.createElement('div');mentionMenu.id='mention-menu';mentionMenu.className='mention-menu';mentionMenu.hidden=true;mentionMenu.setAttribute('popover','manual');
  mentionMenu.innerHTML='<div class="mention-heading">提及角色</div><div id="mention-options" class="mention-options" role="listbox" aria-label="角色建议"></div><div class="mention-hint">↑↓ 选择 · Enter 补全 · To 决定收件人</div>';
  document.body.appendChild(mentionMenu);mentionOptions=$('mention-options');
  mentionInput.setAttribute('aria-autocomplete','list');mentionInput.setAttribute('aria-controls','mention-options');mentionInput.setAttribute('aria-expanded','false');
  mentionInput.addEventListener('input',e=>{if(!e.isComposing)updateMentions();});
  mentionInput.addEventListener('click',updateMentions);
  mentionInput.addEventListener('keyup',e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key))updateMentions();});
  mentionInput.addEventListener('compositionstart',()=>{mentionComposing=true;closeMentions();});
  mentionInput.addEventListener('compositionend',()=>{mentionComposing=false;updateMentions();});
  mentionInput.addEventListener('keydown',e=>{
    if(mentionMenu.hidden||mentionComposing||e.isComposing||e.keyCode===229||e.ctrlKey||e.metaKey||e.altKey)return;
    if(['ArrowDown','ArrowUp','Escape'].includes(e.key)||(!e.shiftKey&&['Enter','Tab'].includes(e.key))){
      e.preventDefault();e.stopImmediatePropagation();
      if(e.key==='Escape')closeMentions();
      else if(e.key==='ArrowDown'||e.key==='ArrowUp')selectMentionOption((mentionIndex+(e.key==='ArrowDown'?1:-1)+mentionItems.length)%mentionItems.length);
      else if(!e.repeat)acceptMention(mentionIndex);
    }else if(e.key==='Enter')closeMentions();
  },true);
  mentionMenu.addEventListener('mousedown',e=>e.preventDefault());
  mentionMenu.addEventListener('click',e=>{const button=e.target.closest('[data-mention-option]');if(button)acceptMention(Number(button.dataset.mentionOption));});
  mentionInput.addEventListener('blur',()=>setTimeout(()=>{if(!mentionMenu.contains(document.activeElement))closeMentions();},0));
  document.addEventListener('pointerdown',e=>{if(e.target!==mentionInput&&!mentionMenu.contains(e.target))closeMentions();});
  window.addEventListener('resize',positionMentions);window.visualViewport?.addEventListener('resize',positionMentions);window.visualViewport?.addEventListener('scroll',positionMentions);
  window.addEventListener('scroll',e=>{if(!mentionMenu.contains(e.target))positionMentions();},true);
}
