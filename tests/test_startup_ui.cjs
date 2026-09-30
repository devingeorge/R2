// Deterministic checks of the real UI handlers; no browser/hardware timing claim.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

function ui() {
  const elements = new Map(), renders = [], sources = [];
  let now = 1000;
  const audio = {
    currentTime: 2, state: 'running', destination: {},
    getOutputTimestamp: () => ({contextTime: 1.99, performanceTime: 990}),
    createBuffer: (_, length, rate) => ({duration: length / rate, getChannelData: () => new Float32Array(length)}),
    createBufferSource: () => {
      const source = {connect() {}, start(at) { this.at = at; }, stop() { this.onended?.(); }};
      sources.push(source); return source;
    },
  };
  const sandbox = vm.createContext({
    window: {}, performance: {now: () => now}, console: {debug() {}},
    document: {getElementById: id => {
      if (!elements.has(id)) elements.set(id, {replaceChildren() {}});
      return elements.get(id);
    }},
    requestAnimationFrame: fn => renders.push(fn),
    WebSocket: {OPEN: 1}, atob: x => Buffer.from(x, 'base64').toString('binary'),
    fakeAudio: audio,
  });
  // Load declarations without attaching click handlers or running page bootstrap.
  const app = fs.readFileSync('static/app.js', 'utf8').split("$('start').onclick")[0];
  vm.runInContext(app, sandbox);
  vm.runInContext('context = fakeAudio; active = true;', sandbox);
  function message(data) { sandbox.event = {data: JSON.stringify(data)}; vm.runInContext('onMessage(event)', sandbox); }
  return {sandbox, audio, sources, renders, elements, message, tick: value => { now = value; }};
}

test('readiness updates immediately and playback only adds the existing 25 ms', () => {
  const page = ui();
  page.message({type:'wake', calibrating:false});
  page.tick(1200);
  page.message({type:'state', state:'listening', message:'Ready', startup:{id:'one'}});
  const trace = page.sandbox.window.r2StartupTiming;
  assert.equal(page.elements.get('state-title').textContent, 'I’m listening');
  assert.equal(trace.browser.marks.ui_ready_updated - trace.browser.marks.session_ready_received, 0);
  assert.equal(trace.browser.marks.ui_ready_render_opportunity, undefined);
  page.tick(1216); page.renders[0]();
  assert.equal(trace.browser.marks.ui_ready_render_opportunity, 1216);
  // 10 ms silence then speech: estimate the first audible sample, not buffer start.
  const pcm = Buffer.alloc(2560);
  for (let i = 160; i < 1280; i++) pcm.writeInt16LE(1000, i * 2);
  page.message({type:'audio', audio:pcm.toString('base64')});
  assert.equal(page.sources[0].at, 2.025);
  assert.ok(Math.abs(trace.browser.marks.first_audible_output_estimated - 1035) < .001);
  page.audio.currentTime = 2.2;
  page.sources[0].onended();
  assert.equal(trace.browser.marks.first_audio_buffer_completed, 1216);
  page.message({type:'wake', calibrating:false});
  assert.notEqual(page.sandbox.window.r2StartupTiming, trace);
  assert.equal(page.sandbox.window.r2StartupTiming.browser.marks.first_assistant_audio_scheduled, undefined);
});

test('stopping playback early does not claim completed or audible playback', () => {
  const page = ui();
  page.audio.state = 'suspended';
  page.message({type:'wake', calibrating:false});
  const pcm = Buffer.alloc(2560, 8);
  page.message({type:'audio', audio:pcm.toString('base64')});
  vm.runInContext('stopPlayback()', page.sandbox);
  const marks = page.sandbox.window.r2StartupTiming.browser.marks;
  assert.equal(marks.first_audible_output_estimated, undefined);
  assert.equal(marks.first_audio_buffer_completed, undefined);
});
