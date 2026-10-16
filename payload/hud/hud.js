#!/opt/node-v22.23.2-linux-arm64/bin/node
/*
 * hud —— 把 agent 此刻在干什么写到手机屏幕的浮窗上。
 *
 *   hud "正在打开微信"
 *   hud -d "点底部第一个会话" "给张三发消息"
 *   hud -t "逆水寒日课" -d "第 2/4 个任务" "自动跑任务"
 *   hud --status | --hide | --show | --off
 *   hud --alarm -t "标题" -d "正文"   # 抢屏提醒：能弹横幅的通知 + 震动
 *   hud --alarmoff                    # 收回那条抢屏提醒
 *   hud --cost [--at 毫秒时间戳]   # 现在是波峰(全价)还是波谷(半价)
 *
 * 浮窗没在跑时会自动拉起来（先走命令守护，再退到 adb）。
 */
'use strict';
const fs = require('fs');
const { execFileSync } = require('child_process');

const BASE = 'http://127.0.0.1:18787';
const BOOT = 'am start -n com.dsh.hud/.BootActivity';

function curl(args, input) {
  try {
    return execFileSync('curl', ['-s', '-m', '6'].concat(args),
      { input: input, encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }).trim();
  } catch (e) { return ''; }
}

function sleep(ms) { try { execFileSync('sleep', [String(ms / 1000)]); } catch (e) { } }

function ping() { return curl([BASE + '/ping']); }

function boot() {
  const tries = [['sh', ['/sdcard/dsh/tools/shd.sh', BOOT]]];
  try {
    const serial = fs.readFileSync('/sdcard/dsh/.adb-serial', 'utf8').trim();
    if (serial) tries.push(['adb', ['-s', serial, 'shell', 'am', 'start', '-n', 'com.dsh.hud/.BootActivity']]);
  } catch (e) { }
  for (const t of tries) {
    try { execFileSync(t[0], t[1], { timeout: 25000, stdio: 'ignore' }); } catch (e) { continue; }
    for (let i = 0; i < 14; i++) {
      if (ping()) return true;
      sleep(500);
    }
  }
  return false;
}

const argv = process.argv.slice(2);
let title = null, detail = null, mode = 'say', linger = 0, done = false, buzz = false, at = '';
let alarm = false, alarmOff = false;
const words = [];
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === '-t' || a === '--title') title = argv[++i];
  else if (a === '-d' || a === '--detail') detail = argv[++i];
  else if (a === '--linger') linger = parseFloat(argv[++i]) || 0;
  else if (a === '--done') done = true;
  else if (a === '--buzz') buzz = true;
  else if (a === '--alarm') alarm = true;
  else if (a === '--alarmoff') alarmOff = true;
  else if (a === '--cost') mode = 'cost';
  else if (a === '--at') at = argv[++i] || '';
  else if (a === '--hide') mode = 'hide';
  else if (a === '--show') mode = 'show';
  else if (a === '--status') mode = 'status';
  else if (a === '--off') mode = 'off';
  else if (a === '-h' || a === '--help') mode = 'help';
  else words.push(a);
}

if (mode === 'help') {
  process.stdout.write(fs.readFileSync(__filename, 'utf8').split('*/')[0].replace(/^[\s\S]*?\/\*/, ''));
  process.exit(0);
}

function oneLine(s) { return s == null ? null : String(s).replace(/[\r\n]+/g, ' '); }

if (mode === 'status') {
  const st = ping();
  console.log(st || '浮窗未运行');
  process.exit(st ? 0 : 1);
}

if (!ping() && !boot()) {
  console.error('浮窗没起来（App 是否还装着？试试 sh /sdcard/dsh/hud/install.sh）');
  process.exit(1);
}

// ★ 2026-10-09（用户要求）：--cost 问「现在是波峰（全价）还是波谷（半价）」。
//   判定逻辑写在浮窗 App 里（HudService.isOffPeak），这里只转发 —— 保证命令行
//   查到的和浮窗上显示的永远是同一个答案。--at 可问任意时刻（毫秒时间戳）。
if (mode === 'cost') {
  const out = curl([BASE + '/cost' + (at ? '?at=' + encodeURIComponent(at) : '')]);
  console.log(out || '查询失败：浮窗没回应');
  process.exit(out ? 0 : 1);
}

if (mode === 'hide') { curl([BASE + '/hide']); console.log('已隐藏'); process.exit(0); }
if (mode === 'show') { curl([BASE + '/show']); console.log('已显示'); process.exit(0); }
if (mode === 'off') { curl([BASE + '/quit']); console.log('已停止'); process.exit(0); }

// 注意：空串是有效值（表示"这一行清空"），所以一律用 null 判断，不能用真值判断
const action = words.length > 0 ? oneLine(words.join(' ')) : null;
title = oneLine(title);
detail = oneLine(detail);

// ★ 2026-10-10（用户报障「他在那抢微信，他不告诉我，他没有通知，没有通知我就会
//   乱点」）：--alarm 发一条**能弹横幅**的高优先级通知。桥接原来走的是
//   `adb shell cmd notification post` —— 那是静默渠道，横幅不弹、不响、连屏都不亮，
//   用户等于什么都没收到。这里改由浮窗 App 自己发（见 HudService.alert）。
//   用法：hud --alarm -t "标题" -d "正文"   收回：hud --alarmoff
if (alarmOff) {
  const out = curl([BASE + '/alarmoff']);
  console.log(out || '（抢屏提醒已收回）');
  process.exit(0);
}
if (alarm) {
  const q = BASE + '/alarm?title=' + encodeURIComponent(oneLine(title) || 'DSH 微信机器人')
          + '&text=' + encodeURIComponent(oneLine(detail) || oneLine(words.join(' ')) || '')
          // ★ 2026-10-13（用户要求）：**抢屏提醒不再震动** —— ms/times 传 0，
          //   HudService.alert 见到 0 就整条跳过震动，横幅照弹（见那边的 doVib）。
          + '&ms=0&times=0';
  const out = curl([q]);
  console.log(out || '（抢屏提醒已发出）');
  process.exit(0);
}

// ★ 2026-10-08（用户要求）：--buzz 只震一下，不动浮窗内容。
//   场景：需要用户注意一下、但没必要刷屏（比如"该看一眼了"）。
if (buzz) {
  const out = curl([BASE + '/vibrate?ms=600&times=2']);
  console.log(out || '（震动请求已发）');
  process.exit(0);
}

if (action === null && title === null && detail === null) {
  console.error('用法: hud [-t 标题] [-d 细节] "正在做什么"');
  process.exit(2);
}

// --done：这是「干完了，你可以拿回手机了」的信号，浮窗多停一会儿（30 秒）
if (done && linger <= 0) linger = 30;

const body = [];
if (title !== null) body.push('title=' + title);
if (action !== null) body.push('action=' + action);
if (detail !== null) body.push('detail=' + detail);

const out = curl(['-X', 'POST', '--data-binary', '@-', BASE + '/say'], body.join('\n'));
if (!out) { console.error('推送失败：浮窗没回应'); process.exit(1); }
// --linger N：显示 N 秒后自己收起（干完活时用，别让它一直挂着）
if (linger > 0) curl([BASE + '/hidein?ms=' + Math.round(linger * 1000)]);
console.log(out);

// ★ 2026-10-08（用户要求）：「锁屏之后我怎么知道你完事了」——
//   --done 除了浮窗，再补两样，三样缺一不可：
//     · 浮窗：只在**解锁且亮屏**时看得见；
//     · 通知：锁屏上也显示，但 shell 发的走 `channel=shell_cmd` **静默渠道**
//       （不弹横幅、不响、不震、也不亮屏）；
//     · 所以必须再来一发 `input keyevent 224`（KEYCODE_WAKEUP）把屏幕点亮，
//       否则用户锁着屏，通知躺在那儿他也不知道。
if (done) {
  let serial = '';
  try { serial = fs.readFileSync('/sdcard/dsh/.adb-serial', 'utf8').trim(); } catch (e) { }
  const sh = (args) => {
    try {
      execFileSync('adb', (serial ? ['-s', serial] : []).concat(['shell'], args),
        { timeout: 15000, stdio: 'ignore' });
    } catch (e) { /* 通知失败不能影响主流程 */ }
  };
  const text = words.join(' ') || '已完成';
  sh(['input', 'keyevent', '224']);                       // ① 点亮屏幕
  // ② 通知 —— ★ 2026-10-09 用户要求**删掉**。
  //   他的原话：「Dsh 有一个通知栏通知，现在没有用了…把那个通知删掉吧」，
  //   补充说明「那个通知显示 dshdone」（= 这里的 tag），问怎么处理时他选的是
  //   **彻底不发这类通知**。
  //   为什么敢删：叫他这件事本来就三样兜着 —— 浮窗（亮屏就看得到）+
  //   `input keyevent 224` 点亮屏幕 + 震动（`/vibrate`，只有浮窗 App 做得到）。
  //   而这条通知是 shell 发的（tag=`dshdone`、标题「DSH 任务」），
  //   既不像通知也不像给人看的东西，纯占通知栏。
  //   ⚠️ 想恢复：把下面两行放开，然后 `sh /sdcard/dsh/hud/link.sh` 重装入口。
  // sh(['cmd', 'notification', 'post', '-S', 'bigtext',
  //     '-t', title || 'DSH 任务', 'dshdone', text]);
  // ③ 震动 —— ★ 2026-10-08（用户要求）：shell 发的通知不响不震，锁屏的人只靠
  //    "屏幕亮起 + 通知"未必察觉（手机可能揣在兜里/扣在桌上）。震动是最后一道、
  //    也是最直接的一道。实现在 HudService.vibrate()（App 内，走 VIBRATE 权限）。
  curl([BASE + '/vibrate?ms=600&times=3']);   // 600ms×3：默认振幅太弱，见 HudService 注释
  console.log('（已点亮屏幕 + 震动）');
}
