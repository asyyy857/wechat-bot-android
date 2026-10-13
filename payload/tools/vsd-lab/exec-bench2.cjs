#!/opt/node-v22.23.2-linux-arm64/bin/node
// 新 ShellDaemon 专项测试：延迟 / 中文 / CLASSPATH / 并发不阻塞 / 超时整组清理
const http = require('http');
const { execSync } = require('child_process');
const fs = require('fs');
const port = +process.argv[2], tok = process.argv[3], tag = process.argv[4] || '';
const A = fs.readFileSync('/sdcard/dsh/.adb-serial', 'utf8').trim();
const adb = c => { try { return execSync(`adb -s ${A} shell '${c}'`, { encoding: 'utf8', timeout: 30000 }).trim(); } catch (e) { return (e.stdout || '') + (e.stderr || ''); } };

function call(body, timeoutMs = 40000) {
  return new Promise(resolve => {
    const t = process.hrtime.bigint();
    let done = false;
    const req = http.request({ host: '127.0.0.1', port, path: '/exec', method: 'POST',
      headers: { 'X-Token': tok, 'Content-Length': Buffer.byteLength(body) } }, r => {
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
  for (let i = 0; i < 6; i++) arr.push((await call('echo hi')).ms);
  const s = [...arr].sort((a, b) => a - b);
  console.log(`${tag} ① 延迟：echo hi 中位 ${s[3].toFixed(1)}ms（最小 ${s[0].toFixed(1)} / 最大 ${s[5].toFixed(1)}）`);

  const zh = await call('echo 中文测试');
  console.log(`${tag} ① 中文命令：${zh.ms.toFixed(0)}ms → ${JSON.stringify(zh.out)}`);

  const cp = await call('echo "CLASSPATH=[$CLASSPATH]"');
  console.log(`${tag} ④ CLASSPATH：${JSON.stringify(cp.out)}`);

  console.log(`${tag} ③⑤ 并发 + 超时整组清理：`);
  const slow = call('sleep 300 & sleep 300', 45000);
  await new Promise(r => setTimeout(r, 1200));
  console.log(`    慢命令启动后，设备上 sleep 300 数量 = ${adb('ps -A -o ARGS | grep "sleep 300" | grep -c sleep || true')}`);
  const t0 = Date.now();
  const quick = await call('echo 慢命令还在跑，我这条不该等');
  console.log(`    慢命令占用中，另一条命令耗时 ${Date.now() - t0}ms → ${JSON.stringify(quick.out)}`);
  const sl = await slow;
  console.log(`    sleep 25 类命令超时响应：${sl.ms.toFixed(0)}ms → ${JSON.stringify(sl.out)}`);
  await new Promise(r => setTimeout(r, 1500));
  console.log(`    超时清理后，设备上 sleep 300 数量 = ${adb('ps -A -o ARGS | grep "sleep 300" | grep -c sleep || true')}`);
})();
