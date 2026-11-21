'use strict';
const $ = id => document.getElementById(id);
let activeTab = 'overview', settings = null, tracks = [], dirtyZones = false, refreshBusy = false;
let cameraOffset = [0,0];
let reminderState = null, dirtyDeparture = false, savingDeparture = false;
let arrivalState = null, dirtyArrival = false, savingArrival = false;
const zoneColors = {outside_zone:'#ffb450',transition_zone:'#dcdc3c',inside_zone:'#8ee1bb',door_zone:'#82b8ff',arrival_zone:'#dc98e5'};
const names = {overview:'运行概览',camera:'摄像头与区域',events:'进出事件',announcements:'进出门播报',settings:'检测设置',logs:'运行日志'};
const url = path => new URL(path, document.baseURI).href;
async function api(path, options={}) {
  const response = await fetch(url('api/'+path), {cache:'no-store',...options});
  if (!response.ok) {
    let message = `${response.status} 请求失败`;
    try { const data = await response.json(); message = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail); } catch {}
    throw new Error(message);
  }
  return response.json();
}
function notice(text) { $('notice').textContent=text; $('notice').hidden=false; }
function switchTab(name) {
  for(const id of ['overview-image','camera-image'])stopImage($(id));
  activeTab=name; $('page-title').textContent=names[name];
  document.querySelectorAll('.tab').forEach(el=>el.classList.toggle('active',el.id===name));
  document.querySelectorAll('[data-tab]').forEach(el=>el.classList.toggle('selected',el.dataset.tab===name));
  refresh();
}
document.querySelectorAll('[data-tab]').forEach(el=>el.onclick=()=>switchTab(el.dataset.tab));
document.querySelectorAll('[data-jump]').forEach(el=>el.onclick=()=>switchTab(el.dataset.jump));
function node(tag, text, className) {const el=document.createElement(tag); if(text!==undefined)el.textContent=text; if(className)el.className=className; return el;}
function connection(label, value, good) {
  const row=node('div',undefined,'connection'); row.append(node('span',label),node('b',value,good?'good':'')); return row;
}
function renderStatus(s) {
  const labels={RUNNING:'运行正常',CAMERA_CONNECTING:'等待摄像头',DEGRADED:'部分功能未就绪',ERROR:'运行异常'};
  $('overall').textContent=labels[s.state]||s.state; $('overall').classList.toggle('good',s.state==='RUNNING');
  $('input-fps').textContent=s.input_fps.toFixed(1);$('detect-fps').textContent=s.detection_fps.toFixed(1);
  $('inference').textContent=s.inference_ms.toFixed(0);$('persons').textContent=s.persons;
  $('connections').replaceChildren(...[['摄像头',s.camera],['人体检测',s.ai_ready],['MQTT',s.mqtt],['Home Assistant',s.ha],['三区域',s.calibrated],['出门路线',s.departure_path_ready]].map(([label,good])=>connection(label,good?'已就绪':'未就绪',good)));
  $('frame-age').textContent=s.last_frame_age_ms===null?'暂无新画面':`帧龄 ${s.last_frame_age_ms} ms · 已丢弃旧帧 ${s.dropped_frames}`;
  $('calibration-status').textContent=[s.calibrated?'三区域已标定':'三区域未标定',s.departure_path_ready?'出门路线已启用':'出门路线未启用'].join(' · ');
  if(s.arrival_detection)renderDoorStatus(s.arrival_detection);
  if(activeTab==='overview'&&s.camera)loadImage($('overview-image'));
  if(activeTab==='camera'&&s.camera)loadImage($('camera-image'));
  if(!s.camera){stopImage($('overview-image'));stopImage($('camera-image'));}
}
const previews = new WeakMap();
function stopImage(img){previews.get(img)?.stop();}
function loadImage(img) {
  if(document.hidden)return;
  if(!previews.has(img)){
    const placeholder=img.parentElement.querySelector('.empty-camera');
    previews.set(img,new FramePreview(img,{
      streamUrl:url('api/stream'),snapshotUrl:url('api/snapshot'),
      onFrame:()=>{if(placeholder)placeholder.hidden=true;if(img.id==='camera-image'&&img.naturalWidth)img.parentElement.style.aspectRatio=`${img.naturalWidth}/${img.naturalHeight}`;},
      onStop:()=>{if(placeholder)placeholder.hidden=false;},
    }));
  }
  previews.get(img).start();
}
function timeLabel(timestamp) {return new Date(timestamp).toLocaleString('zh-CN',{hour12:false});}
function renderEvents(items) {
  $('event-list').replaceChildren();
  if(!items.length){$('event-list').append(node('p','暂无进出事件。完成区域标定后，完整穿越会记录在这里。','empty'));return;}
  const latest=items[0];const last=node('div',eventLabel(latest),'last-event');last.append(node('small',timeLabel(latest.timestamp)));$('last-event').replaceChildren(last);
  for(const item of items){
    const card=node('article',undefined,'event-card'); const link=node('a');link.href=url(`api/events/${item.event_id}/snapshot?raw=true`);link.target='_blank';link.rel='noopener';
    const img=node('img');img.src=url(`api/events/${item.event_id}/snapshot`);img.loading='lazy';img.alt='进出事件快照';link.append(img);
    const meta=node('div',undefined,'event-meta');meta.append(node('b',eventLabel(item)),node('small',`${timeLabel(item.timestamp)} · ${item.source==='simulation'?'手动模拟':`置信度 ${(item.confidence*100).toFixed(0)}%`}`));card.append(link,meta);$('event-list').append(card);
  }
}
function eventLabel(item){return item.source==='simulation'?(item.event_type==='enter'?'↗ 模拟进门':'↙ 模拟出门'):item.event_type==='enter'?(item.trigger==='door_reveal'?'↗ 开门后人员出现':'↗ 进入 ENTER'):(item.trigger==='departure_path'?'↙ 摄像头旁出门':'↙ 离开 EXIT');}
function renderDoorStatus(d){
  cameraOffset=d.camera_offset_px||[0,0];
  const doors={closed:'门关闭',open:'门打开',unknown:'不确定'};
  const phases={wait_closed:'等待确认关门',wait_open:'等待开门',wait_person:'等待人员出现',fired:'本轮已触发'};
  const c=d.calibration;
  $('door-state').replaceChildren(connection('当前观测',doors[d.observation]||'不确定',d.observation!=='unknown'),connection('进门阶段',phases[d.phase]||'等待标定',d.phase==='fired'),connection('参考画面',c.ready?'已就绪':`关门 ${c.closed_samples} / 开门 ${c.open_samples}`,c.ready));
  const alignment={aligned:'已对齐',held:'沿用最近对齐',pending:'正在核对',occluded:'背景被遮挡',unavailable:'固定背景不足',recalibrate:'请检查机位并重新标定'};
  if(d.alignment)$('door-state').append(connection('固定背景',alignment[d.alignment]||'正在核对',['aligned','held'].includes(d.alignment)));
  $('door-error').textContent=c.error||'';
  $('arrival-rule').textContent=d.mode==='door_reveal'?'开门后人员出现 · 每次开门一次':'当前仍使用三区域进门判定，请完成标定后切换';
}
function renderArrival(r){
  arrivalState=r;
  if(!dirtyArrival){$('arrival-enabled').checked=r.settings.enabled;$('arrival-text').value=r.settings.text;}
  $('save-arrival').disabled=savingArrival;$('arrival-unsaved').hidden=!dirtyArrival;
  $('arrival-audio-state').replaceChildren(connection(r.tts_provider==='qwen'?'阿里千问语音':'本地语音',r.tts_ready?'已生成':'未就绪',r.tts_ready),connection('ESP32 缓存',r.esp32_cached?'已就绪':'未就绪',r.esp32_cached));
  const last=r.last_arrival_playback,labels={requested:'等待播放',playing:'正在播放',finished:'播放成功',complete:'已完成',failed:'播放失败',timed_out:'等待回执超时',superseded:'已被新事务替代',fallback:'已请求内置欢迎音'};
  if(last)$('arrival-audio-state').append(connection('最近播放',labels[last.status]||last.status,last.status==='complete'));
  const preview=$('arrival-preview');
  if(r.tts_cache_version&&preview.dataset.version!==r.tts_cache_version){preview.src=url(`api/audio/${r.tts_cache_version}.wav?v=${r.last_sync}`);preview.dataset.version=r.tts_cache_version;}
  if(!r.tts_ready)resetPreview(preview);
  preview.hidden=!r.tts_ready;
  $('arrival-error').textContent=r.error||'';
}
function renderReminders(r) {
  reminderState=r;
  if(!dirtyDeparture){$('departure-enabled').checked=r.settings.enabled;$('departure-text').value=r.settings.text;$('departure-include-todos').checked=r.settings.include_todos;}
  $('save-departure').disabled=savingDeparture;$('departure-unsaved').hidden=!dirtyDeparture;
  $('departure-todos').hidden=!r.settings.include_todos;
  $('reminder-count').textContent=r.reminder_count+' 项';$('todo-list').replaceChildren(...r.items.map(item=>node('li',item.text)));
  if(!r.items.length)$('todo-list').append(node('p','目前没有未完成的出门提醒。','empty'));
  $('audio-state').replaceChildren(connection(r.tts_provider==='qwen'?'阿里千问语音':'本地语音',r.tts_ready?'已生成':'未就绪',r.tts_ready),connection('ESP32 缓存',r.esp32_cached?'已就绪':'未就绪',r.esp32_cached));
  const playbackLabels={requested:'等待播放',playing:'正在播放',finished:'播放成功，待提交',completing:'正在完成待办',complete:'已完成',failed:'播放失败',timed_out:'等待回执超时',superseded:'已被新事务替代',fallback:'固定告别音'};
  const last=r.last_reminder_playback;if(last)$('audio-state').append(connection('最近播放',playbackLabels[last.status]||last.status,last.status==='complete'));
  $('tts-text').textContent=r.text||'暂无需要合成的提醒。';$('reminder-error').textContent=r.error||'';
  const preview=$('audio-preview');
  if(r.tts_cache_version&&preview.dataset.version!==r.tts_cache_version){preview.src=url(`api/audio/${r.tts_cache_version}.wav?v=${r.last_sync}`);preview.dataset.version=r.tts_cache_version;}
  if(!r.tts_ready)resetPreview(preview);
  preview.hidden=!r.tts_ready;
}
function resetPreview(preview){preview.pause();preview.removeAttribute('src');delete preview.dataset.version;preview.load();preview.hidden=true;}
function drawDeparturePath(ctx,w,h){
  const points=settings.departure_path||[];if(!points.length)return;
  const [sx,sy]=points[0];ctx.strokeStyle='#ff7070';ctx.fillStyle='#ff7070';ctx.lineWidth=3;ctx.font='14px sans-serif';
  ctx.beginPath();ctx.arc(sx*w,sy*h,5,0,Math.PI*2);ctx.fill();ctx.fillText('起点 · 摄像头旁',Math.max(4,Math.min(w-110,sx*w+8)),Math.max(18,sy*h-10));
  if(points.length!==2)return;
  const [ex,ey]=points[1],dx=(ex-sx)*w,dy=(ey-sy)*h,angle=Math.atan2(dy,dx);
  const img=$('camera-image'),iw=img.naturalWidth||320,ih=img.naturalHeight||240;
  const vx=(ex-sx)*iw,vy=(ey-sy)*ih,length=Math.hypot(vx,vy),radius=settings.departure_path_tolerance*Math.hypot(iw,ih);
  const nx=-vy/length*radius/iw*w,ny=vx/length*radius/ih*h;
  ctx.fillStyle='#ff707018';ctx.beginPath();ctx.moveTo(sx*w+nx,sy*h+ny);ctx.lineTo(ex*w+nx,ey*h+ny);ctx.lineTo(ex*w-nx,ey*h-ny);ctx.lineTo(sx*w-nx,sy*h-ny);ctx.closePath();ctx.fill();
  ctx.beginPath();ctx.moveTo(sx*w,sy*h);ctx.lineTo(ex*w,ey*h);ctx.stroke();
  ctx.beginPath();ctx.moveTo(ex*w-14*Math.cos(angle-.5),ey*h-14*Math.sin(angle-.5));ctx.lineTo(ex*w,ey*h);ctx.lineTo(ex*w-14*Math.cos(angle+.5),ey*h-14*Math.sin(angle+.5));ctx.stroke();
  ctx.fillStyle='#ff7070';ctx.fillText('终点 · 门口',Math.max(4,Math.min(w-90,ex*w+8)),Math.max(18,ey*h-10));
}
function draw() {
  if(!settings)return;
  $('tracking-point-hint').textContent=settings.tracking_point==='center'?'当前判定点：人体框中心（适用于半身画面）':'当前判定点：人体框底边中心（适用于全身画面）';
  const canvas=$('zone-canvas'),ctx=canvas.getContext('2d'),w=canvas.width,h=canvas.height;ctx.clearRect(0,0,w,h);
  const image=$('camera-image'),iw=image.naturalWidth||320,ih=image.naturalHeight||240;
  ctx.save();ctx.translate(cameraOffset[0]*w/iw,cameraOffset[1]*h/ih);
  for(const [key,color] of Object.entries(zoneColors)){
    const points=settings[key];if(!points.length)continue;ctx.strokeStyle=color;ctx.fillStyle=color+'20';ctx.lineWidth=2;
    ctx.beginPath();points.forEach(([x,y],i)=>i?ctx.lineTo(x*w,y*h):ctx.moveTo(x*w,y*h));if(points.length>2){ctx.closePath();ctx.fill();}ctx.stroke();
    ctx.fillStyle=color;for(const [x,y]of points){ctx.beginPath();ctx.arc(x*w,y*h,4,0,Math.PI*2);ctx.fill();}
  }
  drawDeparturePath(ctx,w,h);
  ctx.restore();
  if(!$('show-tracks').checked)return;
  for(const track of tracks){const [x1,y1,x2,y2]=track.bbox;ctx.strokeStyle='#8ee1bb';ctx.strokeRect(x1/iw*w,y1/ih*h,(x2-x1)/iw*w,(y2-y1)/ih*h);ctx.fillStyle='#e7efee';ctx.font='14px monospace';ctx.fillText('#'+track.track_id,x1/iw*w,Math.max(16,y1/ih*h));ctx.beginPath();(track.history||[]).forEach(([x,y],i)=>i?ctx.lineTo(x*w,y*h):ctx.moveTo(x*w,y*h));ctx.stroke();
    if(track.point){const [x,y]=track.point;ctx.fillStyle=track.zone?'#dcdc3c':'#ffb450';ctx.beginPath();ctx.arc(x*w,y*h,5,0,Math.PI*2);ctx.fill();const label={inside:'室内',transition:'过渡',outside:'室外'}[track.zone]||'区域外';ctx.fillText(label,Math.min(w-54,Math.max(0,x*w+8)),Math.min(h-8,Math.max(16,y*h-8)));}
  }
}
$('zone-canvas').onclick=e=>{
  if(!settings)return;
  const rect=e.currentTarget.getBoundingClientRect(),key=$('zone-select').value,points=settings[key];
  if(points.length>=(key==='departure_path'?2:20))return notice(key==='departure_path'?'出门路线只需起点和终点；请撤销或清除后重画':'每个区域最多 20 个顶点');
  const image=$('camera-image');
  const x=(e.clientX-rect.left)/rect.width-cameraOffset[0]/(image.naturalWidth||320);
  const y=(e.clientY-rect.top)/rect.height-cameraOffset[1]/(image.naturalHeight||240);
  const point=[Math.max(0,Math.min(1,x)),Math.max(0,Math.min(1,y))];
  if(key==='departure_path'&&points.length&&Math.hypot(point[0]-points[0][0],point[1]-points[0][1])<.1)return notice('路线太短，请将终点画在门口');
  points.push(point);dirtyZones=true;draw();
};
$('zone-undo').onclick=()=>{if(settings){settings[$('zone-select').value].pop();dirtyZones=true;draw();}};
$('zone-clear').onclick=()=>{if(settings){settings[$('zone-select').value]=[];dirtyZones=true;draw();}};
$('show-tracks').onchange=draw;
$('zone-select').onchange=()=>{$('zone-draw-hint').textContent=$('zone-select').value==='departure_path'?'先点摄像头旁的起点，再点门口终点。沿人体判定点的实际路线画箭头；保存启用，清除并保存停用。':'点击画面添加区域顶点，至少三个点后保存。';};
async function saveSettings(){if(settings.departure_path.length===1)throw new Error('请再点门口终点，或清除出门路线后保存');settings=await api('settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(settings)});dirtyZones=false;notice('设置已保存并生效');draw();}
$('zones-save').onclick=()=>saveSettings().catch(e=>notice(e.message));
$('settings-form').onsubmit=e=>{e.preventDefault();if(!settings)return;for(const [key,value]of new FormData(e.target))settings[key]=['tracking_point','entry_detection_mode'].includes(key)?value:Number(value);saveSettings().catch(e=>notice(e.message));};
async function action(button,path){button.disabled=true;try{await api(path,{method:'POST'});notice('请求已提交，准备状态会自动更新。');}catch(e){notice(e.message);}finally{button.disabled=false;}}
$('sync-reminders').onclick=e=>dirtyDeparture?notice('请先保存出门提醒'):action(e.target,'reminders/sync');
$('regenerate').onclick=e=>{if(dirtyDeparture)return notice('请先保存出门提醒');resetPreview($('audio-preview'));action(e.target,'reminders/regenerate');};
$('arrival-form').oninput=()=>{dirtyArrival=true;$('arrival-unsaved').hidden=false;};
$('arrival-form').onsubmit=async e=>{
  e.preventDefault();if(!arrivalState)return;const button=$('save-arrival');savingArrival=true;button.disabled=true;
  const submitted={enabled:$('arrival-enabled').checked,text:$('arrival-text').value};
  try{const state=await api('arrival',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(submitted)});dirtyArrival=$('arrival-text').value!==submitted.text||$('arrival-enabled').checked!==submitted.enabled;renderArrival(state);notice(submitted.enabled?'欢迎语已保存，音频正在准备。':'欢迎语已保存，进门播报已关闭。');}catch(error){notice(error.message);}finally{savingArrival=false;button.disabled=false;}
};
$('departure-form').oninput=()=>{dirtyDeparture=true;$('departure-unsaved').hidden=false;};
function departureDraft(){return {enabled:$('departure-enabled').checked,text:$('departure-text').value,include_todos:$('departure-include-todos').checked};}
$('departure-form').onsubmit=async e=>{
  e.preventDefault();if(!reminderState)return;const button=$('save-departure');savingDeparture=true;button.disabled=true;
  const submitted=departureDraft();
  try{const state=await api('reminders',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(submitted)});dirtyDeparture=JSON.stringify(departureDraft())!==JSON.stringify(submitted);renderReminders(state);notice(submitted.enabled?'出门提醒已保存，音频正在准备。':'提醒语已保存，出门播报已关闭。');}catch(error){notice(error.message);}finally{savingDeparture=false;button.disabled=false;}
};
$('regenerate-arrival').onclick=e=>{if(dirtyArrival)return notice('请先保存欢迎语');resetPreview($('arrival-preview'));action(e.target,'arrival/regenerate');};
function eventId(){const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;}
$('simulate-arrival').onclick=async e=>{
  if(!arrivalState||dirtyArrival)return notice('请先保存欢迎语并等待状态加载');
  const button=e.target;button.disabled=true;
  try{await api('events/simulate-enter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:eventId(),expected_version:arrivalState.tts_cache_version})});notice('已发送模拟进门事件，等待 HA 自动化和播放回执。');}catch(error){notice(error.message);}finally{setTimeout(()=>{button.disabled=false;},10000);}
};
for(const state of ['closed','open'])$('capture-'+state).onclick=async e=>{
  if(dirtyZones)return notice('请先保存区域，再采集参考画面');
  const buttons=[$('capture-closed'),$('capture-open')];buttons.forEach(b=>b.disabled=true);
  notice('正在采集约一秒画面，请保持门的位置并让开门体观察区。');
  try{renderDoorStatus(await api('arrival/reference',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({state})}));notice('参考画面已保存。完成开关门采集后，可在检测设置中启用新规则。');}catch(error){notice(error.message);}finally{buttons.forEach(b=>b.disabled=false);}
};
$('simulate-departure').onclick=async e=>{
  if(!reminderState||dirtyDeparture)return notice('请先保存出门提醒并等待状态加载');
  const button=e.target;button.disabled=true;
  const event_id=eventId();
  try{
    await api('events/simulate-exit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id,expected_items:reminderState.items.map(item=>item.uid),expected_version:reminderState.tts_cache_version})});
    notice('已发送模拟出门事件，等待 HA 自动化和播放回执。');
  }catch(error){notice(error.message);}finally{setTimeout(()=>{button.disabled=false;},10000);}
};
$('refresh-events').onclick=()=>api('events').then(renderEvents).catch(e=>notice(e.message));
async function refresh(){
  if(refreshBusy||document.hidden)return;refreshBusy=true;
  try{
    renderStatus(await api('status'));
    if(!settings){settings=await api('settings');for(const input of $('settings-form').querySelectorAll('input,select'))input.value=settings[input.name];draw();}
    if(activeTab==='camera'){tracks=await api('tracks');draw();}
    if(activeTab==='overview'||activeTab==='events')renderEvents(await api('events'));
    if(activeTab==='announcements'){
      const results=await Promise.allSettled([api('arrival').then(renderArrival),api('reminders').then(renderReminders)]);
      for(const result of results)if(result.status==='rejected')notice(result.reason.message);
    }
    if(activeTab==='logs')$('log-lines').textContent=(await api('logs')).map(line=>`${timeLabel(line.time*1000)}  ${line.level.padEnd(7)} ${line.message}`).join('\n')||'暂无日志';
  }catch(e){$('overall').textContent='连接中断';$('overall').classList.remove('good');notice(e.message);}finally{refreshBusy=false;}
}
window.addEventListener('beforeunload',e=>{if(dirtyZones||dirtyArrival||dirtyDeparture){e.preventDefault();e.returnValue='';}});
document.addEventListener('visibilitychange',()=>{if(document.hidden){stopImage($('overview-image'));stopImage($('camera-image'));}else refresh();});
window.addEventListener('pagehide',()=>{stopImage($('overview-image'));stopImage($('camera-image'));});
refresh();setInterval(refresh,1500);
