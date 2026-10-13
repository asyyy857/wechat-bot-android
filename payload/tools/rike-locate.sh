#!/bin/sh
# rike-locate.sh —— 通用定位器：**不背坐标，每次现找**
#
#   为什么需要它（2026-10-08 结论）：
#     game UI 是「自适应布局」——容器里内容一变，里面的元素就整体位移：
#       · 庄园左菜单：项数 5→6 时整体上移半个行距(28px)（实测 272→243）
#       · 朋友圈：帖子多一张图，互动行整体下移（实测 559→580）
#       · 游历事件卡：「前往」跟着地图上的银杏图标跑（实测 x 468~880，跨 412px）
#     所以这类元素**不存在"正确坐标"**，每天总有一两次撞上 → 必须每次现找。
#
#   用法:
#     sh rike-locate.sh blue  [帧图]   找蓝色菱形图标（银杏/战斗）→ "x y 像素数"
#                                      ⚠️ 复杂背景（手绘地图）噪声大，**只当候选**，必须裁图确认
#     sh rike-locate.sh gold  [帧图]   找金色横条按钮 → ⚠️ 同样只当候选（会撞上底部金色装饰带）
#     sh rike-locate.sh goldy [帧图] [卡中心x] [卡中心x] ...  → 每张卡「领取」金色文字的 y
#     sh rike-locate.sh likerow [帧图]  → 朋友圈第一条帖子互动行的 "👍x y"（x 恒 307；★2026-10-10 换 RapidOCR）
#   不给帧图时自己去副屏抓一帧。
. "$(dirname "$0")/../env.sh"
SLOT=${RIKE_SLOT:-1}
CMD=$1; shift
IMG=$1
if [ -z "$IMG" ] || [ ! -f "$IMG" ]; then
  IMG=/tmp/rike-locate.jpg
  rm -f "$IMG"
  node "$DSH_ROOT/tools/vscreen" -i "$SLOT" shot "$IMG" >/dev/null 2>&1 || { echo "❌ 抓帧失败"; exit 1; }
else
  shift
fi

case "$CMD" in
  blue)
    # 深蓝底 + 白图案的菱形（银杏/战斗）：B 明显大于 R 和 G
    convert "$IMG" -resize 50% txt:- 2>/dev/null | tail -n +2 | sed 's/[:,#()]/ /g' \
      | awk '{x=$1;y=$2;r=$3;g=$4;b=$5;
              if (b>g && g>r && r<100 && b>95) { k=int(x/26)"_"int(y/26); sx[k]+=x; sy[k]+=y; n[k]++ }}
             END{ for(k in n) if(n[k]>6) printf "%d %d %d\n", sx[k]/n[k]*2, sy[k]/n[k]*2, n[k] }' \
      | sort -k3 -nr
    ;;
  gold)
    # 金色横条（游历事件卡的「前往」按钮）：整行都偏金，且横向很宽
    convert "$IMG" -resize 50% txt:- 2>/dev/null | tail -n +2 | sed 's/[:,#()]/ /g' \
      | awk '{x=$1;y=$2;r=$3;g=$4;b=$5;
              if (r>200 && g>160 && b<130) { if(!(y in mn)||x<mn[y])mn[y]=x; if(x>mx[y])mx[y]=x; n[y]++ }}
             END{ # 按钮是"实心横条"：要求连续 >=6 行都宽 >100（resize50% 后）
                  best=-1; bw=0; run=0; starty=0;
                  for(y=0; y<320; y++){
                    w = (y in mx) ? mx[y]-mn[y] : 0;
                    if (w>100 && n[y]>25) { if(run==0) starty=y; run++; if(run>=6 && w>bw){bw=w; best=starty+int(run/2)} }
                    else run=0;
                  }
                  if(best>=0) printf "%d %d %d\n", (mn[best]+mx[best]), best*2, bw*2 }'
    ;;
  likerow)
    # 朋友圈「第一条帖子的互动行」（👍 💬 🔄 … 那一排图标+数字）的 y。
    # ★ 为什么要它：互动行的 y **完全跟着帖子高度走**。2026-10-09 实测第一条帖子只写了
    #   "好吧"+3 张图时，互动行在 **y≈270**；而老脚本的搜索窗口是 y 490~630
    #   （依据的是历史上"帖子较长"的样本 559/570/580）→ 必然点空，还会把
    #   第二条帖子的图片当成"👍 变青"而误报成功（当天真踩了）。
    # 判据：互动行是整条帖子里**唯一一行"同一行挤着 ≥2 个纯数字"**的
    #   （点赞数/评论数/转发数…；时间行只有 1 个数字、正文行没有）→
    #   裁帖子区放大 2 倍 OCR，按 y 分桶，取**最上面**那个装 ≥2 个数字的桶。
    # 输出: "👍x y"（x 取实测区间 305~309 的中值 307，只让 OCR 负责 y）
    # ⚠️ 裁剪从 y=110 起（不是 95）：95~105 那一带是面板顶边，OCR 会把边缘文字
    #    拆成 "15"/"1" 这种碎片数字，凑够 2 个就把最上面的桶抢走（2026-10-09 实测踩过，
    #    于是返回 y=114 这种鬼值）。上移到 110 之后两帧噪声全部消失。
    # ★ 2026-10-09：**跑两种裁剪高度投票**。tesseract 的版面分析对裁剪尺寸敏感
    #   （实测同一帧换个高度就能从"读出 3 个数字"变成"只读出 1 个"，于是桶判据落空），
    #   单跑一次经常给出离谱值。两次结果接近（<=25px）才认为可信；差太多就交回上层候选集。
    _likerow_one() {
      # ★ 2026-10-10：tesseract tsv → RapidOCR tsv。原来是「裁 530xN → 放大 200% → tesseract tsv」，
      #   现在直接把 ROI 交给常驻服务（不落盘、不放大），坐标就是原图坐标（不用 /2）。
      sh "$DSH_ROOT/tools/rike-ocr.sh" --tsv --roi "280,110,530,$1" "$IMG" 2>/dev/null | awk -F'\t' '
        $1 ~ /^[0-9]{1,6}$/ {
          b=int($3/60); cnt[b]++; sumy[b]+=($3+$5)/2
          if (!(b in mnx) || $2<mnx[b]) mnx[b]=$2
          if ($4>mxx[b]) mxx[b]=$4
          if (!(b in mnt) || $3<mnt[b]) mnt[b]=$3
          if ($5>mxt[b]) mxt[b]=$5
        }
        END {
          best=-1
          # 判据②：这排数字必须**横向分散**（极差>=150）—— 互动行的三个数各自躺在图标右边；
          # 判据③：桶内数字的 top 跨度 <=25 —— 真正的互动行在同一水平线上；
          # ⚠️ 判据④：b+0 强制数值比较（mawk 数组下标是字符串，"14" < "5" 会成立）。
          for (b in cnt) if (cnt[b]>=2 && (mxx[b]-mnx[b])>=150 && (mxt[b]-mnt[b])<=25) { bn=b+0; if (best==-1 || bn<best) best=bn }
          if (best==-1) exit 1
          printf "%d", sumy[best]/cnt[best]
        }'
    }
    RA=$(_likerow_one 520); RB=$(_likerow_one 460)
    if [ -n "$RA" ] && [ -n "$RB" ]; then
      DF=$((RA>RB ? RA-RB : RB-RA))
      if [ "$DF" -le 25 ]; then echo "307 $(( (RA+RB)/2 ))"; else echo "307 $RA"; fi
    elif [ -n "$RA" ]; then echo "307 $RA"
    elif [ -n "$RB" ]; then echo "307 $RB"
    else exit 1
    fi
    ;;
  goldy)
    # 卡片「领取」金色大字的 y（每张卡一段）：给若干卡中心 x
    [ $# -ge 1 ] || { echo "用法: rike-locate.sh goldy <帧图> <卡中心x> ..."; exit 2; }
    for cx in "$@"; do
      y=$(convert "$IMG" -crop 140x120+$((cx-70))+400 +repage txt:- 2>/dev/null | tail -n +2 | sed 's/[:,#()]/ /g' \
          | awk '{yy=$2;r=$3;g=$4;b=$5; if(r>235&&g>200&&b<130) c[yy]++}
                 END{best=-1;bn=0; for(y in c) if(c[y]>bn){bn=c[y];best=y} if(best>=0) print best+400+15}')
      printf "x=%s → 领取y=%s\n" "$cx" "${y:-未找到}"
    done
    ;;
  *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
