# 虚拟屏帧率（2026-10-09 锁上）

## 目的
控制发热/功耗。改之前虚拟屏固定 60fps，微信**空闲**也在持续产出约 19.7fps
（7×24 常驻），是持续的合成开销。

## 现状（改完）
| 屏 | 用途 | 分辨率@密度 | 帧率 | 空闲实测 |
|---|---|---|---|---|
| dsh-trusted-0 | 微信读屏 | 636×1400 @280 | **10fps** | 1.3fps（静止不出帧） |
| dsh-trusted-1 | 逆水寒日课 | 1400×636 @280 | **20fps** | 0fps（无应用） |

屏1 平时没有应用 = 0 帧产出，压它只省"日课那十几分钟"；屏0 才是常驻开销。

## 怎么改
`src/dsh/VsDaemon.java` 的 `SPEC`，**第 4 列 = 请求刷新率**，0 = 不指定（系统默认 60）：

    static final int[][] SPEC = {{636,1400,280,10},{1400,636,280,20}};

改完必须：`sh build.sh && sh start.sh 18250`

## ★ 两个必须记住的坑

1. **编译用的 android.jar 是 API 23，没有 `VirtualDisplayConfig` 类** —— 所以创建
   虚拟屏那段**必须走反射**（`createVd()`），直写编译不过。
2. **Android 16 上 `DisplayManager.createVirtualDisplay` 的参数顺序是
   `(VirtualDisplayConfig, Handler, VirtualDisplay.Callback)`** —— **Handler 在前**！
   而 API 31~35 的老文档是 `(Config, Callback, Handler)`。代码里两个顺序都试，
   实测签名由 Probe 打出：
   `createVirtualDisplay(VirtualDisplayConfig,android.os.Handler,VirtualDisplay$Callback)`
   （`VirtualDisplayConfig.Builder.setRequestedRefreshRate(float)` 确实存在）

## 怎么验证
    dumpsys display | grep 'DisplayDeviceInfo{"dsh-trusted' | sed 's/^\(.\{0,150\}\).*/\1/'
应看到 `renderFrameRate 10.0` / `renderFrameRate 20.0`。
再看实际产出速率：`curl -s 127.0.0.1:18250/list` 隔 12 秒两次，比 frames 增量。

## 重建的副作用（每次重建都会发生）
- **displayId 会变**（本次 3/4 → 31/32）。桥接/机器人是动态查的（`wxbridge.display_id()`），
  不会出错；但**别在任何地方写死屏号**。
- **虚拟屏上的应用会被踢到物理屏**（微信会出现在用户手机上）。搬回来：
      adb shell "am start --display <新屏号> --windowingMode 5 -n com.tencent.mm/.ui.LauncherUI"
  （`wxbridge` 的 `/takeover` 就是这条命令）
- 所以改完要顺手确认：微信在虚拟屏、物理屏干净、桥接 `/health` 的 displayId 对上。

## 回退
- 想恢复默认帧率：SPEC 第 4 列都改 0 → 重编译 + 重建；
- 完整回退到锁帧率之前：`cp -p src/dsh/VsDaemon.java.bak-1009-锁帧率前 src/dsh/VsDaemon.java`
  → `sh build.sh && sh start.sh 18250`（注意仍会换 displayId、要把微信搬回来）。

---

# ★ 2026-10-11 更新：建屏 flags 与虚拟屏保活

> 上面那份写于 2026-10-09，其中**分辨率已过时** —— 屏0 在 2026-10-10 改成了
> **1264×2780@560**（精确对齐物理屏，减少跨屏移动 task 时的配置差异），见 SPEC 处注释。
> 本节记录之后新增的三件事。

## 一、建屏 flags（`createVd()` 里那一行）
```java
int flags = DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC | TOUCH | OWNFOCUS | TRUSTED | OWN_CONTENT
    | ROTATES_WITH_CONTENT        // 1<<7
    | OWN_DISPLAY_GROUP           // 1<<11
    | ALWAYS_UNLOCKED;            // 1<<12
```

| flag | 值 | 作用 | 本机实测 |
|---|---|---|---|
| `ROTATES_WITH_CONTENT` | 1<<7 | 内容随方向旋转填满，而不是被 letterbox | 两屏尺寸本就与内容方向一致（rotation=0）→ **无可见变化**，作标准配置保留 |
| `OWN_DISPLAY_GROUP` | 1<<11 | VD 拿独立 power group，不随主屏休眠 | ✅ `Display Groups: size=2`，锁屏时 VD 不再跟着睡 |
| `ALWAYS_UNLOCKED` | 1<<12 | 锁屏时仍派送触控 | ⚠️ **必须与 `OWN_DISPLAY_GROUP` 配套**，否则被 `VirtualDisplayAdapter` 静默丢弃（AOSP 原码有明确 `Slog.w`） |

⚠️ Android 17 上 `OWN_DISPLAY_GROUP` 可能被 `DisplayGroupAllocator` 覆盖（MoonClicker 实测），
本机 Android 16 没这个问题 —— 升级系统后要重新验证 `Display Groups` 的数量。

## 二、看门狗保活（`startWatchdog()`）
独立 group 有**自己的闲置超时**：VD 空闲时约 26 秒变 OFF，而且**唤醒主屏不会让它恢复**
（独立 group 不再跟主屏联动）。所以每 5 秒查一次 `DisplayInfo.state`，发现 `STATE_OFF`
且该 VD 落在**非 0 group** 时唤醒它：

```java
// Android 16 的 IPowerManager（注意：无 displayId 的版本叫 wakeUp，带 displayId 的是这个）
void wakeUpWithDisplayId(long time, int reason, String details, String opPackageName, int displayId);
// reason = PowerManager.WAKE_REASON_APPLICATION = 2
```

- AOSP 原码确认它**只唤醒 displayId 所属的 group**（内部走 `wakePowerGroupLocked`），不碰主屏；
- 权限要求 `DEVICE_POWER`，shell 已持有（`granted=true`）；
- 读 `DisplayInfo` 走 hidden 的 `DisplayManagerGlobal.getDisplayInfo(int)`
  —— Android 16 的 `Display` 上已经没有 `getDisplayInfo()` 了。

**实测**：锁屏期间 VD 保持 ON **52 秒**；熄屏时 `ocr.sh --pipe 0` 读屏成功；
VD 超时后看门狗 **3 秒内**把它叫回 ON（daemon 日志 `WAKEUP dsh-vd-watchdog displayId=59`）。

## 三、⛔ 已否决的方案（别再试）
**绑 displayId 的 `SCREEN_BRIGHT_WAKE_LOCK` 保活 —— 不可用。**
`IPowerManager.acquireWakeLock(..., displayId, ...)` 能调用成功，但 **displayId 参数不生效**：
锁在 dumpsys 里没有 displayId 标注（对比：系统自己的锁会标 `WindowManager/displayId:0`），
于是退化成**全局屏幕锁**，把**主屏的熄屏也挡住**（`mHoldingDisplaySuspendBlocker=true`，
当时按电源键都关不掉屏幕）。

## 四、⚠️ 重建守护后必须检查机器人
`wxbridge` 缓存的是**启动那一刻**的 displayId。重建守护后屏号会变（实测 57 → 59），
机器人会一直报「读不到画面：微信不在虚拟屏前台，跳过本轮」。

```sh
cd /sdcard/dsh/wechat-auto && bash bot.sh start --restart
```

## 五、验证命令
```sh
# flags 是否都生效
shd "dumpsys display | grep -E 'DisplayDeviceInfo\{\"dsh-trusted-[01]\"' | grep -oE 'FLAG_[A-Z_]+' | sort -u"
# 是否独立成组（size 应为 2）
shd "dumpsys display | grep -A4 'Display Groups:'"
# 看门狗是否在工作
shd "grep -E 'WATCHDOG|WAKEUP' /data/local/tmp/vs-daemon.log | tail -5"
```
