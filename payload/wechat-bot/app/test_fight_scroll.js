// 实战台消息列表滚动行为测试（自己搭一个最小 DOM 桩，不依赖 jsdom）
// 回归测试：实战台消息列表的滚动行为（自动跟到最新 + 翻历史时不被顶走）
// 跑法：面板在跑的前提下 `node test_fight_scroll.js`
const { execFileSync } = require('child_process');
const fs = require('fs');
let page;
try {
  page = execFileSync('curl', ['-s', 'http://127.0.0.1:8790/']).toString();
} catch (e) {
  page = fs.readFileSync('/tmp/panel.html', 'utf8');
}
if (!page || page.indexOf('fightlist') < 0) throw new Error('拿不到面板页面（面板没跑？）');
const blocks = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const script = blocks.filter((b) => b.indexOf('fightlist') >= 0).pop();
if (!script) throw new Error('没找到实战台脚本块');

const CLIENT = 200, ROWH = 45, MAXROWS = 12;

function makeEl() {
  const el = {
    style: { display: '' },
    textContent: '',
    value: '',
    disabled: false,
    _html: '',
    _top: 0,
    _onScroll: null,
    _onClick: null,
    clientHeight: CLIENT,
    get innerHTML() { return this._html; },
    set innerHTML(v) {
      this._html = v;
      const full = Math.min(MAXROWS, (String(v).match(/class="simrow"/g) || []).length);
      // ★ 2026-10-10：`_lag` 用来模拟真机首屏「innerHTML 插完了、字体/折行还在回流」——
      //   高度先只算一半，120ms 后才长到全高（回归「一打开停在最早那条」那个 bug）。
      this._rows = this._lag ? Math.floor(full / 2) : full;
      if (this._lag) setTimeout(() => { this._rows = full; }, 120);
    },
    get scrollHeight() { return (this._rows || 0) * ROWH; },
    get scrollTop() { return this._top; },
    set scrollTop(v) {
      const max = Math.max(0, this.scrollHeight - this.clientHeight);
      const nv = Math.max(0, Math.min(v, max));
      this._top = nv;
      if (this._onScroll) this._onScroll({});
    },
    addEventListener(t, f) {
      if (t === 'scroll') this._onScroll = f;
      if (t === 'click') this._onClick = f;
      if (t === 'touchstart') this._onTouch = f;
    },
    querySelectorAll(sel) {
      if (sel !== '.simrow') return [];
      return Array.from({ length: this._rows || 0 }, (_, i) => ({
        getAttribute: () => String(i), addEventListener() {},
      }));
    },
  };
  return el;
}

const els = {};
const ids = ['fightlist', 'fightjump', 'fightmeta', 'fightsent', 'fighttext',
             'fightselrow', 'fightwho', 'fightsel', 'fightkind', 'fighthint',
             'fightagain', 'fightclear', 'fightsend'];
for (const id of ids) els[id] = makeEl();

const document = {
  getElementById: (id) => els[id] || (els[id] = makeEl()),
  activeElement: null,
  hidden: false,
  addEventListener() {},
};
// ★ 2026-10-10：面板脚本会用到 window / requestAnimationFrame（钉底与"持续钉底"窗口），
//   桩里必须给全，否则整段脚本在 new Function 里第一句 window.addEventListener 就抛
//   ReferenceError —— 这个测试此前就是这么失修的（跑不起来 = 等于没有回归保护）。
const window = {
  requestAnimationFrame: (fn) => setTimeout(fn, 16),
  addEventListener() {},
};
const requestAnimationFrame = window.requestAnimationFrame;

let payload = { pickable: [], total: 0, seen: Math.floor(Date.now() / 1000) };
const fetch = () => Promise.resolve({ json: () => Promise.resolve(payload) });
let intervalFn = null;
const setInterval = (fn) => { intervalFn = fn; return 1; };

new Function('document', 'window', 'requestAnimationFrame', 'fetch', 'setInterval',
             'alert', 'confirm', script)(
  document, window, requestAnimationFrame, fetch, setInterval, () => {}, () => true);

const flush = () => new Promise((r) => setTimeout(r, 10));
const maxTop = () => els.fightlist.scrollHeight - els.fightlist.clientHeight;
const pick = (n) => Array.from({ length: n }, (_, i) => ({ who: 'U' + i, text: 'm' + i }));

function check(name, cond, extra) {
  console.log((cond ? '✅ ' : '❌ ') + name + (extra !== undefined ? '  [' + extra + ']' : ''));
  if (!cond) process.exitCode = 1;
}

(async () => {
  // ① 首次加载：最新 3 条 → 应当自动滚到最新（底部）
  payload = { pickable: pick(8), total: 8, seen: Math.floor(Date.now() / 1000) };
  intervalFn();
  await flush();
  check('首次渲染自动滚到最新', els.fightlist.scrollTop === maxTop() && maxTop() > 0,
        'scrollTop=' + els.fightlist.scrollTop + ' max=' + maxTop());
  check('贴底时不显示角标', els.fightjump.style.display === 'none', els.fightjump.style.display);

  // ② 用户在底部，来了新消息 → 应当继续跟着滚到最新
  payload = { pickable: pick(9), total: 9, seen: Math.floor(Date.now() / 1000) };
  intervalFn();
  await flush();
  check('贴底时新消息自动跟随', els.fightlist.scrollTop === maxTop() && maxTop() > 0,
        'scrollTop=' + els.fightlist.scrollTop + ' max=' + maxTop());

  // ③ 用户手动往上翻历史，来了新消息 → 位置不动 + 出现「N 条新消息」角标
  //    注：真机上"用户上滑"一定先有 touchstart —— 脚本借它取消"持续钉底"窗口
  //    （2026-10-10 加的），所以这里也必须先派一次，否则测的不是真实路径。
  els.fightlist._onTouch({});
  els.fightlist.scrollTop = 0;
  check('上滑后脱离底部', els.fightlist.scrollTop === 0, els.fightlist.scrollTop);
  payload = { pickable: pick(10), total: 10, seen: Math.floor(Date.now() / 1000) };
  intervalFn();
  await flush();
  check('翻历史时不把用户顶走', els.fightlist.scrollTop === 0, els.fightlist.scrollTop);
  check('角标提示新消息', els.fightjump.style.display === 'block'
        && /1 条新消息/.test(els.fightjump.textContent), els.fightjump.textContent);

  // ④ 点角标 → 回到底部、角标消失
  els.fightjump._onClick({});
  check('点角标回到底部', els.fightlist.scrollTop === maxTop(), els.fightlist.scrollTop);
  check('回到底部后角标隐藏', els.fightjump.style.display === 'none', els.fightjump.style.display);

  // ⑤ ★ 2026-10-10 用户报的那个 bug：「一打开实战台，先停在最早那条，
  //    过一会儿才到最新」。首屏 innerHTML 插完时高度只算一半（字体/折行还在回流），
  //    之后才长到全高 —— 而 scrollHeight 事后变大**不触发 scroll 事件**，
  //    旧代码那 4 下点钉全落在布局稳定之前 → 视觉上停在顶部。
  //    现在列表重建时会开 2 秒「持续钉底」窗口，必须仍然到底。
  els.fightlist._lag = true;
  payload = { pickable: pick(20), total: 20, seen: Math.floor(Date.now() / 1000) };
  intervalFn();
  await flush();
  const halfTop = els.fightlist.scrollTop;          // 此刻只钉到"半高"的底
  await new Promise((r) => setTimeout(r, 400));     // 等回流 + 持续钉底追上
  check('首屏布局晚一步稳也能钉到最新',
        els.fightlist.scrollTop === maxTop() && maxTop() > 0,
        '半高时=' + halfTop + ' 400ms后=' + els.fightlist.scrollTop + ' max=' + maxTop());

  // ⑥ 持续钉底窗口期内用户自己上滑 → 必须立刻让位，不被拽回底部
  els.fightlist._lag = false;
  payload = { pickable: pick(21), total: 21, seen: Math.floor(Date.now() / 1000) };
  intervalFn();
  await flush();
  check('重建后先钉到底', els.fightlist.scrollTop === maxTop(), els.fightlist.scrollTop);
  els.fightlist._onTouch({});                       // touchstart → 取消持续钉底
  els.fightlist.scrollTop = 0;                      // 用户滑到最上面看历史
  await new Promise((r) => setTimeout(r, 250));
  check('窗口期内用户上滑不被拽回底部', els.fightlist.scrollTop === 0,
        els.fightlist.scrollTop);
})();
