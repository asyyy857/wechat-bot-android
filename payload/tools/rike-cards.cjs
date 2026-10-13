#!/usr/bin/env node
/**
 * rike-cards.cjs —— 读副屏日课卡（副屏模式核心）
 *   用法: node rike-cards.cjs <帧图>
 *   输出: index<TAB>名称<TAB>target<TAB>refreshX<TAB>src<TAB>state
 *         state = 待做 | 领取 | 已领 | ?   （见下"状态判据"）
 *
 *   ★ 2026-10-10 OCR 大换血：tesseract → RapidOCR 常驻服务（tools/rike-ocr.sh）
 *     实测同一批 7 张历史帧的 5 个卡名：
 *       tesseract  26/35 = 74%  （最右那张卡几乎必成乱码："司太有辣间半生"）
 *       RapidOCR   **35/35 = 100%**（逐帧裁图人工核对过，含 cb6 的「拍照|游历|好感度|小镇|朋友圈」）
 *     耗时 0.76s → 1.05s（多 0.3s），但省掉 convert+400% 放大那一整个进程，端到端反而持平。
 *     ⚠️ 实测**不需要放大**（原样最快最好）—— 别把 tesseract 那套 400% 搬过来。
 *
 *   识别（RapidOCR 返回带坐标 → 按 x 分配到卡位，比"按 | 切分"稳）：
 *     ① 标题行 ROI 900x50+460+358 一次请求拿到 5 个卡名
 *     ② 某张卡标题读不出时，退回该卡的【描述文字】再匹配一次
 *     ③ 还读不出就单独裁这张卡的标题再读
 *   名称匹配用「编辑距离最近」（对"一个字读错"最合适）。
 *
 *   状态判据（2026-10-08 实测标定，副屏 1400x636）：
 *     取每张卡 140x60 区域（金色大字区），数「金色文字像素」(r>235 && g>200 && b<130)：
 *       待做 = 0 ／ 领取 = 59~115 ／ 已领 = 461~533（阈值 25 / 250）
 *     ★ 2026-10-10：5 张卡的采样区**合并成一次 convert**（整条 704x60 一次出，按段统计），
 *       省掉 4 次进程启动（~0.6s/帧）；每段宽度与旧实现等价。
 */
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const TARGETS = ['剧组','拍照','朋友圈','庄园','好感度','小镇','游历','扮演派对'];
const CANDIDATES = ['剧组','拍照','朋友圈','庄园','好感度','小镇','菠萝农场','星梦乐园','副本','剧情',
  '探索','竞技','创意工坊','今日穿搭','帮会活动','游历','身份','扮演派对'];
const STEP = 141, REFRESH_Y = 322, REFRESH_X0 = 598;
const CARD_W = 140, CARD_X0 = 477;
const TITLE_ROI = '460,358,900,50';      // x,y,w,h
const GOLD_ROW = '704x60+477+445';       // 5 张卡的金色大字采样条
const OCR = path.join(__dirname, 'rike-ocr.sh');

const img = process.argv[2];
if (!img || !fs.existsSync(img)) { console.error('用法: node rike-cards.cjs <帧图>'); process.exit(2); }

// ── 编辑距离（比 lcs 更适合"一个字读错"的场景）──────────────
function lev(a, b) {
  const m = a.length, n = b.length;
  if (!m) return n; if (!n) return m;
  let prev = Array.from({ length: n + 1 }, (_, j) => j), cur = new Array(n + 1);
  for (let i = 1; i <= m; i++) {
    cur[0] = i;
    for (let j = 1; j <= n; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i-1] === b[j-1] ? 0 : 1));
    }
    [prev, cur] = [cur, prev];
  }
  return prev[n];
}
const cjk = s => (s.match(/[\u4e00-\u9fa5]/g) || []).join('');

// ── OCR：走常驻 RapidOCR（带坐标），失败时 rike-ocr.sh 自己回退 tesseract ──
function ocrRows(roi) {
  let out = '';
  try {
    out = execFileSync('sh', [OCR, '--tsv', '--roi', roi, img],
      { encoding: 'utf8', timeout: 90000, maxBuffer: 8 << 20, stdio: ['ignore', 'pipe', 'ignore'] });
  } catch (e) { return []; }
  const rows = [];
  for (const ln of out.split('\n')) {
    if (!ln.trim()) continue;
    const p = ln.split('\t');
    if (p.length >= 6 && !isNaN(+p[1])) rows.push({ text: p[0], x1: +p[1], y1: +p[2], x2: +p[3], y2: +p[4] });
    else rows.push({ text: ln.trim(), x1: -1 });       // 兜底纯文本（tesseract 分支）
  }
  return rows;
}
const cardCx = i => CARD_X0 + CARD_W / 2 + STEP * (i - 1);   // 卡 i 标题中心 x（547 + 141*(i-1)）
function titleParts() {
  const rows = ocrRows(TITLE_ROI);
  if (!rows.length) return ['', '', '', '', ''];
  if (rows.some(r => r.x1 >= 0)) {                    // 正常路径：按坐标归位
    const per = ['', '', '', '', ''];
    for (const r of rows) {
      if (r.x1 < 0) continue;
      const cx = (r.x1 + r.x2) / 2;
      let best = -1, bd = 1e9;
      for (let i = 1; i <= 5; i++) { const d = Math.abs(cx - cardCx(i)); if (d < bd) { bd = d; best = i; } }
      if (bd <= 70) per[best - 1] += r.text;
    }
    if (per.some(t => cjk(t))) return per.map(cjk);
  }
  // 兜底：整行文本按 | 切
  return rows.map(r => r.text).join(' ').split(/[|｜]/).map(cjk);
}

function ocrLine(roi) {                    // 描述/单卡兜底：只要一行文本
  const rows = ocrRows(roi);
  return cjk(rows.map(r => r.text).join(' '));
}

// 最近的候选卡名 + 距离；dist 越小越可信
function bestName(text) {
  let name = '', dist = 99;
  for (const c of CANDIDATES) { const d = lev(c, text); if (d < dist) { dist = d; name = c; } }
  return { name, dist };
}
const okDist = n => Math.max(1, Math.floor(n.length / 2));

// ── 卡片状态：一次 convert 出 5 张卡的金色像素数 ──────────────
function goldPixelsAll() {
  const cmd = `convert ${JSON.stringify(img)} -crop ${GOLD_ROW} +repage txt:- 2>/dev/null `
    + `| tail -n +2 | sed 's/[:,#()]/ /g' `
    + `| awk '{r=$3;g=$4;b=$5; if(r>235&&g>200&&b<130){s=int($1/${STEP})+1; if(s<=5) c[s]++}} `
    + `END{for(i=1;i<=5;i++) printf "%d ", c[i]+0}'`;
  try {
    const out = execFileSync('bash', ['-c', cmd], { encoding: 'utf8', timeout: 30000 });
    const v = String(out).trim().split(/\s+/).map(n => parseInt(n, 10));
    return v.length === 5 && v.every(n => !isNaN(n)) ? v : [-1, -1, -1, -1, -1];
  } catch (e) { return [-1, -1, -1, -1, -1]; }
}
function stateOf(n) {
  if (n < 0) return '?';
  if (n <= 25) return '待做';
  if (n <= 250) return '领取';
  return '已领';
}

const parts = titleParts();
const gold = goldPixelsAll();
if (process.env.RK_DEBUG) console.error('DEBUG parts=' + JSON.stringify(parts) + ' gold=' + JSON.stringify(gold));

const rows = [];
for (let i = 1; i <= 5; i++) {
  const rx = REFRESH_X0 + STEP * (i - 1);
  let text = parts[i - 1] || '';
  let { name, dist } = bestName(text);
  let src = '标题';
  if (dist > okDist(name)) {                          // ① 读不出 → 退回描述
    const d = ocrLine(`${rx - 130},402,180,50`);
    const r2 = bestName(d);
    if (r2.dist < dist) { text = d; name = r2.name; dist = r2.dist; src = '描述'; }
  }
  if (dist > okDist(name)) {                          // ② 还读不出 → 单独裁这张卡的标题再读
    const one = ocrLine(`${rx - 140},362,140,45`);
    const r3 = bestName(one);
    if (r3.dist < dist) { text = one; name = r3.name; dist = r3.dist; src = '单卡'; }
  }
  const ok = dist <= okDist(name);
  rows.push({ i, name: ok ? name : (text ? text.slice(0, 10) + '?' : '(读不出)'),
              target: ok && TARGETS.includes(name), x: rx, src, state: stateOf(gold[i - 1]) });
}
for (const r of rows) console.log([r.i, r.name, r.target ? 'yes' : 'no', r.x, r.src, r.state].join('\t'));

// ── 界面健康检查：一张卡名都认不出 → 多半不在青崖书（或还没刷新完）──
const named = rows.filter(r => !/读不出|\?$/.test(r.name)).length;
if (named === 0) {
  if (rows.every(r => r.state === '已领')) {
    console.error('ℹ️ 5 张卡全是「已领」—— 今天这个角色的日课看起来已经做完了（别再刷卡）。');
    process.exit(4);
  }
  console.error('⚠️ 5 张卡名一个都没认出来 —— 当前多半不是青崖书界面（或卡片还没渲染完），别据此刷卡！');
  process.exit(3);
}
