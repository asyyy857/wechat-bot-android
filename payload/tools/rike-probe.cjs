#!/usr/bin/env node
/**
 * rike-probe.cjs —— 日课进场「廉价探针」（副屏模式）
 *
 * ★ 为什么有这个东西（2026-10-09 实测数据）：
 *   旧进场脚本每等一个页面 = 抓帧 + convert 放大 200%(1.4s) + tesseract 全屏(2.6~6s) + sleep 2
 *   ≈ **每轮 6 秒**，而一轮只是"看一眼"；冷启动加载期画面根本认不出，等于白烧。
 *   本探针换三层策略：
 *     ① **廉价采样**：只取 32px 宽小帧（服务端缩放，实测单次 16~85ms/中位 49ms）+ md5 当签名，
 *        单进程内循环（不再每次 fork 一个 node）—— 比"看一次"便宜 ~100 倍；
 *     ② **静止才 OCR**：画面还在动（加载动画/进度条）= 认不出 → 直接跳过 OCR；
 *        只有签名连续 --still 次不变（画面静止）才做全屏 OCR；
 *     ③ **画面缓存**：同一签名只 OCR 一次，文本存盘（跨进程复用）→ 循环动画再久也只烧一次；
 *        缓存命中时直接拿旧文本判正则，**零 OCR 命中**，命中延迟降到 1 个采样周期；
 *     ④ **--force-ocr 兜底**：画面动个不停时也每 N 秒看一眼，防"永远不稳定"。
 *
 * 用法:
 *   node rike-probe.cjs wait --match "<正则>" [--timeout 90] [--slot 1]
 *                            [--still 2] [--gap 200] [--force-ocr 7] [--trace 文件] [--verbose]
 *   退出码: 0=命中  1=超时  2=用法/抓帧错
 *   退出时把「当前画面」的 OCR 全文写 /tmp/rike-probe.ocr（命中=命中那份，超时=最后一次）；
 *   机器可读摘要写 /tmp/rike-probe.meta（hit= / elapsed= / textAge= / ocr=）。
 */
const http = require('http');
const crypto = require('crypto');
const fs = require('fs');
const { execFileSync } = require('child_process');

const VSD_PORT = +(process.env.VSD_PORT || 18250);
const OCROUT = '/tmp/rike-probe.ocr';
const CACHEF = '/tmp/rike-probe.cache.json';
const FULL = '/tmp/rike-probe-full.jpg';

function arg(name, def) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] !== undefined ? process.argv[i + 1] : def;
}
const flag = name => process.argv.includes('--' + name);

const cmd = process.argv[2];
const slot = +arg('slot', process.env.RIKE_SLOT || 1);
const GAP = +arg('gap', 200);              // 画面在动时的采样间隔 ms
const GAP_STILL = +arg('gap-still', 350);  // 已静止后的采样间隔
const STILL = +arg('still', 2);            // 连续几次同签名算"静止"
const FORCE = +arg('force-ocr', 7);        // 距上次 OCR 超这么久就强制看一次
const CHANGEFROM = arg('change-from', '');   // 等到签名 != 这个值（零 OCR，秒判点击是否被吞）
const NOTSIG = (arg('not-sig', '') || '').split(',').filter(Boolean);  // 这些签名视为「还没走」
const PROBE_W = +arg('probe-w', 64);       // 采样宽度（越大越不易撞签名，成本几乎不变）
const TMO = +arg('timeout', 90);
const MATCH = arg('match', null);
const TRACE = arg('trace', null);
const VERBOSE = flag('verbose');
const agent = new http.Agent({ keepAlive: true, maxSockets: 2 });

const T0 = Date.now();
const now = () => (Date.now() - T0) / 1000;

function frame(w) {
  return new Promise((res, rej) => {
    const path = `/${slot}/frame` + (w ? `?w=${w}` : '');
    const r = http.request({ host: '127.0.0.1', port: VSD_PORT, path, method: 'GET',
                             agent, timeout: 8000 }, resp => {
      if (resp.statusCode !== 200) { resp.resume(); return rej(new Error('HTTP ' + resp.statusCode)); }
      const cs = []; resp.on('data', c => cs.push(c)); resp.on('end', () => res(Buffer.concat(cs)));
    });
    r.on('timeout', () => r.destroy(new Error('timeout')));
    r.on('error', rej); r.end();
  });
}
const sigOf = buf => crypto.createHash('md5').update(buf).digest('hex').slice(0, 8);

// 向副屏注入输入：与 vscreen 同一条通道（命令守护 :18252，失败回退 adb）
function info() {
  return new Promise(res => {
    http.get({ host: '127.0.0.1', port: VSD_PORT, path: `/${slot}/info`, timeout: 5000 }, r => {
      const cs = []; r.on('data', c => cs.push(c)); r.on('end', () => { try { res(JSON.parse(Buffer.concat(cs))); } catch (e) { res({}); } });
    }).on('error', () => res({}));
  });
}
function input(args) {
  return new Promise(res => {
    let token = '';
    try { token = fs.readFileSync('/sdcard/dsh/.shd-token', 'utf8').trim(); } catch (e) {}
    const body = Buffer.from(args, 'utf8');
    const r = http.request({ host: '127.0.0.1', port: +(process.env.SHD_PORT || 18252), path: '/exec',
      method: 'POST', timeout: 15000, headers: { 'X-Token': token, 'Content-Length': body.length } },
      resp => { resp.resume(); resp.on('end', () => res(true)); });
    r.on('error', () => res(false)); r.on('timeout', () => { r.destroy(); res(false); });
    r.write(body); r.end();
  });
}

let cache = {};
try { cache = JSON.parse(fs.readFileSync(CACHEF, 'utf8')); } catch (e) { cache = {}; }
function saveCache() {
  try {
    const ks = Object.keys(cache);
    if (ks.length > 60) { const keep = {}; ks.slice(-40).forEach(k => keep[k] = cache[k]); cache = keep; }
    fs.writeFileSync(CACHEF, JSON.stringify(cache));
  } catch (e) {}
}
function trace(line) { if (TRACE) try { fs.appendFileSync(TRACE, `+${now().toFixed(1)}s\t${line}\n`); } catch (e) {} }
function meta(hit, ocrN, textAge, sig, stillN) {
  try { fs.writeFileSync('/tmp/rike-probe.meta',
    `hit=${hit ? 1 : 0}\nelapsed=${now().toFixed(1)}\ntextAge=${textAge.toFixed(1)}\nocr=${ocrN}\nsig=${sig || ''}\nstill=${stillN || 0}\n`); } catch (e) {}
}

// OCR：★ 渐进式两段（2026-10-09 实测）
//   ① 快路径 = 原图 + psm 11：8 张历史帧上与「200% 放大」读出的关键词**完全一致**，
//      最好时 1.2s（放大版 3.4~6s）；
//   ② 慢兜底 = 200% 放大（旧脚本同款）：只有快路径没匹配上正则时才跑，保住识别率不退化。
//   返回 [文本, 用时秒, 用了几段]
const DSH = process.env.DSH_ROOT || '/sdcard/dsh';
const OCRSH = DSH + '/tools/rike-ocr.sh';
// ★ 2026-10-10 换引擎：tesseract → RapidOCR 常驻服务（tools/rike-ocr.sh）。
//   同一批历史帧整屏关键词召回 96.5% vs tesseract 的 25~75% → 「看不清就重试」的浪费大幅减少。
//   ⚠️ 原来「读不出就放大 200% 再来一遍」的慢兜底**已删掉**：实测 RapidOCR 放大后召回不变、
//      耗时 +50%（卡行 100% 与 400% 都是 94%），纯浪费。
function tess(src) {
  try {
    return execFileSync('sh', [OCRSH, src],
                        { encoding: 'utf8', timeout: 120000, maxBuffer: 8 << 20, stdio: ['ignore', 'pipe', 'ignore'] });
  } catch (e) { return ''; }
}
// 已知页面特征词：快路径只要认出**任意一个**，就说明"这一页已经看清了"（只是不是目标页），
// 不必再花 3 秒跑慢兜底 —— 待机画面（「匆匆」）就是这么从 8.1s 降到 2.5s 的。
const PAGE_WORDS = /匆匆|其他账号登录|上次登录|正在初始化|闯荡江湖|缘定三生|登录展示|进入游戏|青崖书|青崖韦|青岩书|上岩书|殊途同归|日课点|侧图标定位|顶号|切换账号|开始游戏|每日签到|公告|邮箱/;
function ocr(re) {
  const t0 = Date.now();
  const t1 = tess(FULL);
  if (re && re.test(t1)) return [t1, (Date.now() - t0) / 1000, 1];
  if (PAGE_WORDS.test(t1)) return [t1, (Date.now() - t0) / 1000, 1];
  // 读出来的汉字太少 = 黑屏/加载动画/纯色画面 —— 放大再 OCR 也是白烧 3~5 秒，直接放弃
  const han = (t1.match(/[\u4e00-\u9fa5]/g) || []).length;
  if (han < 8) return [t1, (Date.now() - t0) / 1000, 1];
  return [t1, (Date.now() - t0) / 1000, 1];
}

// ★ 2026-10-10 加：settle —— **纯采样等画面静止**（零 OCR，几十 ms 一次）。
//   用途：点之前先等 UI 稳定。实测吞点击的主因是"游戏正在加载/准备界面时抑制输入"
//   （同一批实验：关面板 16/16 成功，开面板只有 18/24）→ 别在加载的那几帧点。
//   退出码：0 = 连续 STILL 次同签名（稳定）｜1 = 超时（画面一直在动）
async function settleMain() {
  let lastSig = null, stillN = 0;
  while (now() < TMO) {
    let buf;
    try { buf = await frame(PROBE_W); }
    catch (e) { await new Promise(r => setTimeout(r, 120)); continue; }
    const s = sigOf(buf);
    if (s === lastSig) stillN++; else { stillN = 0; lastSig = s; }
    if (stillN >= STILL - 1) {
      console.log(`PROBE SETTLE ${now().toFixed(1)}s sig=${s} still=${stillN + 1}`);
      meta(true, 0, 0, s, stillN + 1);
      process.exit(0);
    }
    await new Promise(r => setTimeout(r, GAP));
  }
  console.log(`PROBE SETTLE-TIMEOUT ${now().toFixed(1)}s last=${lastSig || ''}`);
  meta(false, 0, 0, lastSig, stillN);
  process.exit(1);
}

async function main() {
  if (cmd === 'settle') return settleMain();
  if (cmd !== 'wait') {
    console.log('用法:');
    console.log('  node rike-probe.cjs wait --match "<正则>" [--timeout 90] [--slot 1] [--still 2] [--gap 200] [--force-ocr 7]');
    console.log('  node rike-probe.cjs settle [--still 2] [--gap 80] [--timeout 1.2]   # 等画面静止（零 OCR）');
    process.exit(2);
  }
  const re = MATCH ? new RegExp(MATCH) : (CHANGEFROM ? null : /.*/);
  let lastSig = null, stillN = 0, lastOcrAt = 0, ocrN = 0, lastTextAt = 0, lastOcrText = '';
  trace(`START match=${MATCH} tmo=${TMO} still=${STILL} gap=${GAP} force=${FORCE}`);

  while (now() < TMO) {
    let buf;
    try { buf = await frame(PROBE_W); }
    catch (e) { await new Promise(r => setTimeout(r, 400)); continue; }

    const s = sigOf(buf);
    if (CHANGEFROM && s !== CHANGEFROM) {
      trace(`CHANGE +${now().toFixed(1)}s ${CHANGEFROM} → ${s}`);
      console.log(`PROBE CHANGE ${now().toFixed(1)}s ${CHANGEFROM}→${s}`);
      meta(true, 0, 0, s, 0);
      process.exit(0);
    }
    if (s === lastSig) stillN++; else { stillN = 0; lastSig = s; }
    const stable = stillN >= STILL - 1;

    // ③ 缓存命中：同一画面已经 OCR 过 → 直接拿旧文本判，零成本、零延迟
    const cached = cache[s];
    if (cached !== undefined && !NOTSIG.includes(s) && re && re.test(cached)) {
      fs.writeFileSync(OCROUT, cached);
      trace(`HIT(cache) +${now().toFixed(1)}s sig=${s}`);
      console.log(`PROBE HIT ${now().toFixed(1)}s sig=${s} ocr=${ocrN}次(缓存)`);
      meta(true, ocrN, now() - lastTextAt, s, stillN);
      process.exit(0);
    }

    const sinceOcr = now() - lastOcrAt;
    const wantOcr = cached === undefined && !NOTSIG.includes(s) && (stable || sinceOcr > FORCE);
    if (wantOcr) {
      let full;
      try { full = await frame(0); } catch (e) { full = null; }
      if (full) {
        fs.writeFileSync(FULL, full);
        // 诊断用：滚动留存最近 3 张 OCR 帧（/tmp/rike-probe-last1.jpg = 最新）
        try {
          if (fs.existsSync('/tmp/rike-probe-last2.jpg')) fs.renameSync('/tmp/rike-probe-last2.jpg', '/tmp/rike-probe-last3.jpg');
          if (fs.existsSync('/tmp/rike-probe-last1.jpg')) fs.renameSync('/tmp/rike-probe-last1.jpg', '/tmp/rike-probe-last2.jpg');
          fs.copyFileSync(FULL, '/tmp/rike-probe-last1.jpg');
        } catch (e) {}
        const [txt, costS, segs] = ocr(re);
        const cost = costS.toFixed(1);
        lastOcrAt = now(); ocrN++;
        cache[s] = txt; saveCache();
        const hit = re ? re.test(txt) : true;
        trace(`OCR #${ocrN} +${now().toFixed(1)}s cost=${cost}s(${segs}段) sig=${s} ${hit ? 'HIT' : 'miss'} :: ${txt.replace(/\s+/g, '').slice(0, 46)}`);
        // 待机画面（竹林 +「江湖匆匆 稍作停歇…」）会吞掉一切输入，且它一直有动画 →
        // 签名永不静止。★ 检测词只能是「匆匆」（实测读不出「稍作停歇」）。
        if (!hit && /匆匆/.test(txt)) {
          const inf = await info();
          if (inf && inf.displayId !== undefined) {
            trace(`IDLE 唤醒 +${now().toFixed(1)}s`);
            if (VERBOSE) console.log(`  · 检测到待机画面 → 唤醒`);
            await input(`input -d ${inf.displayId} swipe 300 318 1000 318 300`);
            await new Promise(r => setTimeout(r, 2500));
            lastSig = null; stillN = 0;      // 唤醒后画面必变，重新开始判静止
          }
        }
        if (VERBOSE) console.log(`  · 探针 OCR #${ocrN} @${now().toFixed(1)}s 用时 ${cost}s ${hit ? '✅命中' : '未命中'}`);
        if (hit) {
          fs.writeFileSync(OCROUT, txt);
          console.log(`PROBE HIT ${now().toFixed(1)}s sig=${s} ocr=${ocrN}次 OCR用时${cost}s`);
          meta(true, ocrN, 0, s, stillN);
          process.exit(0);
        }
        lastTextAt = now(); lastOcrText = txt;
      }
    }
    await new Promise(r => setTimeout(r, stable ? GAP_STILL : GAP));
  }
  // 超时：只有"最后一次 OCR 还算新鲜"才把文本留给 shell；太旧的文本会骗得 shell 认错页面
  const age = now() - lastTextAt;
  if (lastOcrText && age < 12) { try { fs.writeFileSync(OCROUT, lastOcrText); } catch (e) {} }
  else { try { fs.writeFileSync(OCROUT, ''); } catch (e) {} }
  trace(`TIMEOUT ocr=${ocrN}`);
  console.log(`PROBE TIMEOUT ${now().toFixed(1)}s ocr=${ocrN}次`);
  meta(false, ocrN, now() - lastTextAt, lastSig, stillN);
  process.exit(1);
}
main().catch(e => { console.error('probe 异常: ' + e.message); process.exit(2); });
