package com.dsh.hud;

import android.app.KeyguardManager;
import android.app.Notification;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.ColorFilter;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.Vibrator;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.Calendar;
import java.util.Locale;
import java.util.TimeZone;

/**
 * DSH 浮窗：agent 正在操作这台手机时出现在屏幕上，告诉他"我在干什么"；一停就自己消失。
 *
 * 为什么不常驻（2026-10-05 用户明确要求）：常驻挡视线。用户真正需要它的时间窗只有
 * 一段 —— **agent 占着他屏幕、他看不见发生了什么的那段时间**。所以：
 *
 *   · 任何一次 phone 操作          → 显示 / 续期（见 phone-wrapper.sh）
 *   · 交还界面（open DSH 自己）    → 2.5 秒后淡出（wrapper 会调 /hidein）
 *   · 兜底：最后一次操作后 25 秒没动静 → 自动淡出（防 agent 忘了交还）
 *
 * 编译用的是 API 23 stub，所以 TYPE_APPLICATION_OVERLAY / NotificationChannel
 * 这些高版本符号用数字常量与反射绕开，运行时仍是新系统行为。
 */
public class HudService extends Service {

    public static final int PORT = 18787;

    private static final String CH_ID = "dsh_hud";
    private static final int NOTI_ID = 44531;
    // ★ 2026-10-10：抢屏提醒专用的**高优先级**渠道（见 alert()）。
    //   跟 CH_ID 分开是故意的 —— 那条常驻通知按用户要求删了，别共用它的设置。
    // ★ 2026-10-13（用户要求「取消抢屏震动」）：**通道 id 必须换掉** ——
    //   Android 的通知通道一经创建就不再接受修改，旧 id（dsh_hud_alarm）当年建的是
    //   enableVibration(true)，改不动；换一个新 id 才能把通道自带的震动真正关掉。
    private static final String CH_ALARM = "dsh_hud_alarm_quiet";
    private static final String CH_ALARM_OLD = "dsh_hud_alarm";
    private static final int ALARM_ID = 44532;
    private static final int TYPE_OVERLAY = 2038;    // TYPE_APPLICATION_OVERLAY（API 26+）
    private static final int TYPE_PHONE = 2002;      // TYPE_PHONE（老系统）
    private static final long IDLE_HIDE_MS = 25000L; // 最后动作之后多久自动消失
    private static final long FADE_STEP_MS = 40L;    // 淡入淡出的一帧

    /* ---------------- 波峰 / 波谷（DeepSeek 错峰计费） ----------------
     * ★ 2026-10-09（用户要求）：浮窗上直接告诉他"现在是贵还是便宜"，
     *   好决定现在就让 agent 干活、还是等便宜时段再跑。
     *
     * 规则（2026-10 从官方定价页核实，中英文两版一致）：
     *   北京时间 周一至周五（**不含**中国法定节假日）9:00-12:00、14:00-18:00 = 高峰（全价）；
     *   其余时段 —— 含**周末**与**法定节假日全天** —— 均为空闲（半价）。
     *   deepseek-flash 的高峰价正好是空闲价的 2 倍（输出 8 元 vs 4 元/百万 token），
     *   这也正是用户说的"波峰双倍消耗"。
     *
     * 注意：调休**要上班的周六周日**也按空闲算 —— 官方口径是按"周末"两个字判定的。
     *
     * 节假日表来自《国务院办公厅关于2026年部分节假日安排的通知》
     * （2025-11-04 发布）。**跨年要换表** —— 只影响显示，不影响主流程。
     */
    private static final TimeZone BJ = TimeZone.getTimeZone("Asia/Shanghai");
    private static final String[][] HOLIDAYS = {
            {"2026-01-01", "2026-01-03"},   // 元旦
            {"2026-02-15", "2026-02-23"},   // 春节
            {"2026-04-04", "2026-04-06"},   // 清明节
            {"2026-05-01", "2026-05-05"},   // 劳动节
            {"2026-06-19", "2026-06-21"},   // 端午节
            {"2026-09-25", "2026-09-27"},   // 中秋节
            {"2026-10-01", "2026-10-07"},   // 国庆节
    };
    /** 高峰时段（北京时间，单位：当天第几分钟） */
    private static final int[][] PEAK_WINDOWS = {{9 * 60, 12 * 60}, {14 * 60, 18 * 60}};
    /** 一天里可能发生峰谷翻转的时刻（其余时刻状态不会变，用来快速找切换点） */
    private static final int[][] SWITCH_POINTS = {{0, 0}, {9, 0}, {12, 0}, {14, 0}, {18, 0}};
    private static final int COL_OFFPEAK = 0xCC4ADE80;   // 细条·绿 = 波谷（半价）
    private static final int COL_PEAK = 0xCCFF7A45;      // 细条·橙 = 波峰（全价）
    private static final int TXT_OFFPEAK = 0xFF7EE2A8;
    private static final int TXT_PEAK = 0xFFFFB37A;
    /**
     * 每行文字的最大宽度（dp）。
     * ★ 2026-10-09：用户报「浮窗右半部分都是空白，太占地方」——
     *   这块屏幕逻辑宽度只有 361dp（1264px / 560dpi），原来 250dp 的上限等于屏宽 70%。
     *   收到 200dp：面板整体约 224dp（含左右各 12dp 内边距），不挡事，
     *   真正的宽度仍由**当前内容**决定 —— 内容短就窄。
     */
    private static final int MAX_TEXT_DP = 200;

    private android.content.SharedPreferences prefs;
    private String attachErr = "";
    private WindowManager wm;
    private View root;
    private WindowManager.LayoutParams lp;
    private TextView tvTitle, tvAction, tvDetail, tvDot;
    private TextView tvCost;          // 「现在波峰还是波谷」那一行
    private GradientDrawable barBg;   // 细条背景（颜色随峰谷变）
    private int costState = -1;       // -1 未算过 / 0 波峰 / 1 波谷
    private long lastCostAt = 0L;     // 上次计算峰谷的时刻（节流用）
    private View panel;               // 展开态：完整面板
    private View bar;                 // 常驻态：贴在右边缘的窄把手
    private boolean barOnly = false;  // 现在是不是"只剩细条"

    private final Handler h = new Handler(Looper.getMainLooper());

    private boolean visible = false;
    private boolean collapsed = false;
    private long shownAt = 0L;
    private long lastMsgAt = 0L;
    private long hideAt = 0L;      // 定时隐藏（交还界面后）

    private String title = "DSH 浮窗";
    private String action = "已就绪";
    private String detail = "";

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onCreate() {
        super.onCreate();
        lastMsgAt = System.currentTimeMillis();
        prefs = getSharedPreferences("hud", MODE_PRIVATE);
        wm = (WindowManager) getSystemService(Context.WINDOW_SERVICE);
        goForeground();
        buildOverlay();
        // ★ 2026-10-08 用户要求「浮窗常驻、吸附侧边」：开机就把细条挂上，
        //   不再等第一次推送才出现（attach 失败会记进 attachErr，不影响别的）
        collapseToBar();
        attach();
        h.post(tick);
        HudHttp.start(this);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // ★ 2026-10-09 修（面板上的「拉起浮窗」按钮暴露出来的）：
        //   被重新 startService（BootActivity / 面板按钮）时，**确保细条挂出来**。
        //   原来只有 onCreate 里 attach() 一次 —— 而 `stopSelf()` 之后服务往往
        //   并没有真的被销毁（同一进程 + START_STICKY），再 `am start` 只走
        //   onStartCommand：于是**服务活着、HTTP 通了、浮窗却不见了**
        //   （实测 visible=false，用户点"拉起"看不到任何东西 = 按钮等于没用）。
        if (root != null && !visible) {
            collapseToBar();
            attach();
        }
        if (intent != null) {
            String t = intent.getStringExtra("title");
            String a = intent.getStringExtra("action");
            String d = intent.getStringExtra("detail");
            if (t != null || a != null || d != null) set(t, a, d);
            if (intent.getBooleanExtra("hide", false)) hide();
        }
        return START_STICKY;
    }

    /* ---------------- 前台服务（防被系统杀） ---------------- */

    /**
     * ★ 2026-10-09 用户要求：「Dsh 有一个通知栏通知，现在没有用了，现在换成浮窗了吗？
     *   把那个通知删掉吧」—— 删的就是这条 id=44531「DSH 浮窗待命」。
     *
     * ⚠️ 先说清楚它是干什么的：**它从来不是用来显示状态的**。状态早在 2026-10-08
     *   就归浮窗了；这条通知只是 Android 对**前台服务**的硬性要求
     *   （`startForeground()` 必须挂一条通知）。所以删掉它 = 这个服务从"前台服务"
     *   降级成**普通后台服务**：
     *     · 好处：通知栏干净了，那条划不掉的常驻项没了；
     *     · 代价：被系统（尤其 ColorOS 的后台清理）杀掉的概率会上升一点。
     *
     * 为什么敢降级：
     *   ① 本 App 的 targetSdk 是 22（为了 SYSTEM_ALERT_WINDOW 安装即授予）——
     *      Android 8+ 的「后台服务限制」对 targetSdk < 26 的应用不适用，
     *      普通 Service 照样能被 `BootActivity` → `startService` 拉起来；
     *   ② 浮窗本身是一个**可见窗口**（TYPE_APPLICATION_OVERLAY），
     *      有可见窗口的进程在 LMK 眼里优先级本来就比纯后台进程高；
     *   ③ 容器侧补了自愈：`/sdcard/dsh/tools/start-keepalive.sh` 每 30 秒 ping
     *      一次 `127.0.0.1:18787/ping`，不通就 `hud/start.sh` 重新拉起
     *      —— 最多 30 秒回来（比原来"等下一次 hud 调用"可靠）。
     *
     * 想恢复前台服务：把这里换回 `startForeground(NOTI_ID, b.getNotification())`
     * （含上面的 NotificationChannel 那段），备份见
     * `src/com/dsh/hud/HudService.java.bak-1009-去通知前`。
     */
    private void goForeground() {
        // 方法名保留（onCreate 里的调用点不动）：现在只负责"把旧版本留下的那条
        // 常驻通知取消掉"，不再 startForeground。
        try {
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            nm.cancel(NOTI_ID);
        } catch (Throwable ignored) {
            // 通知相关的事一律不许影响浮窗本身
        }
    }

    /* ---------------- 浮窗本体 ---------------- */

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }

    private void buildOverlay() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(0xE6101418);
        bg.setCornerRadius(dp(14));
        bg.setStroke(dp(1), 0x59FFFFFF);
        box.setBackground(bg);
        box.setPadding(dp(12), dp(8), dp(12), dp(10));

        LinearLayout head = new LinearLayout(this);
        head.setOrientation(LinearLayout.HORIZONTAL);
        head.setGravity(Gravity.CENTER_VERTICAL);

        tvDot = new TextView(this);
        tvDot.setText("●");
        tvDot.setTextColor(0xFF4ADE80);
        tvDot.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10);

        tvTitle = new TextView(this);
        tvTitle.setTextColor(0xFF9AA4B2);
        tvTitle.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10);
        tvTitle.setPadding(dp(5), 0, 0, 0);
        tvTitle.setMaxWidth(dp(MAX_TEXT_DP));      // 标题太长也会把面板撑宽

        head.addView(tvDot);
        head.addView(tvTitle);

        tvAction = new TextView(this);
        tvAction.setTextColor(Color.WHITE);
        tvAction.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        tvAction.setPadding(0, dp(3), 0, 0);
        tvAction.setMaxWidth(dp(MAX_TEXT_DP));

        tvDetail = new TextView(this);
        tvDetail.setTextColor(0xFFB9C2CF);
        tvDetail.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10);
        tvDetail.setPadding(0, dp(3), 0, 0);
        tvDetail.setMaxWidth(dp(MAX_TEXT_DP));

        // ★ 2026-10-09：峰谷提示行。上面那条线用**背景 Drawable** 画，不用 View ——
        //   用 `View` + MATCH_PARENT 会把整个面板撑到屏幕宽（原因见 TopLineDrawable）。
        tvCost = new TextView(this);
        tvCost.setTextColor(TXT_OFFPEAK);
        tvCost.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10);
        tvCost.setPadding(0, dp(6), 0, 0);
        tvCost.setMaxWidth(dp(MAX_TEXT_DP));
        tvCost.setBackground(new TopLineDrawable(0x33FFFFFF, Math.max(1, dp(1))));

        box.addView(head);
        box.addView(tvAction);
        box.addView(tvDetail);
        box.addView(tvCost);
        panel = box;

        // ★ 2026-10-08 用户要求「浮窗搞常驻，但是可以吸附在侧边，有消息了就自动打开」：
        //   平时显示这条**贴在右边缘的窄把手**（不挡屏），有动作时展开成上面的完整
        //   面板，闲置/交还后再缩回来 —— **不再整个消失**。原来空闲和交还都走
        //   `fadeTo(0f, true)` = 淡出 + removeView，用户看不到"它还在"。
        bar = new View(this);
        barBg = new GradientDrawable();
        barBg.setColor(COL_OFFPEAK);   // 绿 = 现在便宜；波峰时改成橙色（见 updateCost）
        barBg.setCornerRadius(dp(3));
        bar.setBackground(barBg);
        bar.setVisibility(View.GONE);

        FrameLayout wrap = new FrameLayout(this);
        wrap.addView(box);
        FrameLayout.LayoutParams blp = new FrameLayout.LayoutParams(dp(5), dp(56));
        blp.gravity = Gravity.CENTER;
        wrap.addView(bar, blp);
        root = wrap;

        int type = Build.VERSION.SDK_INT >= 26 ? TYPE_OVERLAY : TYPE_PHONE;
        lp = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.WRAP_CONTENT,
                type,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                PixelFormat.TRANSLUCENT);
        // 默认贴右上角：对话内容多在左边，这样挡得最少；用户拖过之后位置被记住
        // ⚠️ 2026-10-07：key 从 x/y 换成 x3/y3 —— 旧值被"方向反了"的拖动逻辑写坏过
        //    （用户报「浮窗歪左边里面去了」），换 key 等于把历史坏位置一次清掉，
        //    直接回到默认的右上角。
        // ⚠️ 2026-10-09：key 再换成 **x4/y4** —— 这次是因为面板曾经被撑到 296dp，
        //    用户在屏幕上把它往右推，存下了 **-346**（负的"距右边距离" = 窗口跑到
        //    屏幕右边外面 346px），文字右半截被切掉（dumpsys 实测
        //    frame=[Rect(1100,392-1610,701)]，屏幕只有 1264）。同时给拖动加了
        //    范围钳位（clampX/clampY），以后再也拖不出屏幕。
        lp.gravity = Gravity.TOP | Gravity.END;
        lp.x = clampX(prefs.getInt("x4", dp(10)));
        lp.y = clampY(prefs.getInt("y4", dp(84)));

        // 能拖走（免得挡住用户要看的东西），轻点一下收起/展开
        wrap.setOnTouchListener(new View.OnTouchListener() {
            private float downX, downY;
            private int startX, startY;
            private boolean moved;

            public boolean onTouch(View v, MotionEvent e) {
                switch (e.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        downX = e.getRawX();
                        downY = e.getRawY();
                        startX = lp.x;
                        startY = lp.y;
                        moved = false;
                        return true;
                    case MotionEvent.ACTION_MOVE: {
                        int dx = (int) (e.getRawX() - downX);
                        int dy = (int) (e.getRawY() - downY);
                        if (Math.abs(dx) > dp(6) || Math.abs(dy) > dp(6)) moved = true;
                        if (moved) {
                            // ⚠️ 2026-10-07 修：gravity 是 TOP|END，lp.x 表示**距右边的距离**，
                            //    所以往右拖（dx>0）要**减** x 才对。原来写的是 +dx，
                            //    方向反了 —— 往右拖浮窗往左跑，一路能拖出屏幕左边。
                            // ⚠️ 2026-10-09：钳位后再落位，拖不出屏幕（见 clampX 注释）。
                            lp.x = clampX(startX - dx);
                            lp.y = clampY(startY + dy);
                            try { wm.updateViewLayout(root, lp); } catch (Throwable ignored) { }
                        }
                        return true;
                    }
                    case MotionEvent.ACTION_UP:
                    case MotionEvent.ACTION_CANCEL:
                        if (moved) prefs.edit().putInt("x4", lp.x).putInt("y4", lp.y).apply();
                        else toggleCollapse();
                        return true;
                    default:
                        return false;
                }
            }
        });

        render();
    }

    /**
     * 把「距右边距离」钳进屏幕内。
     *
     * 为什么必须有：这是 **2026-10-09 用户报「右半部分都是空白 / 文字被切」的真正原因**
     * —— 存下来的位置是 **-346**，而 lp.x 为负表示窗口右边界**跑到屏幕右边外面**
     * （dumpsys：frame 右边界 1610 > 屏幕 1264），于是文字右半截看不见。
     * 规则：x≥0（右边绝不越界）；x≤屏宽-窗口宽（不许整个推到屏幕左边外面）。
     */
    private int clampX(int x) {
        int screen = getResources().getDisplayMetrics().widthPixels;
        int w = (root != null && root.getWidth() > 0) ? root.getWidth() : dp(MAX_TEXT_DP + 24);
        int maxX = Math.max(0, screen - Math.min(w, dp(MAX_TEXT_DP + 24)));
        if (x < 0) return 0;
        return Math.min(x, maxX);
    }

    /** 纵向同理：不许拖出屏幕上边/下边（上边留 30dp，别顶进状态栏）。 */
    private int clampY(int y) {
        int screen = getResources().getDisplayMetrics().heightPixels;
        int h = (root != null && root.getHeight() > 0) ? root.getHeight() : dp(140);
        int maxY = Math.max(dp(30), screen - Math.min(h, dp(400)));
        if (y < dp(30)) return dp(30);
        return Math.min(y, maxY);
    }

    /* ---------------- 显示 / 隐藏 ---------------- */

    private void attach() {
        if (root == null) return;
        if (!visible) {
            try {
                root.setAlpha(0f);
                wm.addView(root, lp);
                visible = true;
                attachErr = "";
                shownAt = System.currentTimeMillis();
            } catch (Throwable t) {
                visible = false;
                attachErr = String.valueOf(t);   // 多半是没给悬浮窗权限
                return;
            }
        }
        fadeTo(1f, false);
    }

    private void detachNow() {
        if (root != null && visible) {
            try { wm.removeView(root); } catch (Throwable ignored) { }
            visible = false;
        }
    }

    /** 淡出然后移除；用户主动收起和空闲超时都走这里。 */
    public void hide() {
        h.post(new Runnable() {
            public void run() {
                hideAt = 0L;
                fadeTo(0f, true);
            }
        });
    }

    /** 延迟 ms 毫秒后自动隐藏（交还界面时用）。 */
    /**
     * 让手机震一下。★ 2026-10-08（用户要求）：干完活要能"叫醒"锁屏的用户。
     *
     * 为什么必须 App 自己干：`cmd notification post` 发的 shell 通知走**静默渠道**
     * （不弹横幅、不响、不震），而 `cmd vibrator` 在这台机器上**根本不存在** ——
     * 2026-10-07 实测：桥接日志写着「已弹让位提醒」，用户全屏玩游戏时毫无察觉。
     * 通知 + 亮屏 + 震动三样齐了，锁屏的用户才真的能感知到。
     *
     * API 26 起得用 VibrationEffect，但本工程编译用的是 **API 23 stub**（见 AGENTS），
     * 拿不到那个类 —— 所以走反射，和上面 NotificationChannel 同一个套路。
     */
    public String vibrate(long ms, int times) {
        // ★ 2026-10-08 晚：**返回具体结果**，不再默默吞异常。
        //   起因：用户实测「亮屏了，没震动」，而原实现把所有 Throwable 都吃掉了，
        //   HTTP 只回 OK —— 到底是没有马达、拿不到系统服务、还是反射调用失败，
        //   完全无从查起。现在每一种失败都带原因回去。
        // ★ 2026-10-13（用户要求）：**0（或负数）= 明确"不要震"**，直接返回。
        //   原来这里把参数钳到最小值（ms<50→50、times<1→1），于是调用方传 0
        //   也照样震 —— 抢屏提醒想静音根本没有办法。
        if (ms <= 0 || times <= 0) return "off（参数为 0：不震动）";
        try {
            if (ms < 50) ms = 50;
            if (ms > 3000) ms = 3000;
            if (times < 1) times = 1;
            if (times > 10) times = 10;
            Vibrator v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
            if (v == null) return "err:no-vibrator-service";
            if (!v.hasVibrator()) return "err:device-says-no-vibrator";
            Object effect = null;
            Class<?> veCls = null;
            String why = "";
            try {
                veCls = Class.forName("android.os.VibrationEffect");
                // ★ 2026-10-08 晚：用**最大振幅 255**，不要 -1(DEFAULT_AMPLITUDE)。
                //   实测证据：系统 appops 记着「VIBRATE: allow; duration=+426ms」——
                //   震动**确实执行了**，但用户完全没感觉到：默认振幅在这台 ColorOS 上
                //   弱到无感。255 = 该马达能给的最高强度。
                effect = veCls.getMethod("createOneShot", long.class, int.class)
                             .invoke(null, ms, 255);
            } catch (Throwable t) {
                why = "effect:" + t;
            }
            for (int i = 0; i < times; i++) {
                try {
                    if (effect != null) {
                        Vibrator.class.getMethod("vibrate", veCls).invoke(v, effect);
                    } else {
                        Vibrator.class.getMethod("vibrate", long.class).invoke(v, ms);
                    }
                } catch (Throwable t) {
                    return "err:call:" + t + (why.length() == 0 ? "" : " / " + why);
                }
                if (i + 1 < times) Thread.sleep(Math.max(150L, ms / 2));
            }
            return "OK " + ms + "ms x" + times
                 + (why.length() == 0 ? "" : " [fallback: " + why + "]");
        } catch (Throwable t) {
            return "err:outer:" + t;
        }
    }

    /* ---------------- 抢屏提醒：高优先级通知 ----------------
     * ★ 2026-10-10（用户报障）：「他在那抢微信，他不告诉我，他没有通知，没有通知
     *   我就会乱点。」
     *
     *   病根：桥接原来的"让位提醒"发的是 `adb shell cmd notification post` ——
     *   那是 **shell_cmd 静默渠道**（dumpsys 里看得见，但**横幅不弹、不响、
     *   连屏都不亮**），他在打字或全屏时等于什么都没收到。
     *
     *   所以这里由 App 自己发一条 **IMPORTANCE_HIGH** 的通知：会弹横幅、会响、
     *   按下面的节奏震，而且**一直挂着**到 `alertOff()` 才撤（抢屏结束）。
     *   ⚠️ 这是**按需**的一条，不是常驻通知 —— 用户 2026-10-09 删掉的是那条
     *      "DSH 浮窗待命"的常驻通知，别把那个加回来。
     */
    public String alert(String title, String text, long ms, int times) {
        // ★ 2026-10-11：**熄屏/锁屏时绝不出声、不震动**（最后一道保险）。
        //   用户当天报「锁屏时一直在震动」—— 抢屏预告走的就是这条通知
        //   （渠道 IMPORTANCE_HIGH + setDefaults(DEFAULT_ALL) + 连震三下），
        //   锁屏时它会亮屏、震动、响铃。而锁屏下机器人本来就发不出消息
        //   （系统不给非锁屏窗口输入连接，实测 mServedView=null），
        //   所以即便调用方没拦住，这里也绝不在他睡觉时震他。
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null && !pm.isInteractive()) {
                return "locked-skip（熄屏：不提醒、不震动）";
            }
            KeyguardManager km = (KeyguardManager) getSystemService(Context.KEYGUARD_SERVICE);
            if (km != null && km.isKeyguardLocked()) {
                return "locked-skip（锁屏：不提醒、不震动）";
            }
        } catch (Throwable ignored) {
            // 读不到锁屏状态就照发（别因为一个探测失败把提醒整个吞掉）
        }
        // ★ 2026-10-13（用户要求「取消抢屏震动」）：ms/times 传 0 时，整条提醒
        //   **只弹横幅、不震**（提示本身保留 —— 他要的是别震，不是别提醒）。
        boolean doVib = (ms > 0 && times > 0);
        String vib = doVib ? vibrate(ms, times) : "off（抢屏提醒不震动）";
        try {
            NotificationManager nm = (NotificationManager)
                    getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) return "err:no-notification-service vib=" + vib;
            if (Build.VERSION.SDK_INT >= 26) {
                try {
                    Class<?> nc = Class.forName("android.app.NotificationChannel");
                    Object ch = nc.getConstructor(String.class, CharSequence.class, int.class)
                            .newInstance(CH_ALARM, "DSH 抢屏提醒", 4);   // 4 = IMPORTANCE_HIGH
                    try {
                        // ★ 2026-10-13：通道层面也关震动（这是 API 26+ 真正的开关）。
                        nc.getMethod("enableVibration", boolean.class).invoke(ch, false);
                    } catch (Throwable ignored) { }
                    try {
                        nc.getMethod("setDescription", String.class)
                                .invoke(ch, "机器人要借用一下你的屏幕时会提醒你");
                    } catch (Throwable ignored) { }
                    NotificationManager.class.getMethod("createNotificationChannel", nc)
                            .invoke(nm, ch);
                    // ★ 2026-10-13：顺手删掉旧通道（它自带震动，留着只是噪音源）。
                    try {
                        NotificationManager.class
                                .getMethod("deleteNotificationChannel", String.class)
                                .invoke(nm, CH_ALARM_OLD);
                    } catch (Throwable ignored) { }
                } catch (Throwable ignored) { }
            }
            String ttl = (title == null || title.length() == 0) ? "DSH 微信机器人" : title;
            String body = (text == null) ? "" : text;
            long gap = Math.max(80L, ms);
            Notification.Builder b = new Notification.Builder(this)
                    .setSmallIcon(android.R.drawable.stat_notify_sync)
                    .setContentTitle(ttl)
                    .setContentText(body)
                    .setTicker(body)
                    .setAutoCancel(true)
                    // ★ 2026-10-11：**同一条提醒更新时不再重复震+响**。
                    //   原来没这句，而机器人每次发送前都发一遍这条通知
                    //   （同一个 ALARM_ID）→ Android 每次都当成新提醒重新
                    //   震动+响铃，用户听到的就是"一直在震动"。
                    //   第一次照旧弹横幅 + 震动（"有头有尾"没变），
                    //   只是后续更新不再骚扰；抢完 alertOff() 撤销后，
                    //   下一次新的抢屏事件仍是全新提醒，照样会震。
                    .setOnlyAlertOnce(true);
            if (doVib) {
                b.setVibrate(new long[]{0, gap, 150, gap, 150, gap});
                b.setDefaults(Notification.DEFAULT_ALL);
            } else {
                // ★ 2026-10-13：绝不能再用 DEFAULT_ALL —— 它含 DEFAULT_VIBRATE。
                //   只留提示音 + 呼吸灯：会轻轻响一声，但**不震**。
                b.setDefaults(Notification.DEFAULT_SOUND | Notification.DEFAULT_LIGHTS);
            }
            if (Build.VERSION.SDK_INT >= 26) {
                try {
                    Notification.Builder.class.getMethod("setChannelId", String.class)
                            .invoke(b, CH_ALARM);
                } catch (Throwable ignored) { }
            } else {
                b.setPriority(Notification.PRIORITY_HIGH);
            }
            nm.notify(ALARM_ID, b.getNotification());
            return "OK noti + vib=" + vib;
        } catch (Throwable ex) {
            return "err:" + ex + " vib=" + vib;
        }
    }

    /** 收回抢屏提醒那条通知（抢屏结束后调）。 */
    public String alertOff() {
        try {
            NotificationManager nm = (NotificationManager)
                    getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) nm.cancel(ALARM_ID);
            return "OK off";
        } catch (Throwable ex) {
            return "err:" + ex;
        }
    }

    /** 缩成贴边细条（常驻形态）：面板藏起来，只留把手。 */
    private void collapseToBar() {
        if (panel == null || bar == null) return;
        barOnly = true;
        panel.setVisibility(View.GONE);
        bar.setVisibility(View.VISIBLE);
        applyPos();
    }

    /** 展开成完整面板（有动作时自动调用）。 */
    private void expandPanel() {
        if (panel == null || bar == null) return;
        barOnly = false;
        bar.setVisibility(View.GONE);
        panel.setVisibility(View.VISIBLE);
        applyPos();
        render();
    }

    /** 细条态贴紧右边缘（x=0），展开态回到用户拖过的位置（一律钳进屏幕）。 */
    private void applyPos() {
        if (lp == null || root == null) return;
        lp.x = barOnly ? 0 : clampX(prefs.getInt("x4", dp(10)));
        lp.y = barOnly ? clampY(lp.y) : clampY(prefs.getInt("y4", dp(84)));
        try { wm.updateViewLayout(root, lp); } catch (Throwable ignored) { }
    }

    public void hideIn(final long ms) {
        h.post(new Runnable() {
            public void run() {
                hideAt = System.currentTimeMillis() + ms;
            }
        });
    }

    public void show() {
        h.post(new Runnable() {
            public void run() { attach(); }
        });
    }

    private void fadeTo(final float target, final boolean detachAtEnd) {
        if (root == null || !visible) return;
        h.post(new Runnable() {
            public void run() {
                if (root == null || !visible) return;
                float a = root.getAlpha();
                if (Math.abs(a - target) < 0.01f) {
                    root.setAlpha(target);
                    if (detachAtEnd) detachNow();
                    return;
                }
                float step = (target > a) ? 0.18f : -0.18f;
                float next = a + step;
                // 别越过目标值，否则永远收敛不到 target、浮窗会卡在"全透明但还占着触摸"
                if (step > 0 && next > target) next = target;
                if (step < 0 && next < target) next = target;
                root.setAlpha(next);
                if (next == target) {
                    if (detachAtEnd) detachNow();
                } else {
                    h.postDelayed(this, FADE_STEP_MS);
                }
            }
        });
    }

    /* ---------------- 内容更新 ---------------- */

    /** 更新浮窗文字（并让它出现）；传 null 表示该项不变。 */
    public void set(String t, String a, String d) {
        if (t != null) title = t;
        if (a != null) action = a;
        if (d != null) detail = d;
        lastMsgAt = System.currentTimeMillis();
        hideAt = 0L;                 // 有新动作就取消"准备隐藏"
        h.post(new Runnable() {
            public void run() {
                expandPanel();               // 有动静就自动展开（用户要求）
                attach();
                render();
            }
        });
    }

    private void render() {
        if (root == null) return;
        tvTitle.setText(title);
        tvAction.setText(action);

        boolean showBody = !collapsed;
        tvAction.setVisibility(showBody ? View.VISIBLE : View.GONE);
        boolean hasDetail = showBody && detail != null && detail.length() > 0;
        if (hasDetail) tvDetail.setText(detail);
        tvDetail.setVisibility(hasDetail ? View.VISIBLE : View.GONE);
        if (tvCost != null) tvCost.setVisibility(showBody ? View.VISIBLE : View.GONE);
        updateCost(false);
    }

    /* ---------------- 波峰 / 波谷：判定 ---------------- */

    private static Calendar bj(long ms) {
        Calendar c = Calendar.getInstance(BJ, Locale.CHINA);
        c.setTimeInMillis(ms);
        return c;
    }

    private static String dayKey(Calendar c) {
        return String.format(Locale.US, "%04d-%02d-%02d",
                c.get(Calendar.YEAR), c.get(Calendar.MONTH) + 1, c.get(Calendar.DAY_OF_MONTH));
    }

    private static boolean isHoliday(Calendar c) {
        String d = dayKey(c);
        for (String[] r : HOLIDAYS) {
            if (d.compareTo(r[0]) >= 0 && d.compareTo(r[1]) <= 0) return true;
        }
        return false;
    }

    /** true = 波谷（空闲时段，半价）。 */
    static boolean isOffPeak(long ms) {
        Calendar c = bj(ms);
        int dow = c.get(Calendar.DAY_OF_WEEK);
        if (dow == Calendar.SATURDAY || dow == Calendar.SUNDAY) return true;  // 周末全天
        if (isHoliday(c)) return true;                                       // 法定节假日全天
        int m = c.get(Calendar.HOUR_OF_DAY) * 60 + c.get(Calendar.MINUTE);
        for (int[] w : PEAK_WINDOWS) {
            if (m >= w[0] && m < w[1]) return false;                         // 落在高峰窗口里
        }
        return true;
    }

    /** 下一次峰谷翻转的时刻；12 天内找不到就返回 0。 */
    static long nextSwitch(long now) {
        boolean cur = isOffPeak(now);
        Calendar day = bj(now);
        day.set(Calendar.HOUR_OF_DAY, 0);
        day.set(Calendar.MINUTE, 0);
        day.set(Calendar.SECOND, 0);
        day.set(Calendar.MILLISECOND, 0);
        for (int d = 0; d <= 12; d++) {
            for (int[] hm : SWITCH_POINTS) {
                Calendar p = (Calendar) day.clone();
                p.set(Calendar.HOUR_OF_DAY, hm[0]);
                p.set(Calendar.MINUTE, hm[1]);
                long t = p.getTimeInMillis();
                if (t > now && isOffPeak(t) != cur) return t;
            }
            day.add(Calendar.DAY_OF_MONTH, 1);
        }
        return 0;
    }

    /** 切换时刻的短描述：「12:00」「明天 9:00」「周一 9:00」「10月1日 9:00」 */
    private static String whenText(long now, long at) {
        Calendar a = bj(now), b = bj(at);
        String hm = b.get(Calendar.HOUR_OF_DAY) + ":"
                + String.format(Locale.US, "%02d", b.get(Calendar.MINUTE));
        if (dayKey(a).equals(dayKey(b))) return hm;
        Calendar t = (Calendar) a.clone();
        t.add(Calendar.DAY_OF_MONTH, 1);
        if (dayKey(t).equals(dayKey(b))) return "明天 " + hm;
        if (at - now < 7L * 86400000L) {
            return "周" + "日一二三四五六".charAt(b.get(Calendar.DAY_OF_WEEK) - 1) + " " + hm;
        }
        return (b.get(Calendar.MONTH) + 1) + "月" + b.get(Calendar.DAY_OF_MONTH) + "日 " + hm;
    }

    /** 一行文案：现在贵不贵 + 什么时候变。 */
    static String costLine(long now) {
        boolean off = isOffPeak(now);
        long nx = nextSwitch(now);
        String tail = nx > 0 ? " · " + whenText(now, nx) + "起" + (off ? "全价" : "半价") : "";
        return (off ? "🟢 波谷 半价" : "🔴 波峰 全价") + tail;
    }

    /**
     * 刷新峰谷提示：面板上那一行 + **贴边细条的颜色**。
     * 用户平时看到的是细条 —— 绿色=现在便宜、橙色=现在贵，不展开也一眼能看见。
     * 默认 20 秒最多算一次（tick 每 500ms 跑，没必要每次都算）。
     */
    private void updateCost(boolean force) {
        long now = System.currentTimeMillis();
        if (!force && now - lastCostAt < 20000L) return;
        lastCostAt = now;
        boolean off = isOffPeak(now);
        int st = off ? 1 : 0;
        if (st != costState) {
            costState = st;
            if (barBg != null) {
                barBg.setColor(off ? COL_OFFPEAK : COL_PEAK);
                if (bar != null) bar.invalidate();
            }
        }
        if (tvCost != null) {
            tvCost.setText(costLine(now));
            tvCost.setTextColor(off ? TXT_OFFPEAK : TXT_PEAK);
        }
    }

    private void toggleCollapse() {
        if (barOnly) expandPanel(); else collapseToBar();
    }

    /** 呼吸点 + 走动的计时 + 两个自动隐藏的判定。 */
    private final Runnable tick = new Runnable() {
        public void run() {
            long now = System.currentTimeMillis();
            if (visible) {
                if (tvDot != null) {
                    double phase = (now % 1400) / 1400.0;
                    float a = (float) (0.25 + 0.75 * (0.5 + 0.5 * Math.sin(phase * 2 * Math.PI)));
                    tvDot.setAlpha(a);
                }
                if (tvTitle != null) {
                    tvTitle.setText(title + " · 已运行 " + fmt((now - shownAt) / 1000));
                }
                updateCost(false);               // 峰谷提示（内部节流 20 秒）
                if (hideAt > 0 && now >= hideAt) {
                    hideAt = 0L;
                    collapseToBar();             // 交还界面 → 缩成贴边细条（常驻）
                } else if (now - lastMsgAt > IDLE_HIDE_MS) {
                    // 兜底：闲置太久缩回细条。★ 不再 removeView —— 用户要的是常驻
                    if (!barOnly) collapseToBar();
                }
                h.postDelayed(this, 500);
            } else {
                h.postDelayed(this, 1500);       // 没显示时降频，省点电
            }
        }
    };

    private String fmt(long sec) {
        if (sec < 60) return sec + "s";
        return (sec / 60) + "m" + (sec % 60) + "s";
    }

    public String status() {
        long now = System.currentTimeMillis();
        return "visible=" + visible
                + " collapsed=" + collapsed
                + " barOnly=" + barOnly
                + " shownFor=" + (visible ? ((now - shownAt) / 1000) + "s" : "-")
                + " idle=" + ((now - lastMsgAt) / 1000) + "s"
                + (hideAt > 0 ? " hideIn=" + ((hideAt - now) / 1000) + "s" : "")
                + (attachErr.length() > 0 ? " attachErr=" + attachErr : "")
                + " cost=" + (isOffPeak(now) ? "offpeak(半价)" : "peak(全价)")
                + " barColor=" + (costState == 1 ? "green" : costState == 0 ? "orange" : "?")
                + " panelW=" + panelWidth()
                + " pos=" + (lp != null ? lp.x + "," + lp.y : "-")
                + (screenEdgeWarn() ? " !出屏" : "")
                + " action=" + action;
    }

    /**
     * 窗口是不是跑到屏幕右边外面了（lp.x 为负 = 越界）。
     * 用户 2026-10-09 遇到过一次（x=-346，文字被切），钳位后不该再出现 —— 留着当哨兵。
     */
    private boolean screenEdgeWarn() {
        if (lp == null) return false;
        int screen = getResources().getDisplayMetrics().widthPixels;
        int w = root != null ? root.getWidth() : 0;
        return lp.x < 0 || (w > 0 && lp.x + w > screen);
    }

    /** 面板实际宽度（px/dp）—— 排查"浮窗太宽"时看这个。 */
    private String panelWidth() {
        if (panel == null || panel.getWidth() <= 0) return "-";
        int px = panel.getWidth();
        return px + "px/" + Math.round(px / getResources().getDisplayMetrics().density) + "dp";
    }

    public void shutdown() {
        h.post(new Runnable() {
            public void run() {
                detachNow();
                stopSelf();
            }
        });
    }

    @Override
    public void onDestroy() {
        try {
            if (root != null && visible) wm.removeView(root);
        } catch (Throwable ignored) { }
        visible = false;
        super.onDestroy();
    }

    /**
     * 只在**顶部**画一条细线的背景（当分隔线用）。
     *
     * ★ 2026-10-09：为什么不用 `View` + `MATCH_PARENT` 画这条线 ——
     *   用户报「浮窗右半部分都是空白，太占地方」。查下来就是那条分隔线干的：
     *   外层 LinearLayout 是 `wrap_content`，父给它的测量规格是 **AT_MOST**，
     *   而 `View.getDefaultSize()` 对 AT_MOST 会直接返回 **specSize**
     *   （= 父给的可用宽度），于是这条 MATCH_PARENT 的线被测成"屏幕那么宽"，
     *   反过来把面板一起撑开 —— 实测面板 1036px（296dp，占屏 88%），
     *   右边界 1568 甚至超出了 1264 的屏幕。
     *   背景 Drawable **不参与测量**，宽度自动跟随宿主 TextView，是最省事的画法。
     */
    private static class TopLineDrawable extends Drawable {
        private final Paint paint = new Paint();
        private final int thick;

        TopLineDrawable(int color, int thick) {
            paint.setColor(color);
            this.thick = thick;
        }

        @Override
        public void draw(Canvas c) {
            Rect b = getBounds();
            c.drawRect(b.left, b.top, b.right, b.top + thick, paint);
        }

        @Override
        public void setAlpha(int a) { paint.setAlpha(a); }

        @Override
        public void setColorFilter(ColorFilter f) { paint.setColorFilter(f); }

        @Override
        public int getOpacity() { return PixelFormat.TRANSLUCENT; }
    }
}
