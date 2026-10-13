#!/opt/node-v22.23.2-linux-arm64/bin/node
// 进程内计时测 /exec 通道：延迟用进程内计时，避免 shell 垫层虚高
const http = require('http');
const port = +process.argv[2], tok = process.argv[3], tag = process.argv[4] || '';

function call(body, timeoutMs = 12000) {
  return new Promise(resolve => {
    const t = process.hrtime.bigint();
    let done = false;
    const req = http.request({
      host: '127.0.0.1', port, path: '/exec', method: 'POST',
      headers: { 'X-Token': tok, 'Content-Length': Buffer.byteLength(body) }
    }, r => {
      let d = ''; r.setEncoding('utf8');
      r.on('data', c => d += c);
      r.on('end', () => { if (done) return; done = true; resolve({ ms: Number(process.hrtime.bigint() - t) / 1e6, out: d.trim() }); });
    });
    req.on('error', e => { if (done) return; done = true; resolve({ ms: Number(process.hrtime.bigint() - t) / 1e6, out: 'ERR ' + e.message }); });
    req.setTimeout(timeoutMs, () => { if (done) return; done = true; req.destroy(); resolve({ ms: Number(process.hrtime.bigint() - t) / 1e6, out: 'TIMEOUT(客户端断开)' }); });
    req.end(body);
  });
}

(async () => {
  const arr = [];
  for (let i = 0; i < 8; i++) arr.push((await call('echo hi')).ms);
  const s = [...arr].sort((a, b) => a - b);
  console.log(`${tag} echo hi：中位 ${s[4].toFixed(1)}ms  最小 ${s[0].toFixed(1)}ms  最大 ${s[7].toFixed(1)}ms`);

  const gp = await call('getprop ro.build.version.release');
  console.log(`${tag} getprop：${gp.ms.toFixed(1)}ms → ${JSON.stringify(gp.out)}`);

  const nl = await call('printf "a\\nb\\n" | wc -l');
  console.log(`${tag} 管道命令：${nl.ms.toFixed(1)}ms → ${JSON.stringify(nl.out)}`);

  // 中文命令放最后：旧版按字符数读 body 会挂死，且单线程 accept 会把整个端点堵死
  const zh = await call('echo 中文测试');
  console.log(`${tag} 中文命令：${zh.ms.toFixed(0)}ms → ${JSON.stringify(zh.out)}`);

  const after = await call('echo 挂死之后端点还活吗', 8000);
  console.log(`${tag} 后续命令（端点是否仍活）：${after.ms.toFixed(0)}ms → ${JSON.stringify(after.out)}`);
})();
