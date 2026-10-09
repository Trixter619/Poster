import RFB from '/desktop-client/core/rfb.js';
const csrf=document.querySelector('meta[name="csrf-token"]').content;
const view=document.querySelector('#desktopViewport'), status=document.querySelector('#desktopStatus');
const retry=document.querySelector('#desktopRetry'), check=document.querySelector('#desktopCheck');
let rfb=null, busy=false, leaving=false, polling=false, connected=false, prepared=false, verifying=false, verifyStarted=0;
async function api(action){
 const r=await fetch('/api/browser/'+action,{method:'POST',headers:{'X-CSRF-Token':csrf}});
 if(r.status===401){location.assign('/login');throw Error('Войди в кабинет.');}
 const data=await r.json();if(!r.ok)throw Error(data.error||'Не удалось подключить браузер.');return data;
}
function disconnect(){connected=false;if(rfb){const old=rfb;rfb=null;old.disconnect();}view.replaceChildren();}
async function start(initial=false){
 if(busy||leaving)return;busy=true;retry.hidden=true;status.textContent='Подключаю браузер…';
 try{
  disconnect();if(initial||!prepared){await api('connect');prepared=true;}await api('desktop');
  const client=new RFB(view,`${location.protocol==='https:'?'wss':'ws'}://${location.host}/desktop-socket`);rfb=client;
  client.scaleViewport=true;client.resizeSession=false;client.showDotCursor=true;client.qualityLevel=7;client.compressionLevel=3;
  client.addEventListener('connect',()=>{if(rfb!==client)return;connected=true;check.disabled=false;status.textContent='Браузер подключён. Войди в VK.';});
  client.addEventListener('disconnect',()=>{if(rfb!==client||leaving)return;connected=false;check.disabled=true;status.textContent='Связь с экраном прервалась. Вход VK не очищен.';retry.hidden=false;});
  client.addEventListener('securityfailure',()=>{status.textContent='Не удалось открыть личный экран. Перезайди в кабинет.';retry.hidden=false;});
 }catch(e){status.textContent=e.message;retry.hidden=false;}finally{busy=false;}
}
async function handleResult(result){
 status.textContent=result.message;
 if(result.state==='connected'){
  leaving=true;disconnect();const ch=new BroadcastChannel('vk-connection');ch.postMessage('connected');ch.close();location.replace('/');return;
 }
 if(['manual','session_rejected','stalled','error'].includes(result.state)||Date.now()-verifyStarted>45000){
  verifying=false;
  if(result.state==='waiting')status.textContent='Проверка не завершилась. Браузер оставлен открытым; вход не очищен.';
  await api('desktop-release');
 }
}
async function poll(){
 if(polling||busy||leaving||!connected)return;polling=true;
 try{
  if(verifying)await handleResult(await api('connect-status'));
  else await api('desktop-heartbeat');
 }catch(e){status.textContent=e.message;retry.hidden=false;}finally{polling=false;}
}
retry.onclick=()=>start();
check.onclick=async()=>{
 if(busy||polling||leaving||!connected)return;
 busy=true;verifying=true;verifyStarted=Date.now();check.disabled=true;
 status.textContent='Проверяю сохранённый вход. Страница VK один раз обновится…';
 try{await handleResult(await api('desktop-verify'));}
 catch(e){verifying=false;status.textContent=e.message;try{await api('desktop-release');}catch{}}
 finally{busy=false;if(connected)check.disabled=false;}
};
document.querySelector('#desktopReset').onclick=async()=>{
 if(busy||leaving||!window.confirm('Очистить вход VK только этого оператора и войти заново? Объявления и настройки сохранятся.'))return;
 busy=true;verifying=false;disconnect();try{await api('reconnect');prepared=true;}catch(e){status.textContent=e.message;retry.hidden=false;return;}finally{busy=false;}await start();
};
document.querySelector('#desktopBack').onclick=async e=>{e.preventDefault();leaving=true;disconnect();try{await api('desktop-close');}finally{location.assign('/');}};
document.querySelector('#desktopFullscreen').onclick=()=>view.requestFullscreen?.();
const keyboard=document.querySelector('#desktopKeyboardInput');
document.querySelector('#desktopKeyboard').onclick=()=>keyboard.focus();
keyboard.addEventListener('input',()=>{for(const c of keyboard.value){const n=c.codePointAt(0);rfb?.sendKey(n<=255?n:0x01000000+n);}keyboard.value='';});
keyboard.addEventListener('keydown',e=>{const key={Enter:0xff0d,Backspace:0xff08,Tab:0xff09}[e.key];if(key){e.preventDefault();rfb?.sendKey(key);}});
window.addEventListener('pagehide',()=>{disconnect();fetch('/api/browser/desktop-close',{method:'POST',headers:{'X-CSRF-Token':csrf},keepalive:true}).catch(()=>{});});
setInterval(poll,3000);start(true);
