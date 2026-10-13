#!/bin/sh
# fix-vscreen-ports.sh —— 把 App 分发的 dsh-vscreen 插件端口改成本机实际端口
#
# 背景（2026-10-10 App 0.4.0 更新后实测）：
#   App 这次新带进来的 dsh-vscreen 1.1.0 把端口**写死**成 18240/18242 —— 那正是本机
#   被「失效端口绑定」劫持的坏端口（TCP 连得上但永不回数据），10-03 起已换成
#   env.sh 里的 18250（vs）/ 18252（shd）。插件不读环境变量，只能改源码。
#   症状：8 个 vscreen_* 工具全部报「连不上 127.0.0.1:18240」。
#
# 幂等：已经是目标端口就什么都不做。
# ⚠️ App 每次更新都会重新解包插件、把端口覆盖回去 —— 所以 恢复.sh 每次开机都调本脚本。
set -e
. /sdcard/dsh/env.sh 2>/dev/null || true
VSD=${VSD_PORT:-18250}
SHD=${SHD_PORT:-18252}
python3 - "$VSD" "$SHD" <<'PY'
import glob, os, re, sys, time
vsd, shd = sys.argv[1], sys.argv[2]
files = glob.glob('/root/.dsh/profiles/*/node_modules/dsh-vscreen/index.js')
if not files:
    print('   ⚠️ 没找到 dsh-vscreen 插件（未安装？）'); sys.exit(0)
bak_dir = '/sdcard/dsh/backups'
os.makedirs(bak_dir, exist_ok=True)
for f in files:
    s = open(f, encoding='utf-8').read()
    new = re.sub(r'(const PORT_VSD = )\d+', lambda m: m.group(1) + vsd, s)
    new = re.sub(r'(const PORT_SHD = )\d+', lambda m: m.group(1) + shd, new)
    if new == s:
        m = re.search(r'const PORT_VSD = (\d+)', s)
        if m and m.group(1) == vsd:
            print('   ✓ %s 已是 %s/%s' % (f, vsd, shd))
        else:
            print('   ⚠️ %s 没找到端口常量 —— App 可能改了写法，需人工看'
                  % f)
        continue
    # ★ 不原地改：pnpm 的 node_modules 文件是到 store 的硬链接，原地写会污染 store。
    #   先备份，再删掉重建成独立文件。
    bak = os.path.join(bak_dir, 'vscreen-index.js.bak-%s-端口前'
                       % time.strftime('%Y%m%d-%H%M%S'))
    open(bak, 'w', encoding='utf-8').write(s)
    os.remove(f)
    open(f, 'w', encoding='utf-8').write(new)
    print('   ✔ 已改 %s → VSD=%s SHD=%s（原文件备份 %s）' % (f, vsd, shd, bak))
PY
