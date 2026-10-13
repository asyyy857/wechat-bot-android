#!/opt/node-v22.23.2-linux-arm64/bin/node
/**
 * adb-bootstrap.cjs —— 容器重置后，自动把 adb 通道重新拿回来
 *
 * 为什么需要它：
 *   容器 rootfs 一重置，apt 装的 adb 二进制和 /root/.android 里的**配对密钥**就全没了。
 *   密钥没了就必须重新配对（要用户在手机上点「使用配对码配对设备」）——
 *   这才是"每次都要人工配对"的真正原因，不是 adb 本身麻烦。
 *
 * 做法：
 *   1) 密钥持久化在 /run/dsh-phone/adb-keys/（App 私有目录的 bind mount：
 *      容器重置不丢，且别的应用读不到 —— 放 /sdcard 会被其它应用读到，等于把手机的 shell 送人）；
 *   2) 启动时把密钥还原回 /root/.android/，手机仍然认我们，**无需重新配对**；
 *   3) 端口先用上次记下的；不行再扫一遍（无线调试每次重开端口都会变）；
 *   4) adb 二进制不在就自动 apt 装（arm64 必须走 ports.ubuntu.com）。
 *
 * ⚠️ 所有输出只进日志文件，**绝不写 stdout** ——
 *    App 用正则在引擎启动输出里抓界面地址，任何 http://127.0.0.1:<非3080> 都会把页面抢走。
 *
 * 用法: node adb-bootstrap.cjs [--quiet] [--force-scan]
 */
'use strict'

const fs = require('node:fs')
const path = require('node:path')
const net = require('node:net')
const { execFileSync, spawnSync } = require('node:child_process')

const KEY_DIR = '/run/dsh-phone/adb-keys'
const ANDROID_DIR = '/root/.android'
const LOG = '/sdcard/dsh/logs/adb-bootstrap.log'
const STATUS = path.join(KEY_DIR, 'status.json')
const KEY_FILES = ['adbkey', 'adbkey.pub', 'adb_known_hosts.pb']

const argv = process.argv.slice(2)
const QUIET = argv.includes('--quiet')
const FORCE_SCAN = argv.includes('--force-scan')

// 已知「自己人」端口：adb server / DSH Web / 无障碍桥 / 微信桥接 / 面板 / 代理 / 两个守护。
// ★ 2026-10-05：扫描出来的开放端口里这些占绝大多数，对它们做 adb connect 只会制造
//   offline 僵尸 transport（实测一次全端口扫描能留下好几个），而僵尸在时后续 connect
//   更容易失败。直接跳过。
const SKIP_PORTS = new Set([
  5037, 3080, 3099, 8765, 8790, 7890,
  Number(process.env.VSD_PORT) || 18250,
  Number(process.env.SHD_PORT) || 18252,
])

function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}\n`
  try { fs.appendFileSync(LOG, line) } catch { /* 日志写不了不该让引导失败 */ }
  if (!QUIET) { /* 有意不写 stdout */ }
}

function adb(args, timeout = 20000) {
  const r = spawnSync('adb', args, { encoding: 'utf8', timeout, env: { ...process.env, HOME: '/root' } })
  return { code: r.status, out: (r.stdout || '') + (r.stderr || '') }
}

function haveAdb() {
  try { execFileSync('adb', ['version'], { stdio: 'ignore', timeout: 8000 }); return true } catch { return false }
}

/** 清掉所有 offline transport。
 *  ★ 2026-10-05 加：反复扫描会把非 adb 端口 connect 成 offline 僵尸，而僵尸在时新的
 *  connect 更容易失败（connect-adb.sh 2026-10-04 就是这个顺序：先清再连）。 */
function dropOffline() {
  const out = adb(['devices'], 15000).out || ''
  let n = 0
  for (const line of out.split('\n')) {
    const m = line.match(/^(127\.0\.0\.1:\d+)\s+offline/)
    if (m) { adb(['disconnect', m[1]], 8000); n++ }
  }
  if (n) log(`清掉 ${n} 个 offline transport`)
}

/** 自动装 adb：arm64 的索引在 ubuntu-ports，用 x86 归档会 404 在 binary-arm64/Packages */
function installAdb() {
  log('adb 不存在 → 开始安装')
  const src = '/etc/apt/sources.list.d/ubuntu.sources'
  const wanted = `Types: deb
URIs: http://ports.ubuntu.com/ubuntu-ports/
Suites: noble noble-updates noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
`
  try {
    const cur = fs.existsSync(src) ? fs.readFileSync(src, 'utf8') : ''
    if (!cur.includes('ubuntu-ports')) {
      fs.writeFileSync(src, wanted)
      log('已把 apt 源切到 ports.ubuntu.com（arm64）')
    }
    execFileSync('apt-get', ['update', '-qq'], { timeout: 300000, stdio: 'ignore' })
    execFileSync('apt-get', ['install', '-y', '-qq', 'adb'], { timeout: 600000, stdio: 'ignore' })
  } catch (e) {
    log('安装 adb 失败: ' + (e?.message ?? e))
  }
  const ok = haveAdb()
  log('安装结果: ' + (ok ? 'adb 可用' : '仍然没有 adb'))
  return ok
}

function restoreKeys() {
  fs.mkdirSync(ANDROID_DIR, { recursive: true })
  let restored = 0
  for (const f of KEY_FILES) {
    const from = path.join(KEY_DIR, f)
    const to = path.join(ANDROID_DIR, f)
    if (!fs.existsSync(from)) continue
    // 已有且一致就不动；不一致则以持久化副本为准（容器重置后这里是空的）
    try {
      if (fs.existsSync(to) && fs.readFileSync(to).equals(fs.readFileSync(from))) continue
    } catch { /* 读不了就覆盖 */ }
    try { fs.copyFileSync(from, to); fs.chmodSync(to, 0o600); restored++ } catch (e) { log(`还原 ${f} 失败: ${e.message}`) }
  }
  log(`密钥还原: ${restored} 个` + (fs.existsSync(path.join(KEY_DIR, 'adbkey')) ? '（持久化副本存在）' : '（⚠ 没有持久化副本，需要重新配对）'))
  return restored
}

function saveKeys() {
  try {
    fs.mkdirSync(KEY_DIR, { recursive: true })
    for (const f of KEY_FILES) {
      const from = path.join(ANDROID_DIR, f)
      if (fs.existsSync(from)) { fs.copyFileSync(from, path.join(KEY_DIR, f)); fs.chmodSync(path.join(KEY_DIR, f), 0o600) }
    }
    return true
  } catch (e) { log('保存密钥失败: ' + e.message); return false }
}

function isOpen(port, timeout = 700) {
  return new Promise((resolve) => {
    const s = net.connect({ host: '127.0.0.1', port })
    let done = false
    const fin = (v) => { if (done) return; done = true; try { s.destroy() } catch {} ; resolve(v) }
    s.setTimeout(timeout, () => fin(false))
    s.once('connect', () => fin(true))
    s.once('error', () => fin(false))
  })
}

async function scanOpen(from, to, conc = 200) {
  const ports = []
  for (let p = from; p <= to; p++) ports.push(p)
  const open = []
  let i = 0
  await new Promise((resolve) => {
    let active = 0
    const pump = () => {
      while (active < conc && i < ports.length) {
        const p = ports[i++]; active++
        isOpen(p).then((ok) => { if (ok) open.push(p); active--; pump() })
      }
      if (active === 0 && i >= ports.length) resolve()
    }
    pump()
  })
  return open.sort((a, b) => a - b)
}

function deviceConnected(port) {
  const r = adb(['devices'])
  return new RegExp(`127\\.0\\.0\\.1:${port}\\s+device`).test(r.out)
}

function tryPort(port) {
  adb(["connect", `127.0.0.1:${port}`], 20000)
  // connect 返回 ≠ 已经可用：设备要过一会儿才出现在列表里，必须轮询。
  // ★ 2026-10-05：窗口从 12×0.5s=6s 放宽到 40×0.5s=20s —— 实测本机「新 server + 刚从
  //   持久副本还原回来的密钥」首次握手有时要十几秒才变 device，6 秒会误判成端口不可用。
  for (let i = 0; i < 40; i++) {
    if (deviceConnected(port)) return true
    try { execFileSync("sleep", ["0.5"]) } catch {}
  }
  adb(["disconnect", `127.0.0.1:${port}`], 8000)
  return false
}

function lastPort() {
  try { return Number(fs.readFileSync(path.join(KEY_DIR, 'last-port.txt'), 'utf8').trim()) || 0 } catch { return 0 }
}

async function main() {
  log('=== adb-bootstrap 开始 ===')
  if (!haveAdb() && !installAdb()) { finish(false, 'adb 装不上'); return }

  restoreKeys()
  execFileSync('adb', ['start-server'], { stdio: 'ignore', timeout: 30000, env: { ...process.env, HOME: '/root' } })
  saveKeys()   // 首次生成的新密钥（比如刚配对完）也存下来

  const known = lastPort()
  if (known && !FORCE_SCAN) {
    log(`先试上次的端口 ${known}`)
    if (tryPort(known)) { finish(true, `复用端口 ${known}`); return }
    log(`端口 ${known} 不可用，改扫描`)
  }

  // 无线调试端口一般在 30000-49999；先扫常用段，再退到全端口
  for (const [a, b] of [[30000, 49999], [1, 65535]]) {
    log(`扫描 ${a}-${b}`)
    dropOffline()                     // ★ 先清僵尸，再扫
    const open = await scanOpen(a, b)
    log(`开放端口 ${open.length} 个: ${open.join(',')}`)
    for (const p of open) {
      if (SKIP_PORTS.has(p)) continue     // 自己人，跳过（免得 connect 出 offline 僵尸）
      if (tryPort(p)) { finish(true, `扫描命中端口 ${p}`); return }
    }
    // 常用段没命中才退到全端口
    if (a === 30000 && open.length) continue
  }
  finish(false, '没找到 adbd（手机上的「无线调试」是否还开着？）')
}

function finish(ok, detail) {
  log((ok ? '✅ ' : '❌ ') + detail)
  try {
    const port = (adb(['devices']).out.match(/127\.0\.0\.1:(\d+)\s+device/) || [])[1]
    if (ok && port) {
      fs.writeFileSync(path.join(KEY_DIR, 'last-port.txt'), String(port))
      saveKeys()
    }
    fs.writeFileSync(STATUS, JSON.stringify({
      ok, detail, port: port || null, at: new Date().toISOString(),
      device: (adb(['devices', '-l']).out.split('\n').find((l) => /device product/.test(l)) || '').trim() || null,
    }, null, 2))
  } catch (e) { log('写状态失败: ' + e.message) }
  log('=== adb-bootstrap 结束 ===')
}

main().catch((e) => { log('异常: ' + (e?.stack ?? e)); finish(false, '异常: ' + (e?.message ?? e)) })
