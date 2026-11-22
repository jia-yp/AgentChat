/* Keep phone chrome out of the reading/editing area without moving its footer. */
(() => {
  const byId=id=>document.getElementById(id),media=matchMedia('(max-width:800px)');
  const box=byId('messages'),input=byId('message-input'),composer=byId('composer');
  const toggle=byId('mobile-room-toggle'),options=byId('mobile-composer-options');
  const description=byId('round-mode-description'),details=byId('round-details'),budget=byId('round-budget-controls');
  let frame=0,gesture=null;
  function fitInput() {
    // Desktop grows from two lines up to a third of the window; phones stay tighter.
    const view=window.visualViewport?.height||innerHeight,mobile=media.matches;
    const floor=mobile?40:52,limit=mobile?Math.max(40,Math.min(128,view*.25)):Math.max(floor,Math.min(320,view*.33));
    // Measuring at zero briefly expands the message viewport and clamps scrollTop.
    // Restore its reading position before a refresh can yield to a network request.
    preserveReading(()=>{
      input.style.height='0px';input.style.height=Math.min(limit,Math.max(floor,input.scrollHeight))+'px';
    });
  }
  function preserveReading(change) {
    const bottom=box.scrollHeight-box.clientHeight-box.scrollTop<12;
    const edge=box.getBoundingClientRect().top;
    const anchor=[...box.querySelectorAll('.message,.round-header')].find(el=>el.getBoundingClientRect().height&&el.getBoundingClientRect().bottom>edge);
    const offset=anchor?anchor.getBoundingClientRect().top-edge:0;
    change();
    // Preserve the same paragraph, or stay at the newest message if already there.
    if(bottom)box.scrollTop=box.scrollHeight;
    else if(anchor)box.scrollTop+=anchor.getBoundingClientRect().top-box.getBoundingClientRect().top-offset;
  }
  function setFocused(focused) {
    focused=!!focused&&media.matches&&!byId('workspace').hidden;
    if(document.body.classList.contains('mobile-chat-focus')===focused)return;
    preserveReading(()=>{
      document.body.classList.toggle('mobile-chat-focus',focused);
      toggle.setAttribute('aria-expanded',String(!focused));
      toggle.setAttribute('aria-label',focused?'展开顶部栏':'收起顶部栏');
      toggle.title=focused?'展开顶部栏':'收起顶部栏';toggle.firstElementChild.textContent=focused?'⌄':'⌃';
    });
  }
  function update() {
    const mobile=media.matches,manager=byId('round-mode').hidden,sync=byId('round-mode').value==='synchronous';
    composer.classList.toggle('private-composer',manager);
    options.hidden=manager;
    if(manager)options.open=false;
    const descriptionParent=mobile?byId('mobile-options-content'):document.querySelector('.send-options');
    if(description.parentElement!==descriptionParent){if(mobile)descriptionParent.prepend(description);else descriptionParent.append(description);}
    const detailsParent=mobile&&sync?byId('mobile-options-content'):budget;
    if(details.parentElement!==detailsParent){if(detailsParent===budget)budget.prepend(details);else detailsParent.append(details);}
    if(!mobile){setFocused(false);options.open=false;}
    fitInput();
  }
  function viewport() {
    cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>{
      const view=window.visualViewport;
      if(media.matches&&(!view||Math.abs(view.scale-1)<.05))preserveReading(()=>{
        document.documentElement.style.setProperty('--chat-height',(view?.height||innerHeight)+'px');
        document.documentElement.style.setProperty('--chat-top',(view?.offsetTop||0)+'px');
        fitInput();
      });else if(!media.matches){
        document.documentElement.style.removeProperty('--chat-height');document.documentElement.style.removeProperty('--chat-top');
        fitInput();
      }
    });
  }
  toggle.addEventListener('click',()=>setFocused(!document.body.classList.contains('mobile-chat-focus')));
  input.addEventListener('focus',()=>setFocused(true));input.addEventListener('input',fitInput);box.addEventListener('wheel',()=>setFocused(true),{passive:true});
  box.addEventListener('pointerdown',e=>{gesture={interactive:!!e.target.closest('button,input,select,a,label,summary')};},{passive:true});
  box.addEventListener('pointerup',()=>{
    if(gesture&&!gesture.interactive&&!window.getSelection()?.toString())setFocused(true);
    gesture=null;
  },{passive:true});
  box.addEventListener('pointercancel',()=>{if(gesture&&!gesture.interactive)setFocused(true);gesture=null;},{passive:true});
  document.addEventListener('pointerdown',e=>{if(options.open&&!options.contains(e.target))options.open=false;},{passive:true});
  document.addEventListener('keydown',e=>{if(e.key==='Escape')options.open=false;});
  media.addEventListener('change',()=>{update();viewport();});
  window.addEventListener('resize',viewport);window.visualViewport?.addEventListener('resize',viewport);window.visualViewport?.addEventListener('scroll',viewport);
  window.AgentChatMobile={update,fitInput,reset(){options.open=false;setFocused(false);fitInput();}};
  update();viewport();
})();
