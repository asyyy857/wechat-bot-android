#!/opt/node-v22.23.2-linux-arm64/bin/node
// 测 /list 与各屏取帧延迟（进程内计时），并把帧存盘供镜像比对
const http = require('http');
const fs = require('fs');
const port = +process.argv[2], tag = process.argv[3] || '';

function get(path, timeoutMs = 20000) {
  return new Promise(resolve => {
    const t = process.hrtime.bigint();
    let done = false;
    const req = http.request({ host: '127.0.0.1', port, path, method: 'GET' }, r => {
      const chunks = [];
      r.on('data', c => chunks.push(c));
      r.on('end', () => {
        if (done) return; done = true;
        resolve({ ms: Number(process.hrtime.bigint() - t) / 1e6, buf: Buffer.concat(chunks), ct: r.headers['content-type'] || '' });
      });
    });
    req.on('error', e => { if (done) return; done = true; resolve({ ms: Number(process.hrtime.bigint() - t) / 1e6, buf: Buffer.alloc(0), err: e.message }); });
    req.setTimeout(timeoutMs, () => { if (done) return; done = true; req.destroy(); resolve({ ms: Number(process.hrtime.bigint() - t) / 1e6, buf: Buffer.alloc(0), err: 'TIMEOUT' }); });
    req.end();
  });
}

(async () => {
  const list = await get('/list');
  const txt = list.buf.toString('utf8');
  console.log(`${tag} /list：${list.ms.toFixed(1)}ms → ${txt.slice(0, 320)}`);
  let n = 0;
  try { const j = JSON.parse(txt); n = Array.isArray(j) ? j.length : (j.displays || []).length; } catch (e) {}
  for (let i = 0; i < Math.min(n, 2); i++) {
    await get(`/${i}/frame`);
    const arr = [];
    for (let k = 0; k < 5; k++) arr.push((await get(`/${i}/frame`)).ms);
    const s = [...arr].sort((a, b) => a - b);
    const f = await get(`/${i}/frame`);
    console.log(`${tag} 屏${i} 取帧：中位 ${s[2].toFixed(1)}ms  最小 ${s[0].toFixed(1)}ms  最大 ${s[4].toFixed(1)}ms  字节=${f.buf.length} 类型=${f.ct}`);
    fs.writeFileSync(`/root/vsd-test/vsd-${port}-disp${i}.jpg`, f.buf);
  }
})();
