const bubblePalette=[
  {name:'白',color:'#ffffff'}, {name:'香芋紫',color:'#eee5f8'},
  {name:'海盐蓝',color:'#e3effb'}, {name:'蜜桃橙',color:'#fae5d5'},
  {name:'草莓粉',color:'#f8e4eb'}, {name:'薄荷绿',color:'#e3f2e7'},
  {name:'奶油黄',color:'#faf1d4'}
];
const appearanceEdits=new Map(),appearanceQueues=new Map();
const validBubbleColor=value=>/^#[0-9a-f]{6}$/i.test(value);
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
  if(member.kind!=='agent')return;
  card.classList.add('has-color');
  let trigger=card.querySelector('.member-color-trigger');
  let palette=card.querySelector('.member-colors');
  if(!palette){
    trigger=document.createElement('button');trigger.type='button';trigger.className='member-color-trigger';trigger.innerHTML='<span></span>';trigger.setAttribute('aria-label',`设置 ${member.name} 气泡颜色`);trigger.setAttribute('aria-haspopup','dialog');trigger.setAttribute('aria-expanded','false');trigger.setAttribute('aria-controls',`member-colors-${member.id}`);
    palette=document.createElement('div');palette.className='member-colors';palette.id=`member-colors-${member.id}`;palette.dataset.colorMember=member.id;palette.setAttribute('role','dialog');palette.setAttribute('aria-label',`${member.name} 气泡颜色`);palette.setAttribute('popover','auto');palette.hidden=true;
    palette.innerHTML=`<div class="color-heading"><strong>${esc(member.name)} · 气泡颜色</strong><button type="button" class="color-close" aria-label="关闭调色盘">×</button></div><div class="color-swatches">${bubblePalette.map(p=>`<button type="button" class="color-swatch" data-bubble-preset="${p.color}" title="${p.name}" aria-label="${esc(member.name)}：${p.name}" aria-pressed="false"><span>✓</span></button>`).join('')}</div><div class="custom-bubble-color"><label class="custom-color-label"><input type="color" data-custom-picker aria-label="${esc(member.name)} 自定义取色"><span>自定义</span></label><input class="color-hex" type="text" data-custom-hex aria-label="${esc(member.name)} HEX 颜色" maxlength="7" pattern="#[0-9a-fA-F]{6}" spellcheck="false" autocomplete="off" placeholder="#RRGGBB"></div><button type="button" class="text-button color-reset" data-bubble-preset="" aria-label="恢复 ${esc(member.name)} 默认颜色">恢复默认</button>`;
    for(const button of palette.querySelectorAll('.color-swatch'))button.style.setProperty('--swatch',button.dataset.bubblePreset);
    palette.addEventListener('toggle',()=>trigger.setAttribute('aria-expanded',String(colorPaletteOpen(palette))));
    card.appendChild(trigger);
    card.appendChild(palette);
  }
  const color=memberBubbleColor(member);
  trigger.style.setProperty('--swatch',color||'#ffffff');
  trigger.title=`${member.name} 气泡颜色：${bubblePalette.find(p=>p.color===color)?.name||color||'默认'} · 点击调整`;
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
  (palette.querySelector('.color-swatch[aria-pressed="true"]')||palette.querySelector('.color-swatch')).focus({preventScroll:true});
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
    const palette=e.target.closest('.member-colors'),button=e.target.closest('[data-bubble-preset]');
    if(button){setMemberColor(palette.dataset.colorMember,button.dataset.bubblePreset);closeMemberColors(palette,true);}
    else if(e.target.closest('.color-close'))closeMemberColors(palette,true);
  });
  document.addEventListener('click',e=>{if(!e.target.closest('.member-colors,.member-color-trigger'))for(const palette of document.querySelectorAll('.member-colors'))closeMemberColors(palette);});
  window.addEventListener('scroll',e=>{if(!e.target.closest?.('.member-colors'))for(const palette of document.querySelectorAll('.member-colors'))closeMemberColors(palette);},true);
  window.addEventListener('resize',()=>{for(const palette of document.querySelectorAll('.member-colors'))closeMemberColors(palette);});
  $('members').addEventListener('input',e=>{if(e.target.matches('[data-custom-picker],[data-custom-hex]')&&validBubbleColor(e.target.value))setMemberColor(e.target.closest('[data-color-member]').dataset.colorMember,e.target.value,450);});
  $('members').addEventListener('change',e=>{
    if(!e.target.matches('[data-custom-picker],[data-custom-hex]'))return;
    if(validBubbleColor(e.target.value))setMemberColor(e.target.closest('[data-color-member]').dataset.colorMember,e.target.value);
    else if(e.target.value.trim())toast('请输入完整 HEX 色值，例如 #B8A1D9');
  });
  $('members').addEventListener('keydown',e=>{
    if(e.key==='Escape'&&e.target.closest('.member-colors')){e.preventDefault();closeMemberColors(e.target.closest('.member-colors'),true);return;}
    if(e.key==='Enter'&&e.target.matches('[data-custom-hex]')){e.preventDefault();if(validBubbleColor(e.target.value))setMemberColor(e.target.closest('[data-color-member]').dataset.colorMember,e.target.value);else toast('请输入完整 HEX 色值，例如 #B8A1D9');}
  });
}
