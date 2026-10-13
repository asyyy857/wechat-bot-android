#!/usr/bin/env node
/**
 * rike-live.js —— 逆水寒副屏「实时直播页」（★2026-10-10 用户要求）
 *
 * 为什么有它：用户看不见虚拟屏（副屏只在后台渲染）。他要"能看见你在虚拟屏的操作和点击"，
 *   这样才能直接给建议，而不是我在看不见的屏幕上自己摸。
 *
 * 用法：
 *   sh tools/rike-live.sh start          # 后台起服务（推荐）
 *   node tools/rike-live.js [端口]        # 前台跑
 *   手机浏览器打开 http://127.0.0.1:18800
 *
 * 接口：
 *   GET /            直播页（自动刷新帧 + 叠加最近 6 秒内的点击圈）
 *   GET /frame       转发副屏当前帧（JPEG，来自虚拟屏守护 :18250）
 *   GET /state       JSON：最近点击 [{x,y,label,ts}]
 *   POST /click?x=&y=&label=   记一次点击（脚本点击时上报 → 页面画圈）
 *   GET /health      ok
 *
 * 备注：容器与手机共享回环（proot 不改 net ns）→ 手机浏览器能直接开 127.0.0.1:18800。
 */
const http = require('http');
const PORT = +(process.argv[2] || process.env.RIKE_LIVE_PORT || 18800);
const VSD  = +(process.env.VSD_PORT || 18250);
const SLOT = +(process.env.RIKE_SLOT || 1);
const W = 1400, H = 636;          // 副屏逻辑分辨率（画圈用百分比定位）

let clicks = [];

const PAGE = `<!doctype html><html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>逆水寒副屏直播</title>
<style>
  html,body{margin:0;background:#0d0d0d;color:#ddd;font-family:-apple-system,system-ui,sans-serif}
  #bar{padding:6px 10px;font-size:13px;background:#1b1b1b;display:flex;gap:8px;align-items:center}
  #dot{width:9px;height:9px;border-radius:50%;background:#2ecc71;animation:bb 1.2s infinite;flex:0 0 auto}
  @keyframes bb{50%{opacity:.25}}
  #wrap{position:relative;width:100%;line-height:0}
  #f{width:100%;display:block;background:#000}
  .ck{position:absolute;border:2px solid #ff2d2d;border-radius:50%;transform:translate(-50%,-50%);
      pointer-events:none;box-shadow:0 0 8px rgba(255,45,45,.85)}
  .ck:after{content:'';position:absolute;left:50%;top:50%;width:16px;height:2px;background:#ff2d2d;transform:translate(-50%,-50%)}
  .ck:before{content:'';position:absolute;left:50%;top:50%;width:2px;height:16px;background:#ff2d2d;transform:translate(-50%,-50%)}
  .lb{position:absolute;color:#ff6060;font-size:11px;font-weight:700;pointer-events:none;
      text-shadow:0 0 4px #000,0 0 4px #000;white-space:nowrap}
</style></head><body>
<div id="bar"><span id="dot"></span><b>逆水寒 · 副屏实时</b><span id="st">连接中…</span>
<button id="q" style="margin-left:auto;background:#2a2a2a;color:#ccc;border:1px solid #444;border-radius:4px;padding:3px 10px;font-size:12px">标清</button></div>
<div id="wrap"><img id="f" alt=""><div id="ov"></div></div>
<script>
const img=document.getElementById('f'),ov=document.getElementById('ov'),st=document.getElementById('st');
const W=${W},H=${H}; let n=0,fails=0;
const QS=[1400,1000,700], QN=['高清','标清','流畅']; let qi=1;
const qb=document.getElementById('q');
qb.onclick=()=>{ qi=(qi+1)%QS.length; qb.textContent=QN[qi]; };
const cv=document.createElement('canvas'); cv.width=8; cv.height=8;
const cx=cv.getContext('2d');
function isBlack(im){   // 8x8 采样判断是不是纯黑帧（副屏没有应用在渲染时就是这样）
  try{ cx.drawImage(im,0,0,8,8); const d=cx.getImageData(0,0,8,8).data;
       let s=0; for(let i=0;i<d.length;i+=4) s+=d[i]+d[i+1]+d[i+2];
       return s<24; }catch(e){ return false; }
}
function tick(){
  const im=new Image();
  im.onload=()=>{
    img.src=im.src; fails=0; n++;
    if(isBlack(im)) st.textContent='副屏暂无画面（游戏/应用没在前台渲染）· 第 '+n+' 帧';
    else st.textContent='第 '+n+' 帧 · '+new Date().toLocaleTimeString();
  };
  im.onerror=()=>{fails++;st.textContent='抓帧失败 '+fails+' 次';};
  im.src='/frame?w='+QS[qi]+'&t='+Date.now();
}
setInterval(tick,450); tick();
async function poll(){
  try{
    const j=await (await fetch('/state',{cache:'no-store'})).json();
    const now=Date.now()/1000;
    ov.innerHTML='';
    for(const c of j.clicks){
      const age=now-c.ts; if(age>6) continue;
      const op=Math.max(.3,1-age/6);
      const d=document.createElement('div'); d.className='ck';
      d.style.left=(c.x/W*100)+'%'; d.style.top=(c.y/H*100)+'%';
      d.style.width=(64/W*100)+'%'; d.style.height=(64/H*100)+'%'; d.style.opacity=op;
      ov.appendChild(d);
      if(c.label){const l=document.createElement('div'); l.className='lb';
        l.style.left=(Math.min(c.x+38,W-150)/W*100)+'%';
        l.style.top=(Math.max(c.y-46,4)/H*100)+'%';
        l.textContent=c.label; l.style.opacity=op; ov.appendChild(l);}
    }
  }catch(e){}
}
setInterval(poll,300); poll();
</script></body></html>`;

function frame(res, w){
  // ★ 2026-10-10：透传页面要的宽度（虚拟屏守护支持 ?w= 缩放）。
  //   全尺寸 1400×636 每 0.45s 拉一次，浏览器要解码+重绘 —— 手机上是有代价的（发热）。
  //   默认给 1000px 宽（游戏 UI 文字仍看得清），需要抠细节时页面可切回 1400。
  const path = `/${SLOT}/frame` + (w ? `?w=${w}` : '');
  const r = http.get({host:'127.0.0.1',port:VSD,path,timeout:5000}, resp=>{
    if(resp.statusCode!==200){ res.writeHead(502); res.end('vsd '+resp.statusCode); resp.resume(); return; }
    res.writeHead(200,{'Content-Type':'image/jpeg','Cache-Control':'no-store'});
    resp.pipe(res);
  });
  r.on('error',e=>{ try{res.writeHead(502);res.end('err '+e.message);}catch(_){} });
  r.on('timeout',()=>{ r.destroy(); try{res.writeHead(504);res.end('timeout');}catch(_){} });
}

http.createServer((req,res)=>{
  const u = new URL(req.url,'http://127.0.0.1');
  if(u.pathname==='/'||u.pathname==='/index.html'){
    res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store'});
    return res.end(PAGE);
  }
  if(u.pathname==='/frame')  return frame(res, u.searchParams.get('w'));
  if(u.pathname==='/health'){ res.writeHead(200); return res.end('ok clicks='+clicks.length); }
  if(u.pathname==='/state'){
    const now=Date.now()/1000;
    clicks = clicks.filter(c=>now-c.ts<=8).slice(-20);
    res.writeHead(200,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'});
    return res.end(JSON.stringify({clicks}));
  }
  if(u.pathname==='/click'){
    // ★ 标签一定要走 JSON body：HTTP 请求行不能含非 ASCII 字节，
    //   `?label=日课` 这种未编码中文会被 http parser 直接丢掉
    //   （2026-10-10 实测：带中文的 query 上报进来就没了，纯 ASCII 才成功）。
    let body='';
    req.on('data',c=>{ body+=c; if(body.length>4096) req.destroy(); });
    req.on('end',()=>{
      let o={};
      try{ o=JSON.parse(body||'{}'); }catch(e){ o={}; }
      const x=+(o.x!==undefined?o.x:u.searchParams.get('x'));
      const y=+(o.y!==undefined?o.y:u.searchParams.get('y'));
      const label=o.label||u.searchParams.get('label')||'';
      if(!isNaN(x)&&!isNaN(y)) clicks.push({x,y,label,ts:Date.now()/1000});
      res.writeHead(204); res.end();
    });
    return;
  }
  res.writeHead(404); res.end('no');
}).listen(PORT,'127.0.0.1',()=>{
  console.log('逆水寒副屏直播: http://127.0.0.1:'+PORT+'   (帧来自 vsd:'+VSD+' slot '+SLOT+')');
});
