const $ = id => document.getElementById(id);
let state = {}, socket, context, stream, capture, sink;
let listening = false, active = false, muted = false, testing = false, wakeHits = 0;
let nextPlayback = 0, sessionStart = 0, lastActivitySent = 0, lastTranscriptAt = 0;
let lastRole, lastParagraph, recording = null, polling = false;
let phase = 'off';
const audioSources = new Set(), sourceUrls = new Set();
// Browser and server monotonic clocks have different origins. Never subtract them.
const captureTimings = {};
let startupTiming = null;
function beginStartupTiming(trigger) {
  startupTiming = {trigger, server: null, browser: {clock:'performance.now_ms', marks:{...captureTimings}}};
  startupTiming.browser.marks.activation_observed = performance.now();
  window.r2StartupTiming = startupTiming; // Only the latest session, no audio or credentials.
}
function startupMark(name, at = performance.now(), trace = startupTiming) {
  if (trace && trace.browser.marks[name] === undefined) trace.browser.marks[name] = at;
}

function notice(message) { $('notice').textContent = message; $('notice').hidden = !message; }
function command(type, data = {}) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type, ...data})); }
function setState(name, description) {
  phase = name;
  const titles = {off: 'Ready when you are', idle: testing ? 'Testing “Hey R2”' : 'Say “Hey R2”', connecting:'One moment…', listening:'I’m listening', speaking:'R2 is speaking', working:'On it', muted:'Microphone muted'};
  $('state-title').textContent = titles[name] || name;
  $('state-description').textContent = description || '';
  $('orb-wrap').className = 'orb-wrap ' + (active ? 'active ' : '') + name;
  $('connection-label').textContent = active ? 'LIVE CONVERSATION' : listening ? (testing ? 'LOCAL TEST · NO CLOUD AUDIO' : 'LOCAL WAKE-WORD LISTENING') : 'LOCAL COMPANION';
}
function buttons() {
  $('start').textContent = listening ? '◉   Stop Listening' : '◉   Start Listening';
  $('mute').disabled = !listening;
  $('mute').textContent = muted ? 'Unmute microphone' : 'Mute microphone';
  $('end').disabled = !active;
  $('talk').disabled = active || testing;
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-R2-CSRF':state.csrf},body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'Request failed');
  return result;
}
async function refresh() {
  state = await api('/api/status');
  $('openai-status').textContent = state.openai_configured ? 'Connected · key saved in Windows Credential Manager' : 'Add an API key to start voice conversations';
  $('ha-status').textContent = state.ha_configured ? `Connected · ${state.allowed_count} lights · ${state.allowed_lock_count || 0} doors` : 'Connect your Home Assistant Green';
  $('ha-url').value = state.ha_url;
  $('threshold').value = state.wake_threshold;
  $('threshold-value').textContent = Number(state.wake_threshold).toFixed(2);
  $('wake-validated').checked = state.wake_validated;
  $('wake-badge').textContent = state.wake_ready ? (state.wake_validated ? 'HEY R2 · READY' : 'HEY R2 · NEEDS MIC TEST') : 'HEY R2 · TRAINING REQUIRED';
  $('wake-status').textContent = state.training.running ? state.training.message : state.wake_ready ? (state.wake_validated ? 'Model ready and microphone test confirmed.' : 'Model trained. Test detection here with your microphone before relying on hands-free control.') : 'Custom model not installed yet. Train it locally below; Talk is available as soon as your API key is connected.';
  $('train').disabled = state.training.running;
  if (!state.openai_configured) notice('One connection to finish: add your OpenAI API key in Settings.');
}
async function refreshLights() {
  await refreshLocks();
  try {
    const {lights} = await api('/api/lights');
    if (!lights.length) return;
    $('rooms').replaceChildren();
    $('light-count').textContent = `${lights.length} LIGHTS`;
    const groups = Object.groupBy(lights, x => x.room);
    const icons = {'Bedroom':'☾', 'Fireplace Room':'♨', 'Living Room':'☼', 'Laundry Room':'◈'};
    for (const [room, entries] of Object.entries(groups)) {
      const card = document.createElement('article'); card.className = 'room';
      const top = document.createElement('div'); top.className = 'room-icon';
      const icon = document.createElement('span'); icon.textContent = icons[room] || '⌂';
      const count = document.createElement('span'); count.className = 'room-count'; count.textContent = `${entries.length} ${entries.length === 1 ? 'LIGHT' : 'LIGHTS'}`;
      top.append(icon, count);
      const h = document.createElement('h3'); h.textContent = room;
      const status = document.createElement('p'); const on = entries.filter(x => x.state === 'on').length;
      const unavailable = entries.filter(x => ['unavailable','unknown'].includes(x.state)).length;
      status.textContent = `${on} on · ${entries.length - on - unavailable} off` + (unavailable ? ` · ${unavailable} unavailable` : '');
      const names = document.createElement('p'); names.className = 'lamp-names'; names.textContent = entries.map(x => x.name).join(' · ');
      card.append(top, h, status, names); $('rooms').append(card);
    }
  } catch (error) { $('ha-status').textContent = error.message; }
}
async function refreshLocks() {
  try {
    const {locks} = await api('/api/locks');
    $('doors').replaceChildren();
    for (const lock of locks) {
      const card = document.createElement('article'); card.className = 'room';
      const name = document.createElement('h3'); name.textContent = lock.name;
      const status = document.createElement('p'); status.textContent = `Lock: ${lock.state}`;
      card.append(name, status); $('doors').append(card);
    }
    $('doors-section').hidden = !locks.length;
  } catch (error) {
    // Never leave an old lock state looking current after a failed refresh.
    $('doors').replaceChildren();
    $('doors-section').hidden = false;
    const message = document.createElement('p'); message.textContent = 'Door lock status unavailable';
    $('doors').append(message);
  }
}
async function loadLockSettings() {
  $('save-locks').disabled = true;
  $('lock-options').replaceChildren();
  $('lock-save-result').textContent = 'Reading door locks…';
  try {
    const {locks} = await api('/api/locks?discover=true');
    for (const [index, lock] of locks.entries()) {
      const row = document.createElement('div'); row.className = 'lock-option'; row.dataset.entityId = lock.entity_id;
      const label = document.createElement('label'); label.className = 'checkbox';
      const enabled = document.createElement('input'); enabled.type = 'checkbox'; enabled.checked = lock.enabled;
      const description = document.createElement('span'); description.textContent = `${lock.device_name || lock.name}${lock.serial_suffix ? ' · ' + lock.serial_suffix : ''} · ${lock.state}`;
      label.append(enabled, description);
      const nameLabel = document.createElement('label'); nameLabel.htmlFor = `door-name-${index}`; nameLabel.textContent = 'Name to use with R2';
      const name = document.createElement('input'); name.id = nameLabel.htmlFor; name.type = 'text'; name.maxLength = 80; name.value = lock.name;
      row.append(label, nameLabel, name); $('lock-options').append(row);
    }
    $('lock-save-result').textContent = locks.length ? '' : 'No door locks found. Connect them in Home Assistant first.';
    $('save-locks').disabled = !state.ha_configured;
  } catch (error) { $('lock-save-result').textContent = error.message; }
}
async function openSettings() {
  $('settings-dialog').showModal();
  await loadLockSettings();
}
function appendTranscript(role, delta) {
  if (!delta) return;
  $('transcript').querySelector('.empty-conversation')?.remove();
  if (role !== lastRole || Date.now() - lastTranscriptAt > 3500) {
    const article = document.createElement('div'); article.className = `utterance ${role}`;
    const label = document.createElement('strong'); label.textContent = role === 'user' ? 'YOU' : 'R2';
    lastParagraph = document.createElement('p'); article.append(label,lastParagraph); $('transcript').append(article);
    lastRole = role;
  }
  lastParagraph.textContent += delta; lastTranscriptAt = Date.now();
  $('transcript').scrollTop = $('transcript').scrollHeight;
}
function showTool(event) {
  $('transcript').querySelector('.empty-conversation')?.remove();
  const el = document.createElement('div'); el.className = 'tool-entry';
  if (event.name === 'web_search') el.textContent = event.result.status === 'completed' ? '↗ Web search completed' : `↗ Web search: ${event.result.status}`;
  else if (event.name === 'set_lock' && event.result.status === 'confirmed') el.textContent = `✓ ${event.result.confirmed.join(', ')} · ${event.result.already_in_state ? 'already ' : ''}${event.result.state}`;
  else if (event.result.confirmed?.length) el.textContent = `✓ ${event.result.confirmed.join(', ')} · ${event.result.power}` + (event.result.brightness_pct != null ? ` · ${event.result.brightness_pct}%` : '') + (event.result.failed?.length ? ' · Some lights were not confirmed' : '');
  else if (event.result.message) el.textContent = event.result.message;
  else if (event.name === 'get_locks') el.textContent = event.result.locks?.length ? event.result.locks.map(lock => `${lock.name}: ${lock.state}`).join(' · ') : 'No doors enabled for voice control';
  else el.textContent = event.name === 'get_lights' ? '✓ Checked light status' : `Device action: ${event.result.status}`;
  $('transcript').append(el); lastRole = null;
  $('transcript').scrollTop = $('transcript').scrollHeight;
  refreshLights();
}
function showSources(sources) {
  for (const source of sources) {
    let url; try { url = new URL(source.url); } catch { continue; }
    if (!['http:','https:'].includes(url.protocol) || sourceUrls.has(url.href)) continue;
    sourceUrls.add(url.href);
    const a = document.createElement('a'); a.href = url.href; a.textContent = `${source.title || url.hostname} ↗`; a.target = '_blank'; a.rel = 'noopener noreferrer';
    $('source-links').append(a); $('sources').hidden = false;
  }
}
function stopPlayback() {
  for (const source of audioSources) { try { source.stop(); } catch {} }
  audioSources.clear(); nextPlayback = context?.currentTime || 0;
}
function playAudio(base64) {
  if (!context || !active) return;
  const raw = atob(base64), data = new DataView(new ArrayBuffer(raw.length));
  for (let i=0;i<raw.length;i++) data.setUint8(i,raw.charCodeAt(i));
  const buffer = context.createBuffer(1,raw.length/2,16000), samples = buffer.getChannelData(0);
  for (let i=0;i<samples.length;i++) samples[i] = data.getInt16(i*2,true)/32768;
  const source = context.createBufferSource(); source.buffer = buffer; source.connect(context.destination);
  nextPlayback = Math.max(nextPlayback,context.currentTime+0.025);
  if (nextPlayback-context.currentTime > 8) { command('stop'); stopPlayback(); notice('Audio playback fell behind. Please start a new conversation.'); return; }
  const scheduledAt = nextPlayback, trace = startupTiming;
  source.start(scheduledAt); nextPlayback += buffer.duration;
  const first = trace && trace.browser.marks.first_assistant_audio_scheduled === undefined;
  if (first) {
    startupMark('first_assistant_audio_scheduled');
    // Device-output estimate, not proof of acoustic sound. No timer delays playback.
    const audibleIndex = samples.findIndex(sample => Math.abs(sample) > 160 / 32768);
    const audibleAt = scheduledAt + Math.max(0, audibleIndex) / 16000;
    const stamp = context.getOutputTimestamp?.();
    const estimated = stamp?.performanceTime > 0
      ? stamp.performanceTime + (audibleAt - stamp.contextTime) * 1000
      : performance.now() + (audibleAt - context.currentTime + (context.baseLatency || 0) + (context.outputLatency || 0)) * 1000;
    if (context.state === 'running' && audibleIndex >= 0) startupMark('first_audible_output_estimated', estimated);
    console.debug('R2 startup timing (output time is estimated)', trace);
  }
  audioSources.add(source); source.onended = () => {
    audioSources.delete(source);
    if (first && context?.state === 'running' && context.currentTime >= scheduledAt + buffer.duration) {
      startupMark('first_audio_buffer_completed', performance.now(), trace);
    }
  };
  setState('speaking', 'You can speak naturally, even during a reply.');
}
function onMessage(event) {
  const message = JSON.parse(event.data);
  if (message.type === 'wake' && !message.calibrating) beginStartupTiming('wake_received');
  if (message.startup) {
    if (!startupTiming || (startupTiming.server && startupTiming.server.id !== message.startup.id)) beginStartupTiming(message.startup.trigger);
    startupTiming.server = message.startup;
  }
  if (message.type === 'state') {
    if (message.state === 'listening') startupMark('session_ready_received');
    if (message.state === 'connecting') { active = true; sessionStart = Date.now(); notice(''); sourceUrls.clear(); $('source-links').replaceChildren(); $('sources').hidden = true; }
    setState(message.state,message.message); buttons();
    if (message.state === 'listening') {
      startupMark('ui_ready_updated');
      const trace = startupTiming;
      requestAnimationFrame(() => startupMark('ui_ready_render_opportunity', performance.now(), trace));
    }
  } else if (message.type === 'audio') { startupMark('first_assistant_audio_received'); playAudio(message.audio); }
  else if (message.type === 'transcript') appendTranscript(message.role,message.delta);
  else if (message.type === 'sources') showSources(message.sources);
  else if (message.type === 'tool') showTool(message);
  else if (message.type === 'notice' || message.type === 'error') notice(message.message);
  else if (message.type === 'wake_score') $('wake-meter-fill').style.width = `${Math.min(100,message.score*100)}%`;
  else if (message.type === 'wake' && testing) { wakeHits++; $('test-result').textContent = `Detected ${wakeHits} ${wakeHits===1?'time':'times'}. Try different distances and some ordinary conversation.`; }
  else if (message.type === 'session_end') {
    startupMark('session_end_received');
    console.debug('R2 startup timing', startupTiming);
    active = false; stopPlayback(); buttons();
    setState(muted ? 'muted' : listening ? 'idle' : 'off',message.message);
    $('session-clock').textContent = 'STANDING BY'; refreshLights();
  }
}
async function connectSocket() {
  if (socket?.readyState === WebSocket.OPEN) return;
  socket = new WebSocket(`ws://${location.host}/ws`,['r2',state.csrf]);
  socket.onmessage = onMessage;
  await new Promise((resolve,reject) => { socket.onopen = resolve; socket.onerror = () => reject(Error('Cannot connect to the local R2 service.')); socket.onclose = event => reject(Error(event.reason || 'Another R2 tab may already be listening.')); });
  socket.onclose = () => { active = false; stopMicrophone(); notice('Local connection closed. Start listening to reconnect.'); };
}
async function microphone() {
  if (stream) return;
  captureTimings.microphone_requested = performance.now();
  stream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1},video:false});
  captureTimings.microphone_acquired = performance.now();
  context = new AudioContext({sampleRate:16000});
  await context.resume();
  if (context.sampleRate !== 16000) throw Error('This browser could not provide 16 kHz audio. Use Chrome or Edge.');
  await context.audioWorklet.addModule('/static/capture.js');
  capture = new AudioWorkletNode(context,'r2-capture');
  const source = context.createMediaStreamSource(stream); sink = context.createGain(); sink.gain.value = 0;
  source.connect(capture); capture.connect(sink); sink.connect(context.destination);
  captureTimings.audio_capture_graph_ready = performance.now();
  delete captureTimings.first_capture_frame;
  capture.port.onmessage = ({data}) => {
    if (captureTimings.first_capture_frame === undefined) {
      captureTimings.first_capture_frame = performance.now();
      if (startupTiming) startupMark('first_capture_frame', captureTimings.first_capture_frame);
    }
    if (recording) recording.push(data.slice(0));
    if (socket?.readyState === WebSocket.OPEN) {
      if (socket.bufferedAmount > 32000*5) { command('stop'); stopMicrophone(); notice('The local audio connection fell behind. Reconnect to try again.'); return; }
      socket.send(data);
    }
  };
  listening = true; buttons();
}
function stopMicrophone() {
  recording = null; stream?.getTracks().forEach(track=>track.stop()); stream = null;
  capture?.disconnect(); capture = null; stopPlayback(); context?.close(); context = null;
  listening = false; muted = false; testing = false; buttons();
  $('test-wake').textContent = 'Test wake word locally';
  setState('off','Start listening, then say “Hey R2.”');
}
async function startListening(calibrate=false) {
  try { await connectSocket(); await microphone(); testing = calibrate; command('arm',{calibrate}); buttons(); notice(''); }
  catch (error) { stopMicrophone(); notice(error.name === 'NotAllowedError' ? 'Microphone access was declined. Allow it using the browser’s microphone permission control, then try again.' : error.message); }
}
$('start').onclick = async () => {
  if (listening) { command('disarm'); active=false; stopMicrophone(); }
  else await startListening();
};
$('talk').onclick = async () => {
  if (!state.openai_configured) { await openSettings(); return; }
  if (!listening) await startListening();
  if (!stream) return;
  muted = false; stream.getAudioTracks().forEach(t=>t.enabled=true); command('mute',{muted:false});
  beginStartupTiming('button'); command('activate'); active=true; buttons(); setState('connecting','Connecting to GPT-Live');
};
$('mute').onclick = () => { muted = !muted; stream?.getAudioTracks().forEach(t=>t.enabled=!muted); command('mute',{muted}); buttons(); setState(muted?'muted':active?'listening':'idle',muted?'Your microphone is off.':'Go ahead.'); };
$('end').onclick = () => { command('stop'); stopPlayback(); $('end').disabled=true; setState('idle','Ending the conversation…'); };
$('settings-open').onclick = $('connect-home').onclick = openSettings;
$('refresh-locks').onclick = loadLockSettings;
$('save-locks').onclick = async () => {
  $('save-locks').disabled = true;
  const locks = [...$('lock-options').querySelectorAll('.lock-option')]
    .filter(row => row.querySelector('input[type=checkbox]').checked)
    .map(row => ({entity_id: row.dataset.entityId, name: row.querySelector('input[type=text]').value.trim()}));
  try {
    const result = await api('/api/locks/settings', {locks});
    $('lock-save-result').textContent = result.message;
    await refresh(); await refreshLocks();
  } catch (error) { $('lock-save-result').textContent = error.message; }
  finally { $('save-locks').disabled = false; }
};
$('settings-close').onclick = () => { if (testing) { command('disarm'); stopMicrophone(); } $('settings-dialog').close(); };
$('settings-form').onsubmit = async event => {
  event.preventDefault(); $('save').disabled=true; $('save-result').textContent='Checking connections…';
  const body = {};
  if ($('openai-key').value.trim()) body.openai_api_key=$('openai-key').value.trim();
  if ($('ha-token').value.trim()) body.ha_token=$('ha-token').value.trim();
  if (body.ha_token || (state.ha_configured && $('ha-url').value !== state.ha_url)) body.ha_url=$('ha-url').value;
  try { const result=await api('/api/settings',body); $('openai-key').value=''; $('ha-token').value=''; $('save-result').textContent=result.message; await refresh(); await refreshLights(); if(state.openai_configured)notice(''); }
  catch(error){$('save-result').textContent=error.message;}
  finally{$('save').disabled=false;}
};
$('threshold').oninput=()=>{$('threshold-value').textContent=Number($('threshold').value).toFixed(2);};
$('save-threshold').onclick=async()=>{try{await api('/api/settings',{wake_threshold:Number($('threshold').value)});await refresh();if(testing)command('arm',{calibrate:true});}catch(e){notice(e.message);}};
$('test-wake').onclick=async()=>{
  if(testing){command('disarm');stopMicrophone();return;}
  if(active){notice('End the current conversation before testing your wake word.');return;}
  wakeHits=0;$('test-result').textContent='Say “Hey R2” a few times. No audio is sent to OpenAI.';
  await startListening(true);if(testing)$('test-wake').textContent='Stop local test';
};
async function record(label){
  if(active){notice('End the conversation before recording calibration clips.');return;}
  if(!testing)await startListening(true);if(!stream)return;
  $('record-positive').disabled=$('record-negative').disabled=true;
  recording=[];$('record-status').textContent=label==='positive'?'Recording 4 seconds… say “Hey R2” once.':'Recording 8 seconds… speak normally without saying Hey R2.';
  await new Promise(resolve=>setTimeout(resolve,label==='positive'?4000:8000));
  if(!recording){$('record-positive').disabled=$('record-negative').disabled=false;return;}
  const parts=recording;recording=null;
  const bytes=new Uint8Array(parts.reduce((s,x)=>s+x.byteLength,0));let offset=0;
  for(const part of parts){bytes.set(new Uint8Array(part),offset);offset+=part.byteLength;}
  try{const response=await fetch(`/api/calibration/${label}`,{method:'POST',headers:{'Content-Type':'application/octet-stream','X-R2-CSRF':state.csrf},body:bytes});const data=await response.json();if(!response.ok)throw Error(data.detail);$('record-status').textContent=`${data.count} ${label==='positive'?'wake-phrase':'background'} recordings saved locally.`;}
  catch(e){$('record-status').textContent=e.message;}
  finally{$('record-positive').disabled=$('record-negative').disabled=false;}
}
$('record-positive').onclick=()=>record('positive');$('record-negative').onclick=()=>record('negative');
$('train').onclick=async()=>{try{if(listening){command('disarm');stopMicrophone();}await api('/api/train',{});await refresh();}catch(e){notice(e.message);}};
$('wake-validated').onchange=async()=>{if(!$('wake-validated').checked)return;try{await api('/api/settings',{wake_validated:true});await refresh();}catch(e){notice(e.message);}};
setInterval(()=>{
  if(active){const seconds=Math.floor((Date.now()-sessionStart)/1000);$('session-clock').textContent=`LIVE · ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;command('playback',{seconds:Math.max(0,nextPlayback-(context?.currentTime||0))});if(phase==='speaking'&&!audioSources.size&&!muted)setState('listening','Ask a follow-up, or say what you need.');}
},500);
setInterval(async()=>{if(polling)return;polling=true;try{if(state.training?.running)await refresh();if(!active)await refreshLights();}finally{polling=false;}},15000);
window.addEventListener('pagehide',()=>{command('disarm');stopMicrophone();socket?.close();});
try{await refresh();await refreshLights();}catch(e){notice('Cannot reach the local R2 service. Run Start R2.cmd to start it.');}
