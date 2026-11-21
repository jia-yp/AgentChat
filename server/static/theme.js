/* Loaded synchronously in <head> so the chosen theme is set before first paint.
   Without a saved choice the page follows the system light/dark preference. */
(() => {
  const key='agentchat-theme',system=matchMedia('(prefers-color-scheme: dark)');
  let saved=null;
  try{saved=localStorage.getItem(key);}catch{}
  const current=()=>saved==='light'||saved==='cyber'?saved:(system.matches?'cyber':'light');
  function apply() {
    const theme=current();
    document.documentElement.dataset.theme=theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content',theme==='cyber'?'#05070d':'#fbfaf7');
    const button=document.getElementById('theme-toggle');
    if(button){const label=theme==='cyber'?'切换到浅色主题':'切换到赛博深色主题';button.title=label;button.setAttribute('aria-label',label);button.setAttribute('aria-pressed',String(theme==='cyber'));}
  }
  apply();
  system.addEventListener('change',()=>{if(!saved)apply();});
  document.addEventListener('DOMContentLoaded',()=>{
    apply();
    document.getElementById('theme-toggle')?.addEventListener('click',()=>{
      saved=current()==='cyber'?'light':'cyber';
      try{localStorage.setItem(key,saved);}catch{}
      apply();
    });
  });
})();
