// Record the Run Viewer page to an MP4: a headless Chromium screenshot every 0.5 s, piped to ffmpeg.
// node record_viewer.mjs URL OUT.mp4 [MAX_MINUTES]   Stop early: touch OUT.mp4.stop (the file is playable at any time).
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [url, out, maxMinutes = '90'] = process.argv.slice(2);
const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';   // any Chrome or Chromium
const WIDTH = 1600, HEIGHT = 2100, FPS = 2;
const stopFile = out + '.stop';
const sleep = ms => new Promise(r => setTimeout(r, ms));

rmSync(stopFile, { force: true });
const profile = mkdtempSync(join(tmpdir(), 'record-viewer-'));   // its own profile: a Chrome that is open already is not touched
const chrome = spawn(CHROME, ['--headless', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--hide-scrollbars', `--window-size=${WIDTH},${HEIGHT}`, 'about:blank'], { stdio: 'ignore' });
const ffmpeg = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-c:v', 'mjpeg', '-i', '-',
  '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '26', '-g', String(FPS * 10), '-pix_fmt', 'yuv420p', '-movflags', '+frag_keyframe+empty_moov', out], { stdio: ['pipe', 'inherit', 'inherit'] });

let target;
for (let i = 0; i < 50 && !target; i++) {
  await sleep(200);
  try {                                                        // port 0: Chrome takes a free port and writes it in the profile, so recorders can run side by side
    const port = readFileSync(join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0];
    target = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find(t => t.type === 'page');
  } catch (e) {}
}
if (!target) { console.error(`no Chrome page from ${CHROME}: set CHROME to a Chrome or Chromium binary`); process.exit(1) }
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r));
let id = 0;
const pending = {};
const send = (method, params = {}) => new Promise(r => { pending[++id] = r; ws.send(JSON.stringify({ id, method, params })) });
ws.addEventListener('message', m => { const d = JSON.parse(m.data); if (d.id && pending[d.id]) { pending[d.id](d.result); delete pending[d.id] } });

await send('Emulation.setDeviceMetricsOverride', { width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url });
await sleep(3000);
console.log(`recording ${url} -> ${out}; stop: touch ${stopFile}`);

const end = Date.now() + Number(maxMinutes) * 60000;
let frames = 0;
while (Date.now() < end && !existsSync(stopFile)) {      // one screenshot per 1/FPS s; the frame rate stays fixed, so time in the video is real time
  const started = Date.now();
  const shot = await Promise.race([sleep(5000), send('Page.captureScreenshot', { format: 'jpeg', quality: 75, captureBeyondViewport: true,   // a fixed, even size: x264 needs even sides
    clip: { x: 0, y: 0, width: WIDTH, height: HEIGHT, scale: 1 } })]);   // a capture with no answer (e.g. after the laptop slept) skips the frame, so the stop file still works
  if (shot && shot.data) { ffmpeg.stdin.write(Buffer.from(shot.data, 'base64')); frames += 1 }
  if (frames % 120 === 1) console.log(`${new Date().toLocaleTimeString()} frame ${frames}`);
  await sleep(Math.max(0, 1000 / FPS - (Date.now() - started)));
}
ffmpeg.stdin.end();
await new Promise(r => ffmpeg.on('close', r));
ws.close(); chrome.kill();
await new Promise(r => chrome.exitCode !== null ? r() : chrome.on('exit', r));   // Chrome writes its profile until it exits
rmSync(stopFile, { force: true }); rmSync(profile, { recursive: true, force: true });
console.log(`saved ${out}: ${frames} frames, ${(frames / FPS / 60).toFixed(1)} min`);
process.exit(0);
