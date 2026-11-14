// Graba el clip del monitor para el pitch (docs/monitor.mp4) con Chrome headless por CDP, sin dependencias:
// Node 22+ trae WebSocket y fetch. Guión: sesión de alice → acción #20 (inyección) → sesión de mallory →
// `owit tamper --mode delete --seq 1` sobre su bitácora → insignia ALTERADA → acción #20 NO VERIFICABLE.
//
//   uv run owit monitor --demo --ui ../monitoring-UI/dist          # en otra terminal (bitácoras recién generadas)
//   node bench/tools/record_monitor.mjs /tmp/rec "$PWD" http://127.0.0.1:8790
//   swift bench/tools/frames_to_mp4.swift /tmp/rec docs/monitor.mp4 1.5   # macOS (AVFoundation), sin ffmpeg
import { spawn, execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const [outDir, repo, base] = process.argv.slice(2);
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PORT = 9333, W = 1440, H = 900, SCALE = 4 / 3;   // 1920x1200
const frames = join(outDir, 'frames');
mkdirSync(frames, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));

const chrome = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${PORT}`, '--hide-scrollbars',
  `--user-data-dir=${join(outDir, 'profile')}`, `--window-size=${W},${H}`, 'about:blank'], { stdio: 'ignore' });

let target;
for (let i = 0; i < 100 && !target; i++) {
  try { target = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find(t => t.type === 'page'); }
  catch { await sleep(100); }
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r, { once: true }));
let id = 0; const pending = new Map();
const log = [];
let t0 = null, n = 0;
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
  if (m.method === 'Page.screencastFrame') {
    const { data, metadata, sessionId } = m.params;
    if (t0 === null) t0 = metadata.timestamp;
    const f = `f${String(n++).padStart(5, '0')}.jpg`;
    writeFileSync(join(frames, f), Buffer.from(data, 'base64'));
    log.push({ f, t: metadata.timestamp - t0 });
    send('Page.screencastFrameAck', { sessionId });
  }
});
const send = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const go = async hash => { await send('Runtime.evaluate', { expression: `location.hash = ${JSON.stringify(hash)}` }); };
const marks = [];
const mark = label => marks.push({ label, t: log.length ? log[log.length - 1].t : 0 });

await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: SCALE, mobile: false });
await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'light' }] });

const sessions = await (await fetch(`${base}/api/sessions`)).json();
const sid = u => sessions.find(s => s.user === u).sid;
const alice = sid('alice'), mallory = sid('mallory');
const ledger = join(repo, 'demo', 'data', 'ledger', `${mallory}.jsonl`);
if (!existsSync(ledger)) throw new Error('no está la bitácora de mallory: ' + ledger);

await send('Page.navigate', { url: `${base}/#/s/${alice}` });
await sleep(2500);
await send('Page.startScreencast', { format: 'jpeg', quality: 88, maxWidth: W * SCALE, maxHeight: H * SCALE });
mark('alice'); await sleep(2000);
mark('alice_action'); await go(`/s/${alice}/a/20`); await sleep(5000);
mark('mallory'); await go(`/s/${mallory}`); await sleep(2500);
mark('tamper');
execFileSync('uv', ['run', 'owit', 'tamper', ledger, '--mode', 'delete', '--seq', '1', '--data', join(repo, 'demo', 'data')],
  { cwd: repo, stdio: 'inherit' });
await sleep(4000);
mark('mallory_action'); await go(`/s/${mallory}/a/20`); await sleep(4000);
mark('end');
await send('Page.stopScreencast');
await sleep(300);
writeFileSync(join(outDir, 'frames.json'), JSON.stringify({ width: Math.round(W * SCALE), height: Math.round(H * SCALE), frames: log, marks, alice, mallory }, null, 1));
ws.close(); chrome.kill();
console.log(`frames: ${log.length} · duración ${log.at(-1).t.toFixed(1)} s`, marks);
process.exit(0);
