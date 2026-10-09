'use strict';
const $ = (s) => document.querySelector(s);
const csrf = $('meta[name="csrf-token"]').content;
let state, photos = [], dirty = false, replacing = -1, pendingGroup = null, expanded = false;
let noticeTimer;
let draftRevision = 0, groupsDirty = false;
const escapeHTML = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function notice(message, error = false) {
  clearTimeout(noticeTimer); $('#notice').textContent = message;
  $('#notice').className = error ? 'error' : ''; $('#notice').hidden = false;
  noticeTimer = setTimeout(() => $('#notice').hidden = true, error ? 12000 : 5000);
}
async function api(path, method = 'GET', data) {
  const options = {method, headers: {'X-CSRF-Token': csrf}};
  if (data instanceof FormData) options.body = data;
  else if (data !== undefined) {options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(data);}
  let response;
  try {response = await fetch('/api/' + path, options);} catch {throw new Error('Нет связи с приложением. Проверь, что бот запущен.');}
  if(response.status===401){window.location.assign('/login');throw new Error('Сессия завершена. Войди снова.');}
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Не удалось выполнить действие');
  return result;
}
function action(selector, fn) {
  $(selector).addEventListener('click', async event => {
    const button = event.currentTarget; button.disabled = true;
    try {await fn();} catch (e) {notice(e.message, true);} finally {button.disabled = false;}
  });
}
function tab(name) {
  document.querySelectorAll('.tab').forEach(el => el.hidden = el.id !== name);
  document.querySelectorAll('.nav').forEach(el => el.classList.toggle('active', el.dataset.tab === name));
  $('#breadcrumb').textContent = ({editor:'Объявление',groups:'Группы и расписание',history:'Журнал отправок',settings:'Аккаунт VK'})[name];
}
document.querySelectorAll('.nav').forEach(b => b.onclick = () => tab(b.dataset.tab));
function connectVK(){window.open('/vk-id','_blank','noopener');}
$('#accountBadge').onclick = connectVK;
const vkConnectionChannel=new BroadcastChannel('vk-connection');
vkConnectionChannel.onmessage=async()=>{try{await refresh(false,!groupsDirty);notice('VK подключён.');}catch(e){notice(e.message,true);}};
function changed() {dirty = true; $('#saveStatus').textContent = 'Есть изменения'; preview();}
function preview() {
  const text = $('#postText').value;
  $('#charCount').textContent = `${text.length.toLocaleString('ru')} / 15 000`;
  $('#previewText').textContent = text || 'Здесь появится текст твоего объявления.';
  $('#previewText').classList.toggle('expanded', expanded);
  $('#previewPhotos').innerHTML = photos.map(p => `<img src="/photos/${p}" alt="Фото объявления">`).join('');
  $('#previewPhotos').classList.toggle('carousel',$('#photoLayout').value==='carousel');
  $('#photoCount').textContent = `${photos.length} / 10`;
}
function renderPhotos() {
  $('#photos').innerHTML = photos.map((p,i) => `<div class="photo-tile"><img src="/photos/${p}" alt="Фото ${i+1}">${i===0?'<span class="cover-label">ОБЛОЖКА</span>':''}<div class="photo-controls"><button data-photo="left" data-index="${i}" title="Переместить влево" ${i===0?'disabled':''}>←</button><button data-photo="replace" data-index="${i}" title="Заменить фото">↻</button><button data-photo="delete" data-index="${i}" title="Убрать фото">×</button><button data-photo="right" data-index="${i}" title="Переместить вправо" ${i===photos.length-1?'disabled':''}>→</button></div></div>`).join('');
  if (!photos.length) $('#photos').innerHTML = '<div class="empty">Добавь фотографии</div>';
  preview();
}
$('#photos').onclick = e => {
  const b = e.target.closest('[data-photo]'); if (!b) return;
  const i = Number(b.dataset.index), op = b.dataset.photo;
  if (op === 'replace') {replacing = i; $('#replaceInput').click(); return;}
  if (op === 'delete') photos.splice(i,1);
  else {const j = i + (op === 'left' ? -1 : 1); [photos[i],photos[j]] = [photos[j],photos[i]];}
  changed(); renderPhotos();
};
$('#addPhotos').onclick = () => $('#photoInput').click();
async function upload(files, replace = -1) {
  if (replace < 0 && files.length + photos.length > 10) throw new Error('В объявлении может быть до 10 фото.');
  $('#saveDraft').disabled = true; $('#addPhotos').disabled = true;
  try {
    for (const file of files) {
      if (file.size > 15*1024*1024) throw new Error('Фото должно быть меньше 15 МБ.');
      const data = new FormData(); data.append('photo',file);
      const result = await api('photos','POST',data);
      if (replace >= 0) photos[replace] = result.name; else photos.push(result.name);
      changed(); renderPhotos();
    }
    notice('Фото загружены. Сохрани изменения.');
  } finally {$('#saveDraft').disabled = false; $('#addPhotos').disabled = false;}
}
for (const id of ['photoInput','replaceInput']) $(`#${id}`).onchange = async e => {
  try {await upload([...e.target.files], id==='replaceInput'?replacing:-1);} catch(err){notice(err.message,true);} finally {e.target.value='';}
};
$('#postText').addEventListener('input', changed);
$('#photoLayout').addEventListener('change',changed);
$('#expandPreview').onclick = () => {expanded = !expanded; $('#expandPreview').textContent = expanded?'Свернуть':'Показать полностью'; preview();};
async function refresh(initial = false, renderGroups = true) {
  state = await api('state');
  if (initial) {draftRevision = state.draft_revision; $('#postText').value = state.draft.text; $('#photoLayout').value=state.draft.photo_layout||'grid'; photos = [...state.draft.photos]; renderPhotos();}
  $('#accountBadge').textContent = state.vkid ? 'VK подключён' : 'Подключить VK ID';
  $('#accountBadge').classList.toggle('connected', !!state.vkid);
  automation();
  $('#previewAuthor').innerHTML = `${escapeHTML(state.account?.name || 'Твой профиль VK')}<small>Новая запись в сообществе</small>`;
  $('#vkStatus').textContent = state.vkid ? `Права: ${state.vkid.scope}. Подключение продлевается автоматически при обращении к API.` : 'Подключи VK ID и разреши доступ к стене и фотографиям.';
  $('#navCount').textContent = state.groups.length;
  if (renderGroups) groups(); history();
}
action('#saveDraft', async () => {
  // Revision prevents a stale browser tab from overwriting newer content.
  const result = await api('draft','PUT',{text:$('#postText').value, photos, photo_layout:$('#photoLayout').value, revision:draftRevision});
  draftRevision = result.draft_revision;
  dirty = false; $('#saveStatus').textContent = 'Сохранено'; await refresh(); notice('Объявление сохранено');
});
function localDate(ts) {
  const date = new Date(ts*1000); return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);
}
const displayDate = ts => new Date(ts*1000).toLocaleString('ru-RU',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});
const modes = {api_suggest:'Предложить новость через API'};
function tasks() {} // Historical manual tasks remain in stored data only.
function automation(){
  const a=state.automation || {};
  $('#automationStatus').textContent=a.running?'Запущена':'Остановлена';
  $('#automationStatus').classList.toggle('connected',!!a.running);
  $('#automationDetail').textContent=`Расписаний: ${a.schedules||0} · Проверок публикации: ${a.publication_checks||0}`+(a.busy?' · Выполняется операция':a.paused?' · Общая остановка включена':'');
  $('#pauseAll').textContent=a.running?'Остановить автоматику':'Запустить автоматику';
  $('#pauseAll').disabled=!a.running&&!a.schedules&&!a.publication_checks;
}
function groups() {
  groupsDirty = false;
  $('#scheduleSummary').textContent = `${state.groups.length} групп · ${state.groups.filter(g=>g.enabled).length} расписаний включено`;
  $('#timezoneLabel').textContent = 'Время: ' + Intl.DateTimeFormat().resolvedOptions().timeZone;
  tasks();
  $('#groupList').innerHTML = state.groups.length ? state.groups.map(g => {
    const supported=g.mode==='api_suggest';
    const pending=state.history.some(h=>h.group_id===g.id&&['pending','error'].includes(h.publication?.state));
    const note=!supported?'Старый способ отправки отключён. Выбери «Предложить новость» и сохрани настройки.':pending?'Ожидается публикация предыдущей новости. Повторные отправки заблокированы.':g.api_error||g.api_warning|| (g.api_checked?'Права API проверены. Возможность предложения подтвердится при первой отправке.':'Группа ещё не проверена. Проверка выполнится при сохранении включённого расписания.');
    return `<article class="card group-card" data-id="${g.id}"><div class="group-top"><div><div class="group-name">${escapeHTML(g.name)}</div><a class="group-url" href="${escapeHTML(g.url)}" target="_blank" rel="noreferrer">${escapeHTML(g.url)} ↗</a></div><button data-op="delete" class="danger" title="Удалить группу">×</button></div><div class="group-fields"><label>Название в панели<input name="display-name" maxlength="100" value="${escapeHTML(g.name)}"></label><label>Способ размещения<span>Предложить новость через API</span></label><label>Интервал, часов<input name="hours" type="number" min="1" max="8760" step="0.5" value="${g.interval_hours}"></label><label>Следующая отправка<input name="next" type="datetime-local" value="${localDate(g.next_at)}"></label><label class="toggle"><input type="checkbox" name="enabled" ${g.enabled&&supported?'checked':''}> Расписание включено</label></div><div class="group-actions"><button data-op="rename">Переименовать</button><button data-op="save" class="primary">Сохранить настройки</button><button data-op="api-check">Проверить доступ</button><button data-op="send" ${state.busy.length||pending||!supported||!g.api_checked?'disabled':''}>Отправить автоматически ↗</button></div><div class="group-note">${escapeHTML(note)}</div></article>`;
  }).join('') : '<div class="card empty">Пока нет групп.<br>Вставь первую ссылку выше.</div>';
}
action('#addGroup', async () => {
  const added=await api('groups','POST',{url:$('#groupUrl').value});$('#groupUrl').value='';await refresh();
  notice('Группа добавлена. Проверяю доступ к группе…');
  try{const result=await api(`groups/${added.id}/check`,'POST',{});notice(result.message || 'Доступ к группе проверен.');}
  finally{await refresh();}
});
action('#pauseAll',async()=>{await api('automation','POST',{running:!state.automation.running});await refresh();notice(state.automation.running?'Автоматика запущена.':'Автоматика остановлена. Начатая операция завершится.');});
async function saveGroup(card, checkOnly=false){
  const id=card.dataset.id;
  if(checkOnly){
    const result=await api(`groups/${id}/check`,'POST',{});
    await refresh();notice(result.message || 'Права API проверены.');return;
  }
  const payload={interval_hours:Number(card.querySelector('[name=hours]').value),next_at:new Date(card.querySelector('[name=next]').value).getTime()/1000,enabled:card.querySelector('[name=enabled]').checked,mode:'api_suggest'};
  if(payload.mode!=='api_suggest')throw new Error('Выбери «Предложить новость через API».');
  if(dirty&&(payload.enabled||checkOnly))throw new Error('Сначала сохрани изменения объявления.');
  const saved=state.groups.find(g=>g.id===id);
  const restoreEnabled=payload.enabled&&(!checkOnly||(saved?.enabled&&saved.mode===payload.mode));
  if(checkOnly||payload.enabled){
    await api(`groups/${id}`,'PUT',{...payload,enabled:false});
    notice('Проверяю права VK и доступ к группе…');
    try{
      const result=await api(`groups/${id}/check`,'POST',{});
      if(restoreEnabled)await api(`groups/${id}`,'PUT',{...payload,next_at:Math.max(payload.next_at,Date.now()/1000+5)});
      notice(checkOnly?(result.message || 'Доступ к группе проверен.'):'Группа проверена, расписание сохранено.');
    }finally{await refresh();}
  }else{await api(`groups/${id}`,'PUT',payload);notice('Расписание сохранено');}
}
$('#groupList').addEventListener('input', () => {groupsDirty = true;});
$('#groupList').onclick = async e => {
  const b=e.target.closest('[data-op]'); if(!b)return;
  const card=b.closest('[data-id]'), id=card.dataset.id, op=b.dataset.op;
  b.disabled=true;
  try {
    if(op==='rename'){await api(`groups/${id}/name`,'PUT',{name:card.querySelector('[name=display-name]').value});notice('Название в панели изменено.');}
    else if(op==='save') {await saveGroup(card);}
    else if(op==='api-check'){await saveGroup(card,true);}
    else if(op==='delete'){if(!confirm('Удалить группу из списка и остановить её расписание?'))return; await api(`groups/${id}`,'DELETE');}
    else if(op==='send') {
      if(dirty) throw new Error('Сначала сохрани изменения объявления.');
      pendingGroup=id; $('#confirmText').textContent='Группа: '+state.groups.find(g=>g.id===id).name;
      $('#confirmDialog').showModal();return;
    }
    await refresh();
  }catch(err){notice(err.message,true);}finally{b.disabled=false;}
};
$('#cancelSend').onclick=()=>$('#confirmDialog').close();
action('#confirmSend',async()=>{await api(`groups/${pendingGroup}/send`,'POST',{});$('#confirmDialog').close();await refresh();tab('history');notice('Отправка началась. Результат появится в журнале.');});
function history(){
  const labels={deferred:'Отложена',published:'Опубликована',browser_posted:'Опубликовано автоматически',api_suggested:'Принято VK — ожидается публикация',browser_suggested:'На модерации',manual_posted:'Размещено вручную',manual_suggested:'На модерации — со слов пользователя',cancelled:'Отменено',sent:'Принято VK',sending:'Отправляется',error:'Ошибка',unknown:'Нужна проверка'};
  $('#historyList').innerHTML=state.history.length?state.history.map(h=>`<div class="history-item" data-history="${h.id}"><div class="history-time">${displayDate(h.time)}</div><div><b>${escapeHTML(h.group_name)}</b><span class="status ${h.status}">${h.status==='api_suggested'&&h.publication?.suggested===false?'Предложена — результат уточняется':labels[h.status]}</span><p>${escapeHTML(h.detail)}</p>${h.url&&h.publication?.state!=='found'?`<a href="${escapeHTML(h.url)}" target="_blank" rel="noreferrer">Открыть запись ↗</a>`:''}${publicationView(h)}</div></div>`).join(''):'<div class="empty">Здесь появятся результаты отправок.<br>Пока бот ничего не публиковал.</div>';
}
function publicationView(h){
  const p=h.publication;
  if(!p)return ['api_suggested','browser_suggested','manual_suggested','unknown'].includes(h.status)?'<div class="publication"><p>У этой старой отправки нет сохранённого текста для сравнения.</p><button data-publication="start">Искать по текущему объявлению</button></div>':'';
  const active=['pending','error'].includes(p.state);
  const label=p.state==='pending'&&p.suggested?'На модерации':({queued:'Проверка после отправки',pending:'Публикация пока не найдена',error:'Не удалось проверить стену',found:'Опубликована',stopped:'Проверка остановлена'})[p.state];
  return `<div class="publication ${p.state}"><strong>${label}</strong><p>${escapeHTML(p.detail)}</p>${p.source==='current_draft'?'<p>Поиск по объявлению, выбранному после отправки; исходный текст этой попытки не сохранён.</p>':''}<small>${p.checked_at?'Последняя проверка: '+displayDate(p.checked_at):p.state==='stopped'?'':'Стена ещё не проверялась'}${active?' · Следующая: '+displayDate(p.next_check):''}</small>${p.state==='pending'&&p.suggested?`<p><a href="${escapeHTML(p.suggestion_url)}" target="_blank" rel="noopener noreferrer">Открыть предложенные новости ↗</a></p>`:''}${p.state==='found'?`<p><a href="${escapeHTML(p.url)}" target="_blank" rel="noopener noreferrer">Открыть найденную публикацию ↗</a></p>`:''}${active?`<div class="group-actions"><button data-publication="check" ${state.busy.length?'disabled':''}>Проверить сейчас</button><button data-publication="stop" ${state.busy.length?'disabled':''}>Остановить проверку</button></div><p>Пока идёт поиск, повторные отправки в эту группу заблокированы.</p>`:''}</div>`;
}
$('#historyList').onclick=async e=>{
  const b=e.target.closest('[data-publication]');if(!b)return;
  const id=b.closest('[data-history]').dataset.history,op=b.dataset.publication;
  b.disabled=true;
  try{
    if(op==='start'){
      if(dirty)throw new Error('Сначала сохрани объявление.');
      if(!confirm('Искать по текущему сохранённому тексту и числу фото? Исходная версия старой отправки неизвестна.'))return;
    }
    if(op==='stop'&&!confirm('Остановить поиск публикации? Расписание этой группы также будет выключено.'))return;
    await api(`history/${id}/publication/${op}`,'POST',op==='start'?{use_current_draft:true}:{});
    await refresh();notice(op==='check'?'Проверяю стену. Результат появится в журнале.':op==='start'?'Поиск включён. Результат появится в журнале.':'Проверка остановлена.');
  }catch(err){notice(err.message,true);}finally{b.disabled=false;}
};
action('#refreshHistory',async()=>{await refresh();notice('Журнал обновлён');});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
refresh(true).catch(e=>notice(e.message,true));
// Poll activity without replacing unsaved group fields or the draft revision.
setInterval(async()=>{
  if(!state || document.hidden)return;
  try{const live=await api('state');state.vkid=live.vkid;$('#accountBadge').textContent=state.vkid?'VK подключён':'Подключить VK ID';$('#accountBadge').classList.toggle('connected',!!state.vkid);state.history=live.history;state.busy=live.busy;state.tasks=live.tasks;state.automation=live.automation;automation();history();tasks();
    if(!groupsDirty && !$('#groups').hidden){state.groups=live.groups;groups();}
  }
  catch{/* An explicit action reports connection errors. */}
},30000);

action('#connectVKID', async()=>{connectVK();});

