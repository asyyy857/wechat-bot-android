#!/usr/bin/env node
/**
 * ocr-click.cjs —— 零成本"识图 → 操作"闭环：本机 OCR 拿坐标，再点它
 *   用法: node ocr-click.cjs <图片> <目标文字> [--tap <屏索引>] [--dry]
 *         node ocr-click.cjs -i <屏索引> <目标文字>   （等价 --shot <屏索引>）
 *   原理: tesseract tsv 输出每个词的包围盒 → 先按"行"匹配（中文常被拆词），
 *         再按"词"匹配 → 取包围盒中心 → 调 vscreen tap
 */
const { execFileSync } = require('child_process')
const fs = require('fs')
const ROOT = process.env.DSH_ROOT || '/sdcard/dsh'
const VS = ROOT + '/tools/vscreen'
const args = process.argv.slice(2)
const shotIx = args.includes('--shot') ? args[args.indexOf('--shot') + 1] : (args.includes('-i') ? args[args.indexOf('-i') + 1] : null)
const tapArg = args.includes('--tap') ? args[args.indexOf('--tap') + 1] : null
const tapIx = tapArg !== null ? tapArg : shotIx
const dry = args.includes('--dry')
const positional = args.filter((a, i) => !a.startsWith('-') && !['--shot', '--tap', '-i'].includes(args[i - 1]))
const img = positional[0], target = positional[1]
if (!img || !target) {
  console.log('用法: node ocr-click.cjs <图片> <目标文字> [--tap 屏索引] [--dry]\n       node ocr-click.cjs -i <屏索引> <目标文字>')
  process.exit(2)
}
if (shotIx !== null) {
  // ⚠️ 2026-10-05：先删旧图、抓屏失败就**直接中止**。
  //    旧写法失败只打一行警告继续往下走，两种坏结果：
  //      ① img 不存在 → 下面 tesseract 抛未捕获异常，整个脚本崩掉；
  //      ② img 是上次的残留 → 对**旧画面** OCR，拿到旧坐标，然后真的点下去。
  try { fs.rmSync(img, { force: true }) } catch (e) {}
  try { execFileSync('node', [VS, '-i', shotIx, 'shot', img], { timeout: 40000 }) }
  catch (e) { console.log('  ❌ 抓屏失败，已中止：' + String(e.message).slice(0, 60)); process.exit(1) }
  if (!fs.existsSync(img)) { console.log('  ❌ 抓屏没有产出文件，已中止'); process.exit(1) }
}
const tsv = execFileSync('tesseract', [img, 'stdout', '-l', 'chi_sim+eng', 'tsv'], { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 })
const rows = tsv.split('\n').slice(1).map(l => l.split('\t')).filter(c => c.length >= 12 && c[11].trim())
const words = rows.map(c => ({ line: c[1] + '.' + c[2] + '.' + c[3] + '.' + c[4], text: c[11].trim(), x: +c[6], y: +c[7], w: +c[8], h: +c[9], conf: +c[10] }))
const lines = new Map()
for (const w of words) { const k = w.line; if (!lines.has(k)) lines.set(k, []); lines.get(k).push(w) }
const box = (ws) => ({ x: Math.min(...ws.map(w => w.x)), y: Math.min(...ws.map(w => w.y)),
  x2: Math.max(...ws.map(w => w.x + w.w)), y2: Math.max(...ws.map(w => w.y + w.h)), text: ws.map(w => w.text).join('') })
let hit = null, how = ''
for (const ws of lines.values()) { const b = box(ws); if (b.text.includes(target)) { hit = b; how = '行匹配'; break } }
if (!hit) for (const w of words) if (w.text.includes(target) || target.includes(w.text)) { hit = box([w]); how = '词匹配'; break }
console.log('  OCR: ' + words.length + ' 个词 / ' + lines.size + ' 行（' + img + '）')
if (!hit) { console.log('  ❌ 没找到「' + target + '」'); process.exit(1) }
const cx = Math.round((hit.x + hit.x2) / 2), cy = Math.round((hit.y + hit.y2) / 2)
console.log('  ✅ ' + how + '：「' + hit.text + '」 包围盒[' + hit.x + ',' + hit.y + ']-[' + hit.x2 + ',' + hit.y2 + '] → 点击中心 (' + cx + ',' + cy + ')')
if (dry || !tapIx) { console.log('  （--dry 或未给 --tap，未执行点击）'); process.exit(0) }
try {
  const out = execFileSync('node', [VS, '-i', tapIx, 'tap', String(cx), String(cy)], { encoding: 'utf8', timeout: 30000 })
  console.log('  点击执行：', String(out).trim().slice(0, 120))
} catch (e) { console.log('  ⚠️ 点击出错：', e.message.slice(0, 80)) }
