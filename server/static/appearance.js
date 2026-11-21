const bubblePalette=[
  {name:'白',color:'#ffffff'}, {name:'香芋紫',color:'#eee5f8'},
  {name:'海盐蓝',color:'#e3effb'}, {name:'蜜桃橙',color:'#fae5d5'},
  {name:'草莓粉',color:'#f8e4eb'}, {name:'薄荷绿',color:'#e3f2e7'},
  {name:'奶油黄',color:'#faf1d4'}
];
const appearanceEdits=new Map(),appearanceQueues=new Map();
const validBubbleColor=value=>/^#[0-9a-f]{6}$/i.test(value);
/* Avatars: '' uses the harness icon for agents and initials for people;
   icon:<harness>, text:<1-2 chars> or a small data:image URL override it. */
const harnessIcons={codex:'Codex',claude:'Claude',antigravity:'Antigravity',deepseek:'DeepSeek'};
const avatarEdits=new Map();
function memberAvatar(member) {
  if(!member)return '';
  const key=`${state.room}:${member.id}`,edit=avatarEdits.get(key);
  if(edit&&!edit.saving&&member.avatar===edit.value)avatarEdits.delete(key);
  return edit?.value??member.avatar??'';
}
function avatarSpec(member,fallback={},override) {
  const value=override??(member?memberAvatar(member):'');
  if(value.startsWith('data:image/'))return {kind:'image',value};
  if(value.startsWith('icon:')&&harnessIcons[value.slice(5)])return {kind:'icon',value:value.slice(5)};
  if(value.startsWith('text:'))return {kind:'text',value:value.slice(5)};
  const kind=member?.kind||fallback.kind,harness=String(member?.harness||fallback.harness||'').toLowerCase();
  if(kind==='agent'&&harnessIcons[harness])return {kind:'icon',value:harness};
  return {kind:'initials',value:String(member?.name||fallback.name||'?').slice(0,2).toUpperCase()};
}
function avatarInner(spec) {
  if(spec.kind==='image')return `<img src="${esc(spec.value)}" alt="">`;
  if(spec.kind==='icon')return `<img src="/harness/${spec.value}.svg" alt="">`;
  return esc(spec.value);
}
function avatarClass(member,fallback,spec) {
  const kind=member?.kind||fallback.kind||'';
  return `avatar ${kind==='user'?'human':kind} avatar-${spec.kind}`;
}
function avatarHTML(member,fallback={}) {
  const spec=avatarSpec(member,fallback);
  return `<div class="${avatarClass(member,fallback,spec)}" aria-hidden="true">${avatarInner(spec)}</div>`;
}
function paintAvatar(element,member,fallback={},override) {
  if(!element)return;
  const spec=avatarSpec(member,fallback,override),className=avatarClass(member,fallback,spec),html=avatarInner(spec);
  if(element.className!==className)element.className=className;
  if(element.innerHTML!==html)element.innerHTML=html;
  element.setAttribute('aria-hidden','true');
}
function readAvatarFile(file) {
  if(!/^image\/(png|jpeg|webp|gif)$/.test(file?.type||''))return Promise.reject(new Error('请选择 PNG、JPEG、WebP 或 GIF 图片'));
  if(file.size>8*1024*1024)return Promise.reject(new Error('图片不能超过 8 MB'));
  return new Promise((resolve,reject)=>{
    const reader=new FileReader();
    reader.onerror=()=>reject(new Error('无法读取这张图片'));
    reader.onload=()=>{
      const img=new Image();
      img.onerror=()=>reject(new Error('无法读取这张图片'));
      img.onload=()=>{
        // Center-crop to a square and shrink so the room snapshot stays small.
        const size=96,canvas=document.createElement('canvas'),g=canvas.getContext('2d'),side=Math.min(img.naturalWidth,img.naturalHeight);
        canvas.width=canvas.height=size;g.fillStyle='#ffffff';g.fillRect(0,0,size,size);
        g.drawImage(img,(img.naturalWidth-side)/2,(img.naturalHeight-side)/2,side,side,0,0,size,size);
        for(const [type,quality] of [['image/webp',.86],['image/jpeg',.86],['image/png']]){const data=canvas.toDataURL(type,quality);if(data.startsWith(`data:${type};`)&&data.length<=24000)return resolve(data);}
        reject(new Error('图片压缩后仍然过大，请换一张'));
      };
      img.src=reader.result;
    };
    reader.readAsDataURL(file);
  });
}
function repaintMemberAvatar(id) {
  const member=state.snapshot?.members.find(m=>m.id===id);if(!member)return;
  for(const article of document.querySelectorAll(`.message[data-sender="${id}"]`))paintAvatar(article.querySelector(':scope>.avatar'),member);
  const card=[...$('members').children].find(el=>el.dataset.memberId===id);
  if(card){paintAvatar(card.querySelector('.member-top .avatar'),member);renderMemberColors(card,member);}
  if(member.id===state.snapshot?.viewer?.member_id)paintAvatar(document.querySelector('.user-row .avatar'),member);
}
function setMemberAvatar(id,value) {
  const room=state.room,key=`${room}:${id}`,member=state.snapshot?.members.find(m=>m.id===id);
  if(!member)return;
  const edit={value,saving:true};avatarEdits.set(key,edit);repaintMemberAvatar(id);
  post(`/rooms/${room}/members/${id}/avatar`,{avatar:value}).then(saved=>{
    if(avatarEdits.get(key)!==edit)return;
    edit.saving=false;
    if(state.room===room){const current=state.snapshot?.members.find(m=>m.id===id);if(current)current.avatar=saved.avatar;repaintMemberAvatar(id);toast(value?`${member.name} 的头像已保存`:`${member.name} 已恢复默认头像`);}
  }).catch(error=>{if(avatarEdits.get(key)===edit){avatarEdits.delete(key);if(state.room===room){repaintMemberAvatar(id);toast(error.message);}}});
}
function memberBubbleColor(member) {
  if(!member)return '';
  const edit=appearanceEdits.get(`${state.room}:${member.id}`);
  if(edit&&!edit.saving&&member.bubble_color===edit.value)appearanceEdits.delete(`${state.room}:${member.id}`);
  return edit?.value??member.bubble_color??'';
}
function bubbleLuminance(color) {
  return [1,3,5].map(i=>parseInt(color.slice(i,i+2),16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
}
function bubbleInk(color) {
  const bg=bubbleLuminance(color);
  return ['#3d5145','#ffffff','#000000'].find(ink=>{const light=bubbleLuminance(ink);return (Math.max(bg,light)+.05)/(Math.min(bg,light)+.05)>=4.5;});
}
function bubbleMix(base,ink,amount) {
  return '#'+[1,3,5].map(i=>Math.round(parseInt(base.slice(i,i+2),16)*(1-amount)+parseInt(ink.slice(i,i+2),16)*amount).toString(16).padStart(2,'0')).join('');
}
function applyBubbleColor(element,color) {
  if(!validBubbleColor(color)){delete element.dataset.bubbleColor;for(const key of ['bg','ink','border','inset'])element.style.removeProperty('--bubble-'+key);return;}
  const ink=bubbleInk(color);
  element.dataset.bubbleColor=color;
  for(const [key,value] of Object.entries({bg:color,ink,border:bubbleMix(color,ink,.22),inset:bubbleMix(color,ink==='#ffffff'?'#000000':'#ffffff',.12)}))element.style.setProperty('--bubble-'+key,value);
}
function renderMemberColors(card,member) {
  if(!['agent','user'].includes(member.kind))return;
  if(!canEditAppearance(member)){card.querySelector('.member-colors')?.remove();card.querySelector('.member-color-trigger')?.remove();card.classList.remove('has-color');return;}
  const agent=true;
  card.classList.add('has-color');
  let trigger=card.querySelector('.member-color-trigger');
  let palette=card.querySelector('.member-colors');
  if(palette&&palette.dataset.appearanceName!==member.name){closeMemberColors(palette);palette.remove();trigger.remove();palette=null;trigger=null;}
  if(!palette){
    trigger=document.createElement('button');trigger.type='button';trigger.className='member-color-trigger';trigger.innerHTML='<span></span>外观';trigger.setAttribute('aria-label',`设置 ${member.name} 的头像${agent?'和气泡颜色':''}`);trigger.setAttribute('aria-haspopup','dialog');trigger.setAttribute('aria-expanded','false');trigger.setAttribute('aria-controls',`member-colors-${member.id}`);
    palette=document.createElement('div');palette.className='member-colors';palette.id=`member-colors-${member.id}`;palette.dataset.colorMember=member.id;palette.setAttribute('role','dialog');palette.setAttribute('aria-label',`${member.name} 外观`);palette.setAttribute('popover','auto');palette.hidden=true;
    const avatarChoices=['',...Object.keys(harnessIcons).map(h=>'icon:'+h)].map(value=>`<button type="button" class="avatar-choice" data-avatar-choice="${value}" aria-pressed="false" title="${value?harnessIcons[value.slice(5)]+' 图标':'默认头像'}" aria-label="${esc(member.name)}：${value?harnessIcons[value.slice(5)]+' 图标':'默认头像'}"><span class="avatar"></span></button>`).join('');
    const colors=agent?`<span class="appearance-label">气泡颜色</span><div class="color-swatches">${bubblePalette.map(p=>`<button type="button" class="color-swatch" data-bubble-preset="${p.color}" title="${p.name}" aria-label="${esc(member.name)}：${p.name}" aria-pressed="false"><span>✓</span></button>`).join('')}</div><div class="custom-bubble-color"><label class="custom-color-label"><input type="color" data-custom-picker aria-label="${esc(member.name)} 自定义取色"><span>自定义</span></label><input class="color-hex" type="text" data-custom-hex aria-label="${esc(member.name)} HEX 颜色" maxlength="7" pattern="#[0-9a-fA-F]{6}" spellcheck="false" autocomplete="off" placeholder="#RRGGBB"></div><button type="button" class="text-button color-reset" data-bubble-preset="" aria-label="恢复 ${esc(member.name)} 默认颜色">恢复默认颜色</button>`:'';
    palette.innerHTML=`<div class="color-heading"><strong>${esc(member.name)} · 外观</strong><button type="button" class="color-close" aria-label="关闭外观设置">×</button></div><span class="appearance-label">头像</span><div class="avatar-choices">${avatarChoices}</div><div class="avatar-custom"><input class="avatar-text" type="text" data-avatar-text maxlength="2" placeholder="1–2 个字" aria-label="${esc(member.name)} 文字头像" autocomplete="off"><label class="secondary avatar-upload">上传图片<input type="file" accept="image/png,image/jpeg,image/webp,image/gif" data-avatar-file hidden></label></div>${colors}`;
    for(const button of palette.querySelectorAll('.color-swatch'))button.style.setProperty('--swatch',button.dataset.bubblePreset);
    palette.addEventListener('toggle',()=>trigger.setAttribute('aria-expanded',String(colorPaletteOpen(palette))));
    palette.dataset.appearanceName=member.name;
    card.appendChild(trigger);
    card.appendChild(palette);
  }
  const avatar=memberAvatar(member);
  for(const button of palette.querySelectorAll('[data-avatar-choice]')){
    const value=button.dataset.avatarChoice;button.setAttribute('aria-pressed',String(value===avatar));
    paintAvatar(button.firstElementChild,member,{},value);
  }
  const text=palette.querySelector('[data-avatar-text]');if(document.activeElement!==text)text.value=avatar.startsWith('text:')?avatar.slice(5):'';
  if(!agent){trigger.firstElementChild.hidden=true;trigger.title=`${member.name} 头像 · 点击调整`;return;}
  const color=memberBubbleColor(member);
  trigger.style.setProperty('--swatch',color||'#ffffff');
  trigger.title=`${member.name} 外观 · 气泡颜色：${bubblePalette.find(p=>p.color===color)?.name||color||'默认'} · 点击调整`;
  for(const button of palette.querySelectorAll('.color-swatch'))button.setAttribute('aria-pressed',String(button.dataset.bubblePreset===color));
  palette.querySelector('.color-reset').disabled=!color;
  const picker=palette.querySelector('[data-custom-picker]'),hex=palette.querySelector('[data-custom-hex]');
  if(document.activeElement!==picker)picker.value=color||'#ffffff';
  if(document.activeElement!==hex)hex.value=color;
  palette.querySelector('.custom-color-label').title=color||'默认颜色';
}
function colorPaletteOpen(palette) {return palette.showPopover?palette.matches(':popover-open'):!palette.hidden;}
function closeMemberColors(palette,focus=false) {
  if(!palette||!colorPaletteOpen(palette))return;
  if(palette.hidePopover)palette.hidePopover();
  palette.hidden=true;
  const trigger=palette.parentElement?.querySelector('.member-color-trigger');
  trigger?.setAttribute('aria-expanded','false');if(focus)trigger?.focus({preventScroll:true});
}
function toggleMemberColors(trigger) {
  const palette=trigger.parentElement.querySelector('.member-colors');
  if(colorPaletteOpen(palette)){closeMemberColors(palette,true);return;}
  for(const other of document.querySelectorAll('.member-colors'))closeMemberColors(other);
  palette.hidden=false;if(palette.showPopover)palette.showPopover();
  const rect=trigger.getBoundingClientRect(),width=palette.offsetWidth,height=palette.offsetHeight;
  palette.style.left=Math.max(12,Math.min(rect.right-width,innerWidth-width-12))+'px';
  palette.style.top=Math.max(12,Math.min(rect.bottom+8,innerHeight-height-12))+'px';
  trigger.setAttribute('aria-expanded','true');
  palette.querySelector('.avatar-choice[aria-pressed="true"],.avatar-choice')?.focus({preventScroll:true});
}
function repaintMemberColor(id) {
  const member=state.snapshot?.members.find(m=>m.id===id);if(!member)return;
  const color=memberBubbleColor(member);
  for(const article of document.querySelectorAll(`.message[data-sender="${id}"]`))applyBubbleColor(article,color);
  for(const summary of document.querySelectorAll(`.round-summary[data-color-sender="${id}"]`))applyBubbleColor(summary,color);
  const card=[...$('members').children].find(el=>el.dataset.memberId===id);if(card)renderMemberColors(card,member);
}
function setMemberColor(id,color,delay=0) {
  const room=state.room,key=`${room}:${id}`,member=state.snapshot?.members.find(m=>m.id===id);
  if(!member||!(color===''||validBubbleColor(color)))return;
  color=color.toLowerCase();clearTimeout(appearanceEdits.get(key)?.timer);
  const edit={value:color,saving:true};appearanceEdits.set(key,edit);repaintMemberColor(id);
  const save=()=>{
    const queued=(appearanceQueues.get(key)||Promise.resolve()).catch(()=>{}).then(()=>post(`/rooms/${room}/members/${id}/appearance`,{bubble_color:color}));
    appearanceQueues.set(key,queued);
    queued.then(saved=>{
      if(appearanceEdits.get(key)!==edit)return;
      edit.saving=false;
      if(state.room===room){const current=state.snapshot?.members.find(m=>m.id===id);if(current)current.bubble_color=saved.bubble_color;repaintMemberColor(id);toast(color?`${member.name} 的气泡颜色已保存`:`${member.name} 已恢复默认颜色`);}
    }).catch(error=>{if(appearanceEdits.get(key)===edit){appearanceEdits.delete(key);if(state.room===room){repaintMemberColor(id);toast(error.message);}}}).finally(()=>{if(appearanceQueues.get(key)===queued)appearanceQueues.delete(key);});
  };
  if(delay)edit.timer=setTimeout(save,delay);else save();
}
function initAppearance() {
  $('members').addEventListener('click',e=>{
    const trigger=e.target.closest('.member-color-trigger');if(trigger){toggleMemberColors(trigger);return;}
    const palette=e.target.closest('.member-colors'),button=e.target.closest('[data-bubble-preset]'),choice=e.target.closest('[data-avatar-choice]');
    if(choice){setMemberAvatar(palette.dataset.colorMember,choice.dataset.avatarChoice);return;}
    if(button){setMemberColor(palette.dataset.colorMember,button.dataset.bubblePreset);closeMemberColors(palette,true);}
    else if(e.target.closest('.color-close'))closeMemberColors(palette,true);
  });
  document.addEventListener('click',e=>{if(!e.target.closest('.member-colors,.member-color-trigger'))for(const palette of document.querySelectorAll('.member-colors'))closeMemberColors(palette);});
  // The palette is anchored in the roster; the message list scrolling on refresh does not move it.
  window.addEventListener('scroll',e=>{if(!e.target.closest?.('.member-colors,#messages'))for(const palette of document.querySelectorAll('.member-colors'))closeMemberColors(palette);},true);
  window.addEventListener('resize',()=>{for(const palette of document.querySelectorAll('.member-colors'))closeMemberColors(palette);});
  $('members').addEventListener('input',e=>{if(e.target.matches('[data-custom-picker],[data-custom-hex]')&&validBubbleColor(e.target.value))setMemberColor(e.target.closest('[data-color-member]').dataset.colorMember,e.target.value,450);});
  const avatarText=input=>{
    const value=input.value.trim(),id=input.closest('[data-color-member]').dataset.colorMember;
    if(!value||memberAvatar(state.snapshot?.members.find(m=>m.id===id))==='text:'+value)return;
    if([...value].length>2||/[<>&"']/.test(value)){toast('文字头像需为 1–2 个字符');return;}
    setMemberAvatar(id,'text:'+value);
  };
  $('members').addEventListener('change',e=>{
    if(e.target.matches('[data-avatar-text]')){avatarText(e.target);return;}
    if(e.target.matches('[data-avatar-file]')){
      const input=e.target,file=input.files[0],id=input.closest('[data-color-member]').dataset.colorMember;input.value='';
      if(file)readAvatarFile(file).then(data=>setMemberAvatar(id,data)).catch(error=>toast(error.message));
      return;
    }
    if(!e.target.matches('[data-custom-picker],[data-custom-hex]'))return;
    if(validBubbleColor(e.target.value))setMemberColor(e.target.closest('[data-color-member]').dataset.colorMember,e.target.value);
    else if(e.target.value.trim())toast('请输入完整 HEX 色值，例如 #B8A1D9');
  });
  $('members').addEventListener('keydown',e=>{
    if(e.key==='Escape'&&e.target.closest('.member-colors')){e.preventDefault();closeMemberColors(e.target.closest('.member-colors'),true);return;}
    if(e.key==='Enter'&&e.target.matches('[data-avatar-text]')){e.preventDefault();avatarText(e.target);return;}
    if(e.key==='Enter'&&e.target.matches('[data-custom-hex]')){e.preventDefault();if(validBubbleColor(e.target.value))setMemberColor(e.target.closest('[data-color-member]').dataset.colorMember,e.target.value);else toast('请输入完整 HEX 色值，例如 #B8A1D9');}
  });
}
