 'use strict';
const csrf=document.querySelector('meta[name="csrf-token"]').content;
const image=document.querySelector('#remoteImage'), view=document.querySelector('#remoteViewport'), status=document.querySelector('#remoteStatus'), retry=document.querySelector('#remoteRetry');
let rejectedSession=false;
let opened=false, polling=false, starting=false, leaving=false, imageURL=null;
let events=[], sending=false, sendTimer=null, frameTimer=null, lastInput=0;
async function request(action,data={}){
 const response=await fetch('/api/browser/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(data)});
 if(response.status===401){location.assign('/login');throw new Error('Войди в кабинет заново.');}
 if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(error.error||'Не удалось подключиться. Попробуй ещё раз.');}
 return response;
}
function error(e){status.textContent=e.message;retry.hidden=false;}
function input(event){
 if(!opened||leaving)return;
 lastInput=performance.now();
 const previous=events.at(-1);
 if(event.kind==='text'&&previous?.kind==='text'&&previous.text.length+event.text.length<=2000)previous.text+=event.text;
 else if(event.kind==='scroll'&&previous?.kind==='scroll')previous.delta=Math.max(-2000,Math.min(2000,previous.delta+event.delta));
 else events.push(event);
 if(!sending&&!sendTimer)sendTimer=setTimeout(flush,40);
}
async function flush(){
 clearTimeout(sendTimer);sendTimer=null;
 if(sending||!opened||leaving||!events.length)return;
 sending=true;const batch=events.splice(0,64);
 try{await request('input-batch',{events:batch});}
 catch(e){if(opened&&!leaving){events=[];error(e);}}
 finally{sending=false;if(events.length&&opened&&!leaving)sendTimer=setTimeout(flush,0);}
}
async function frames(){
 if(!opened||leaving)return;
 try{
  if(!document.hidden){
   const response=await request('screen');const blob=await response.blob();
   if(!opened||leaving)return;
   const url=URL.createObjectURL(blob);const decoded=new Image();decoded.src=url;
   try{await decoded.decode();}catch{URL.revokeObjectURL(url);return;}
   if(!opened||leaving){URL.revokeObjectURL(url);return;}
   const previous=imageURL;imageURL=url;image.src=url;image.hidden=false;
   if(previous)URL.revokeObjectURL(previous);
  }
 }catch(e){if(opened&&!leaving)error(e);}
 finally{if(opened&&!leaving)frameTimer=setTimeout(frames,document.hidden?1000:(performance.now()-lastInput<2500?100:350));}
}
async function poll(){
 if(!opened||polling||leaving)return;
 polling=true;
 try{
  const result=await (await request('connect-status')).json();
  if(leaving)return;
  status.textContent=result.message;
  if(result.state==='connected'){
   opened=false;events=[];image.hidden=true;clearTimeout(frameTimer);clearTimeout(sendTimer);
   document.querySelector('#remoteText').value='';
   if(imageURL){URL.revokeObjectURL(imageURL);imageURL=null;}
   const channel=new BroadcastChannel('vk-connection');channel.postMessage('connected');channel.close();
   if(new URLSearchParams(location.search).get('return')==='close')window.close();
   location.replace('/');return;
  }
  rejectedSession=result.state==='session_rejected';
  retry.textContent=rejectedSession?'Войти заново':'Перезагрузить VK';
  retry.hidden=!['stalled','error','session_rejected'].includes(result.state);
  // Keep the screen and input alive while VK is loading or needs attention.
 }catch(e){if(!leaving)error(e);}finally{polling=false;}
}
async function connect(action='connect'){
 if(starting||polling||leaving)return;
 starting=true;retry.hidden=true;clearTimeout(frameTimer);status.textContent='Подключаю VK…';
 try{await request(action);opened=true;frames();await poll();}catch(e){error(e);}finally{starting=false;}
}
retry.onclick=()=>{
 if(rejectedSession){
  if(window.confirm('Очистить сохранённый вход VK только этого оператора и войти заново? Объявления, фотографии и настройки сохранятся.'))connect('reconnect');
 }else connect();
};
document.querySelector('#remoteBack').onclick=async e=>{
 e.preventDefault();leaving=true;opened=false;events=[];clearTimeout(frameTimer);clearTimeout(sendTimer);
 try{await request('close');}catch{}finally{location.assign('/');}
};
image.onclick=e=>{if(!image.naturalWidth||!image.naturalHeight)return;const r=image.getBoundingClientRect();view.focus();input({kind:'click',x:(e.clientX-r.left)*image.naturalWidth/r.width,y:(e.clientY-r.top)*image.naturalHeight/r.height});};
view.addEventListener('keydown',e=>{
 if(!opened)return;
 let key=e.key;
 if((e.ctrlKey||e.metaKey)&&key.toLowerCase()==='v')return;
 if((e.ctrlKey||e.metaKey)&&key.toLowerCase()==='a'){e.preventDefault();input({kind:'key',key:'Control+A'});return;}
 if(e.ctrlKey||e.metaKey||e.altKey)return;
 if(key.length===1){e.preventDefault();input({kind:'text',text:key});}
 else if(['Enter','Tab','Backspace','Delete','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End','PageUp','PageDown'].includes(key)){e.preventDefault();input({kind:'key',key:key==='Tab'&&e.shiftKey?'Shift+Tab':key});}
});
view.addEventListener('paste',e=>{e.preventDefault();const text=e.clipboardData.getData('text');if(text.length<=2000)input({kind:'text',text});else status.textContent='Можно вставить не больше 2000 символов.';});
view.addEventListener('wheel',e=>{if(opened){e.preventDefault();input({kind:'scroll',delta:Math.max(-2000,Math.min(2000,e.deltaY))});}},{passive:false});
document.querySelector('#remoteType').onclick=()=>{const field=document.querySelector('#remoteText');const text=field.value;field.value='';input({kind:'text',text});};
document.querySelector('#remoteEnter').onclick=()=>input({kind:'key',key:'Enter'});
document.querySelector('#remoteBackspace').onclick=()=>input({kind:'key',key:'Backspace'});


setInterval(poll,1000);
connect();
