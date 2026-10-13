package dsh;
// DSH 可信虚拟屏守护（以 shell 身份运行 → 持有 ADD_TRUSTED_DISPLAY → 屏幕带 FLAG_TRUSTED → 支持窗口容器）
// 与 App 版 VsService 保持同一套 HTTP API：/list  /<i>/info  /<i>/frame[?w=]  POST /resize  POST /launch
import android.content.Context;
import android.content.Intent;
import android.content.ComponentName;
import android.app.ActivityOptions;
import android.app.PendingIntent;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.util.Log;
import java.io.*;
import java.net.*;
import java.nio.ByteBuffer;

public class VsDaemon {
  static final String TAG = "DSH-TRUSTED";
  static int PORT = 18241;   // 可由命令行参数覆盖（app_process 传参）
  static final int TRUSTED = 1 << 10, TOUCH = 1 << 6, OWNFOCUS = 1 << 14, OWN_CONTENT = 1 << 3;
  // ★ 2026-10-11 参考 MoonClicker（kuomartin/MoonClicker，平台级实测 + AOSP 原码）：
  //   ROTATES_WITH_CONTENT(1<<7)：物理屏天然带它（本机 dumpsys 实测），VD 默认不带。
  //     它决定内容随方向旋转填满、还是被 letterbox。本机两屏尺寸已与内容方向一致
  //     （rotation=0），实测加上后**无任何可见变化**（尺寸/rotation/displayGroup 全不变），
  //     保留作标准配置 —— 将来把竖屏 App 丢到横屏 VD 上时才真正用得到。
  //   ⛔ ALWAYS_UNLOCKED(1<<12) 已实测：**单独加不生效**。AOSP VirtualDisplayAdapter：
  //     "Ignoring VIRTUAL_DISPLAY_FLAG_ALWAYS_UNLOCKED as it requires
  //      VIRTUAL_DISPLAY_FLAG_DEVICE_DISPLAY_GROUP or OWN_DISPLAY_GROUP"
  //     —— 必须与 OWN_DISPLAY_GROUP 配套（scrcpy 正是两个一起带）。而 OWN_DISPLAY_GROUP
  //     会让 VD 拥有独立 power group 并**自己闲置超时**（API 33-35 叠黑 ColorFade，
  //     36+ 直接 state=OFF + activity pause），须配套"绑 displayId 的
  //     SCREEN_BRIGHT_WAKE_LOCK"保活 —— 属套餐改动，未做长稳验证前不启用。
  static final int ROTATES_WITH_CONTENT = 1 << 7;
  // ★ 2026-10-11（看门狗方案）：实测确认独立 group 后 VD 会**自己闲置超时**
  //   （主屏 Dozing 时前 ~26 秒保持 ON，随后 OFF 且唤醒主屏不恢复）。
  //   试过绑 displayId 的 SCREEN_BRIGHT_WAKE_LOCK 保活 —— acquire 成功但 displayId 参数
  //   不生效（退化成全局屏幕锁，把主屏熄屏也挡住），已回退，改用看门狗：
  //     每 5 秒查 VD 的 DisplayInfo.state，发现 OFF 就 wakeUpWithDisplayId 叫醒它。
  //   （MoonClicker：带 displayId 的 wakeUp 仅 API36/35QPR2+ 有；shell 持有 DEVICE_POWER。）
  //   ⚠️ 只在 VD 落在非 0 group 时才调用，避免误唤醒主屏。
  // ⚠️ 2026-10-12：下面两个旗标**已停用**（死锁根因，见 createVd 上方注释）。
  //   定义留着只为回退/对照，createVd 不再使用。
  static final int OWN_DISPLAY_GROUP = 1 << 11, ALWAYS_UNLOCKED = 1 << 12;
  // 屏0 = 微信读屏屏；屏1 = 逆水寒日课屏
  // ★ 2026-10-09：第 4 列 = 请求刷新率（fps）。0 = 不指定（回退系统默认 60）。
  //   屏0 锁 10（实测空闲就在 19.7fps 持续渲染，锁 10 直接砍半常驻功耗）；
  //   屏1 = 20（纯脚本操作，不需要流畅度）。
  //   改这个必须重编译 + 重建虚拟屏（重建 = displayId 变 = 微信被踢回物理屏）。
  // ★★ 2026-10-10（用户报「微信从物理屏切回虚拟屏会重建/回到会话列表」）：
  //   屏0 从 636x1400@280 改成 **1264x2780@560 —— 精确等于物理屏**。
  //   跨屏移动 task 时系统按目标屏重算 Configuration 再和当前的对；尺寸/density
  //   不一致就多一项差异。微信 LauncherUI 声明了 screenSize，尺寸本身不会触发
  //   relaunch，但对齐后布局完全一致、差异面最小。
  //   ⚠️ 这里治不了根：touchscreen（虚拟屏 -touch / 物理屏 finger）和 colorMode
  //      （虚拟屏无 HDR / 物理屏 hdr widecg）微信都没声明，必然 relaunch；
  //      真正治"回到会话列表"的是 wxbridge 改用 move-stack（见那里的注释）。
  // ★ 2026-10-12（今日死锁复盘后降载）：屏0 10→5fps、屏1 20→10fps。
  //   起因：08:42 手机被虚拟屏 surface 死锁冻住（system_server_watchdog，
  //   surfaceflinger 卡在 VirtualDisplaySurface::dequeueBuffer）→ 用户组合键强启。
  //   合成量 = 像素 x fps：屏0 352万x5 = 1760万/s（原 3520万），
  //   屏1 89万x10 = 890万/s（原 1780万），合计砍掉约一半。
  static final int[][] SPEC = {{1264,2780,560,5},{1400,636,280,10}};
  static final int[][] BASE  = {{1264,2780,560},{2800,1272,560}};   // 全分辨率基准，供 /res 计算

  // ═══ 2026-10-12：亮屏让路（虚拟屏死锁复盘后的缓解，与降帧率配套）═══
  //   死锁时间线：07:44 system_server_pre_watchdog -> 07:45 system_server_watchdog，
  //   卡点 surfaceflinger 的 VirtualDisplaySurface::dequeueBuffer；而 android.display
  //   线程当时正在 DisplayManagerService.requestDisplayState（用户插电/亮屏/解锁那一下）。
  //   所以「物理屏刚亮起」这几秒最危险 —— 这期间守护不做 JPEG/PNG 压缩这种重活。
  static volatile long screenAwakeAt = 0;
  static volatile boolean screenWasOn = true;
  static final long YIELD_MS = 3000;

  static void startScreenWatch() {
    Thread t = new Thread(new Runnable() { public void run() {
      log("SCREENWATCH 启动（每 1s 看主屏 state，亮起后让路 " + YIELD_MS + "ms）");
      while (true) {
        try {
          Thread.sleep(1000);
          int st = displayState(0);            // 0 = 物理主屏
          if (st < 0) continue;                // 读不到 -> 什么都不做（三态铁律）
          boolean on = (st == 2);              // Display.STATE_ON
          if (on && !screenWasOn) {
            screenAwakeAt = android.os.SystemClock.uptimeMillis();
            log("SCREEN 主屏亮起 -> 让路 " + YIELD_MS + "ms（暂停编码，避开 requestDisplayState）");
          }
          screenWasOn = on;
        } catch (Throwable t) { }
      }
    } });
    t.setDaemon(true);
    t.start();
  }

  static class Slot {
    VirtualDisplay vd; ImageReader reader; int id=-1, w, h, dpi, fps;
    volatile int frames = 0; byte[] jpeg; int jpegFrame = -1, jpegW = -1; boolean jpegPng = false;
    final Object lock = new Object(); Image held = null;
  }
  static Slot[] slots = new Slot[SPEC.length];
  static HandlerThread ht;

  static Context shellCtx() throws Exception {
    Class<?> at = Class.forName("android.app.ActivityThread");
    Object thr = at.getMethod("systemMain").invoke(null);
    Context sys = (Context) at.getMethod("getSystemContext").invoke(thr);
    return sys.createPackageContext("com.android.shell", 0);
  }

  static void attach(final Slot s, ImageReader r) {
    r.setOnImageAvailableListener(new ImageReader.OnImageAvailableListener() {
      public void onImageAvailable(ImageReader rr) {
        Image img = null;
        try {
          img = rr.acquireLatestImage();
          if (img == null) return;
          Image old;
          synchronized (s.lock) { old = s.held; s.held = img; s.frames++; s.jpeg = null; }
          if (old != null) { try { old.close(); } catch (Throwable t) {} }
        } catch (Throwable t) {
          Log.w(TAG, "frame", t);
          if (img != null) { try { img.close(); } catch (Throwable t2) {} }
        }
      }
    }, new Handler(ht.getLooper()));
  }

  // png=true 走**无损 PNG**（★2026-10-11 加，专供 A/B 与颜色/模板判据）：
  //   MaaFramework 官方控制方式文档把截图编码列为独立选项，明确警告
  //   「编码为 jpg 是有损的，会显著降低模板匹配的效果，不建议使用」——
  //   而 JPEG 的色度下采样(4:2:0) 正好打击本项目的「金色像素」判据与模板匹配
  //   （OCR 主要吃亮度，受影响最小）。默认仍走 JPEG 95，**不改现有行为**。
  static byte[] jpeg(Slot s, int maxW, boolean png) {
    // ★ 2026-10-12：亮屏/解锁瞬间「让路」—— 不做压缩这种重活。
    if (screenAwakeAt > 0 && android.os.SystemClock.uptimeMillis() - screenAwakeAt < YIELD_MS) {
      // 只回「当前帧已编码好」的新鲜缓存；不新鲜就直接回空（调用方当读不到画面跳过本轮）。
      // ⚠️ 绝不能回 s.jpeg 里可能过期很久的旧帧 —— 那会让机器人对着几小时前的画面回话。
      synchronized (s.lock) { if (s.jpeg != null && s.jpegFrame == s.frames) return s.jpeg; }
      return null;
    }
    Bitmap bmp = null; int frameNo; int w = 0, h = 0;
    synchronized (s.lock) {
      if (s.held == null) return null;
      if (s.jpeg != null && s.jpegFrame == s.frames && s.jpegW == maxW && s.jpegPng == png) return s.jpeg;
      Image img = s.held;
      frameNo = s.frames;
      w = img.getWidth(); h = img.getHeight();
      Image.Plane pl = img.getPlanes()[0];
      ByteBuffer buf = pl.getBuffer();
      int rs = pl.getRowStride(), ps = pl.getPixelStride();
      try {
        if (ps == 4 && rs == w * 4) {
          bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
          buf.rewind();
          bmp.copyPixelsFromBuffer(buf);
        } else {
          int[] px = new int[w * h];
          for (int y = 0; y < h; y++) { int b = y * rs;
            for (int x = 0; x < w; x++) { int o = b + x * ps;
              px[y * w + x] = (0xFF << 24) | ((buf.get(o) & 0xFF) << 16) | ((buf.get(o + 1) & 0xFF) << 8) | (buf.get(o + 2) & 0xFF); } }
          bmp = Bitmap.createBitmap(px, w, h, Bitmap.Config.ARGB_8888);
        }
      } catch (Throwable t) { Log.w(TAG, "jpeg", t); if (bmp != null) bmp.recycle(); return null; }
    }
    // ★ 2026-10-12：**出锁**再缩放/压缩。原实现整段压缩都攥着 s.lock，
    //   1264x2780 的 JPEG 要几百 ms；期间 onImageAvailable（抢同一把锁）换不了帧、
    //   旧 Image 还不到 ImageReader，buffer 一占满 SurfaceFlinger 就卡在
    //   VirtualDisplaySurface::dequeueBuffer —— 正是这次死锁的卡点。
    Bitmap out = bmp;
    try {
      if (maxW > 0 && w > maxW) { int nh = (int) ((long) h * maxW / w); out = Bitmap.createScaledBitmap(bmp, maxW, nh, true); }
      ByteArrayOutputStream bo = new ByteArrayOutputStream();
      // ★★ 2026-10-10：质量 70 -> 95。JPEG 质量是"OCR 读不出/模板匹配不准"的最底层原因之一：
      //   70 的压缩会把文字边缘糊掉、产生色块噪声，tesseract 和模板比对都吃亏。
      if (png) out.compress(Bitmap.CompressFormat.PNG, 100, bo);
      else     out.compress(Bitmap.CompressFormat.JPEG, 95, bo);
      byte[] bytes = bo.toByteArray();
      synchronized (s.lock) {
        if (s.frames == frameNo) { s.jpeg = bytes; s.jpegFrame = frameNo; s.jpegW = maxW; s.jpegPng = png; }
      }
      return bytes;
    } catch (Throwable t) { Log.w(TAG, "jpeg", t); return null; }
    finally { if (out != null && out != bmp) out.recycle(); if (bmp != null) bmp.recycle(); }
  }

  static String listJson() {
    StringBuilder b = new StringBuilder("[");
    for (int i = 0; i < slots.length; i++) { Slot s = slots[i]; if (s == null) continue;
      if (b.length() > 1) b.append(",");
      b.append("{\"i\":").append(i).append(",\"displayId\":").append(s.id)
       .append(",\"width\":").append(s.w).append(",\"height\":").append(s.h)
       .append(",\"frames\":").append(s.frames)
       .append(",\"yield\":").append(screenAwakeAt > 0 && android.os.SystemClock.uptimeMillis() - screenAwakeAt < YIELD_MS).append(",\"trusted\":true}"); }
    return b.append("]").toString();
  }
  static String js(String b, String k) {
    try { java.util.regex.Matcher m = java.util.regex.Pattern.compile("\"" + k + "\"\\s*:\\s*\"([^\"]*)\"").matcher(b);
      return m.find() ? m.group(1) : null; } catch (Throwable t) { return null; }
  }
  static int jn(String b, String k, int def) {
    try { java.util.regex.Matcher m = java.util.regex.Pattern.compile("\"" + k + "\"\\s*:\\s*(-?[0-9]+)").matcher(b);
      return m.find() ? Integer.parseInt(m.group(1)) : def; } catch (Throwable t) { return def; }
  }

  /** POST /res {i, level} —— 按"同 dp"档位切换：full|half|quarter（布局不变，只改像素） */
  static byte[] doRes(String body) {
    try {
      int i = jn(body, "i", 0); String lv = js(body, "level");
      if (i < 0 || i >= slots.length) return "{\"ok\":false,\"error\":\"bad i\"}".getBytes();
      if (lv == null) lv = "half";
      int div = "full".equals(lv) ? 1 : ("quarter".equals(lv) ? 4 : 2);
      int w = BASE[i][0] / div, h = BASE[i][1] / div, dpi = BASE[i][2] / div;
      String b = "{\"i\":" + i + ",\"width\":" + w + ",\"height\":" + h + ",\"dpi\":" + dpi + "}";
      return doResize(b, null);
    } catch (Throwable t) { return ("{\"ok\":false,\"error\":\"" + t + "\"}").getBytes(); }
  }

  static byte[] doResize(String body, Context ctx) {
    try {
      int i = jn(body, "i", -1), w = jn(body, "width", 0), h = jn(body, "height", 0), dpi = jn(body, "dpi", 0);
      if (i < 0 || i >= slots.length) return "{\"ok\":false,\"error\":\"bad i\"}".getBytes();
      Slot s = slots[i]; if (s == null) return "{\"ok\":false,\"error\":\"no slot\"}".getBytes();
      if (w <= 0 || h <= 0) return "{\"ok\":false,\"error\":\"w/h required\"}".getBytes();
      if (dpi <= 0) dpi = s.dpi;
      ImageReader nr = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 4);
      attach(s, nr);
      ImageReader old = s.reader;
      synchronized (s.lock) { if (s.held != null) { try { s.held.close(); } catch (Throwable t) {} s.held = null; } s.jpeg = null; }
      s.reader = nr; s.w = w; s.h = h; s.dpi = dpi;
      s.vd.setSurface(nr.getSurface());
      s.vd.resize(w, h, dpi);
      // ★ 2026-10-12（MoonClicker #121 记的 AImageReader 互锁）：**先拔 listener、再 close**。
      //   直接 close 会走 AImageReader_delete → ALooper::stop → requestExitAndWait 等在途回调，
      //   而回调可能正等同一把内部锁 → 互锁。只清自己的 listener 不够，必须显式摘掉。
      if (old != null) {
        try { old.setOnImageAvailableListener(null, null); } catch (Throwable t) {}
        try { Thread.sleep(200); old.close(); } catch (Throwable t) {}
      }
      return ("{\"ok\":true,\"i\":" + i + ",\"displayId\":" + s.id + ",\"width\":" + w + ",\"height\":" + h + ",\"dpi\":" + dpi + "}").getBytes();
    } catch (Throwable t) { return ("{\"ok\":false,\"error\":\"" + t + "\"}").getBytes(); }
  }

  static byte[] doLaunch(String body, Context ctx) {
    try {
      String pkg = js(body, "pkg"), act = js(body, "act");
      // ⛔ 宿主 App 永不搬上虚拟屏：跨屏 = 配置变更 = MainActivity 重建 = 界面重启
      if ("com.dshmobile.probe".equals(pkg))
        return "{\"ok\":false,\"error\":\"refused: host app must never be launched on a virtual display\"}".getBytes();
      int i = jn(body, "i", 0), wm = jn(body, "wm", 5);   // 默认 freeform(5)
      if (i < 0 || i >= slots.length) i = 0;
      Slot s = slots[i]; if (s == null) return "{\"ok\":false,\"error\":\"no slot\"}".getBytes();
      Intent it = new Intent();
      if (act != null && act.length() > 0) it.setComponent(new ComponentName(pkg, act.charAt(0) == '.' ? pkg + act : act));
      else { it.setPackage(pkg); it.setAction(Intent.ACTION_MAIN); it.addCategory(Intent.CATEGORY_LAUNCHER); }
      it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_MULTIPLE_TASK);
      ActivityOptions op = ActivityOptions.makeBasic();
      try { ActivityOptions.class.getMethod("setLaunchDisplayId", int.class).invoke(op, Integer.valueOf(s.id)); } catch (Throwable t) {}
      try { ActivityOptions.class.getMethod("setLaunchWindowingMode", int.class).invoke(op, Integer.valueOf(wm)); } catch (Throwable t) {}
      try { ctx.startActivity(it, op.toBundle()); }
      catch (SecurityException se) {
        PendingIntent pi = PendingIntent.getActivity(ctx, 0, it, PendingIntent.FLAG_CANCEL_CURRENT | PendingIntent.FLAG_IMMUTABLE, op.toBundle());
        pi.send();
      }
      return ("{\"ok\":true,\"pkg\":\"" + pkg + "\",\"i\":" + i + ",\"displayId\":" + s.id + ",\"wm\":" + wm + "}").getBytes("UTF-8");
    } catch (Throwable t) { return ("{\"ok\":false,\"error\":\"" + t + "\"}").getBytes(); }
  }

  // 诊断输出：Log.* 只在 logcat 里，容器看不到；直接打 stdout，start.sh 会 cat 出来
  static void log(String m) { System.out.println("[vsd] " + m); System.out.flush(); }
  static void sleep(long ms) { try { Thread.sleep(ms); } catch (Throwable t) {} }

  static void serve(final Context ctx) {
    new Thread(new Runnable() { public void run() {
      try {
        // ★ 2026-10-05：改绑回环。原实现 new ServerSocket(PORT) 会监听**所有网卡**
        //   （实测 `ss -ltn` 是 `*:18250`），而这条 HTTP 通道**完全没有鉴权** ——
        //   /list /frame /res /resize /launch 谁都能调，同网段的设备可以读虚拟屏画面、
        //   甚至往副屏拉应用。容器与手机共享网络栈，绑 127.0.0.1 对现有用法完全够用
        //   （vscreen / wxbridge / botctl 全都走 127.0.0.1）。
        ServerSocket ss = new ServerSocket(PORT, 50, InetAddress.getByName("127.0.0.1"));
        log("LISTEN 127.0.0.1:" + PORT + "（仅回环）");
        while (true) {
          Socket sock = null;
          try { sock = ss.accept(); }
          catch (Throwable t) { log("ACCEPT-ERR " + t); sleep(200); continue; }
          try {
            BufferedReader br = new BufferedReader(new InputStreamReader(sock.getInputStream()));
            String line = br.readLine();
            String path = (line == null || line.length() < 5) ? "/" : line.split(" ")[1];
            String method = (line == null || line.length() < 3) ? "GET" : line.split(" ")[0].toUpperCase();
            String bodyText = "";
            if ("POST".equals(method)) {
              int clen = 0; String hd;
              while ((hd = br.readLine()) != null && hd.length() > 0)
                if (hd.toLowerCase().startsWith("content-length:")) clen = Integer.parseInt(hd.substring(15).trim());
              if (clen > 0 && clen < 200000) { char[] cb = new char[clen]; int g = 0;
                while (g < clen) { int r = br.read(cb, g, clen - g); if (r < 0) break; g += r; }
                bodyText = new String(cb, 0, g); }
            }
            int idx = -1; String sub = path;
            if (path.length() > 2 && path.charAt(1) >= '0' && path.charAt(1) <= '9') {
              int k = path.indexOf('/', 1); if (k < 0) k = path.length();
              idx = Integer.parseInt(path.substring(1, k));
              sub = k < path.length() ? path.substring(k) : "/";
            }
            byte[] body; String ct;
            if (path.startsWith("/launch")) { body = doLaunch(bodyText, ctx); ct = "application/json"; }
            // ⚠️ /resize 必须排在 /res 之前：否则 `path.startsWith("/res")` 先命中，
            //    /resize 被当成 /res 执行 —— 2026-10-10 实测：POST /resize 传
            //    1264x2780@560，实际按 /res 默认 half 档执行成了 636x1400@280。
            else if (path.startsWith("/resize")) { body = doResize(bodyText, ctx); ct = "application/json"; }
            else if (path.startsWith("/res")) { body = doRes(bodyText); ct = "application/json"; }
            else if (path.startsWith("/list")) { body = listJson().getBytes("UTF-8"); ct = "application/json"; }
            else if (idx < 0 || idx >= slots.length || slots[idx] == null) { body = "bad index".getBytes("UTF-8"); ct = "text/plain"; }
            else if (sub.startsWith("/info")) {
              Slot s = slots[idx];
              body = ("{\"i\":" + idx + ",\"displayId\":" + s.id + ",\"width\":" + s.w + ",\"height\":" + s.h + ",\"frames\":" + s.frames + ",\"trusted\":true}").getBytes("UTF-8");
              ct = "application/json"; }
            else if (sub.startsWith("/frame")) {
              int maxW = 0; int qi = sub.indexOf("w=");
              if (qi >= 0) { try { maxW = Integer.parseInt(sub.substring(qi + 2).replaceAll("[^0-9].*$", "")); } catch (Throwable t) {} }
              boolean png = sub.indexOf("fmt=png") >= 0;
              byte[] l = jpeg(slots[idx], maxW, png);
              if (l == null) { body = "no-frame-yet".getBytes("UTF-8"); ct = "text/plain"; } else { body = l; ct = png ? "image/png" : "image/jpeg"; } }
            else { body = ("/list  /<i>/info  /<i>/frame[?w=N][&fmt=png]  POST /resize  POST /launch   (i=0.." + (slots.length - 1) + ")").getBytes("UTF-8"); ct = "text/plain"; }
            OutputStream o = sock.getOutputStream();
            o.write(("HTTP/1.1 200 OK\r\nContent-Type: " + ct + "\r\nContent-Length: " + body.length + "\r\nConnection: close\r\n\r\n").getBytes("UTF-8"));
            o.write(body); o.flush();
          } catch (Throwable t) { log("REQ-ERR " + t); }
          try { sock.close(); } catch (Throwable t) {}
        }
      } catch (Throwable t) { log("SRV-ERR " + t); }
    } }).start();
  }

  /** 创建虚拟屏。优先用 VirtualDisplayConfig（API 31+）带**请求刷新率**；
   *  失败就退回老的 createVirtualDisplay（无帧率控制），不让该功能影响虚拟屏本身能不能建起来。
   *  ⚠️ 必须反射：编译用的 android.jar 是 **API 23**，里面根本没有 VirtualDisplayConfig 类，
   *     直写编译不过。本机实机是 Android 16 / SDK 36，运行时有。
   *  参数（实测才能改）：SPEC 第 4 列。 */
  // ═══ 2026-10-12：独立 display group 已停用（这就是死锁根因）═══
  //   这里原本有一整套「独立 group 保活」：OWN_DISPLAY_GROUP 让 VD 拥有自己的
  //   power group，再配一个每 5 秒查 state、发现 OFF 就用 wakeUpWithDisplayId 叫醒的看门狗。
  //   MoonClicker（kuomartin/MoonClicker issue #6/#121、docs/virtual-display-pitfalls.md）
  //   用 AOSP 源码 + 多版本模拟器 + Note20 实机得出的结论：
  //     ① 独立 group 有自己的**闲置逾时**，主屏醒着也照样关；
  //     ② API 36+ 关掉后 Display.state 变 OFF、activity 被 pause、输入被丢弃；
  //     ③ **叫醒这个动作本身会反复触发 DisplayManagerService.requestDisplayState** ——
  //        正是 2026-10-12 那次 system_server watchdog(67s) 的卡点
  //        （android.display 卡在 requestDisplayStateInternal，
  //         surfaceflinger 卡在 VirtualDisplaySurface::dequeueBuffer）。
  //   而我们的机器人**本来就在锁屏时挂起**（bot.py::_wait_awake），根本不需要 VD
  //   在锁屏时活着 —— 这套保活等于白付死锁风险。所以：不要独立 group，让 VD 跟主屏
  //   同生共死，看门狗整个删除。
  //   代价（已知并接受）：锁屏 / 主屏关闭时虚拟屏上的应用会 pause；机器人和日课都不在
  //   那种状态下工作。
  //   参考：https://github.com/kuomartin/MoonClicker/blob/master/docs/virtual-display-pitfalls.md

  static Object displayInfo(int displayId) throws Exception {
    Class<?> dmgCls = Class.forName("android.hardware.display.DisplayManagerGlobal");
    Object inst = dmgCls.getMethod("getInstance").invoke(null);
    return dmgCls.getMethod("getDisplayInfo", int.class).invoke(inst, Integer.valueOf(displayId));
  }

  static int displayState(int displayId) {
    try {
      Object info = displayInfo(displayId);
      if (info == null) return -1;
      return info.getClass().getField("state").getInt(info);
    } catch (Throwable t) { return -1; }
  }

  static VirtualDisplay createVd(DisplayManager dm, String name, Slot s) {
    // ★ 2026-10-12：**刻意不带 OWN_DISPLAY_GROUP / ALWAYS_UNLOCKED**（详见上方注释）。
    //   带它们 = VD 有自己的 power group = 自己闲置逾时 = 需要看门狗反复叫醒 =
    //   撞 requestDisplayState 死锁。不要独立电源组，让 VD 跟主屏同生共死。
    int flags = DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC | TOUCH | OWNFOCUS | TRUSTED | OWN_CONTENT
        | ROTATES_WITH_CONTENT;
    if (s.fps > 0) {
      try {
        Class<?> cfgCls = Class.forName("android.hardware.display.VirtualDisplayConfig");
        Class<?> bCls = Class.forName("android.hardware.display.VirtualDisplayConfig$Builder");
        Object b = bCls.getConstructor(String.class, int.class, int.class, int.class)
            .newInstance(name, Integer.valueOf(s.w), Integer.valueOf(s.h), Integer.valueOf(s.dpi));
        // 方法名在不同版本上不同，两个候选都试一遍
        boolean set = false;
        for (String mn : new String[]{"setRequestedRefreshRate", "setRefreshRate"}) {
          try { bCls.getMethod(mn, float.class).invoke(b, Float.valueOf((float) s.fps)); set = true; break; }
          catch (NoSuchMethodException nsme) { }
        }
        if (!set) throw new NoSuchMethodException("Builder 没有 setRequestedRefreshRate/setRefreshRate");
        bCls.getMethod("setFlags", int.class).invoke(b, Integer.valueOf(flags));
        bCls.getMethod("setSurface", android.view.Surface.class).invoke(b, s.reader.getSurface());
        Object cfg = bCls.getMethod("build").invoke(b);
        // ★ 参数顺序随版本不同：**实测 Android 16 是 (Config, Handler, Callback)**，
        //   而老版本（API 31~35）是 (Config, Callback, Handler) —— 两个都试，哪个能用用哪个。
        //   （Probe 打印的真实签名：createVirtualDisplay(VirtualDisplayConfig,Handler,VirtualDisplay$Callback)）
        VirtualDisplay vd = null;
        try {
          java.lang.reflect.Method m = DisplayManager.class.getMethod(
              "createVirtualDisplay", cfgCls, Handler.class, VirtualDisplay.Callback.class);
          vd = (VirtualDisplay) m.invoke(dm, cfg, new Handler(ht.getLooper()), null);
        } catch (NoSuchMethodException nsme) {
          java.lang.reflect.Method m = DisplayManager.class.getMethod(
              "createVirtualDisplay", cfgCls, VirtualDisplay.Callback.class, Handler.class);
          vd = (VirtualDisplay) m.invoke(dm, cfg, null, new Handler(ht.getLooper()));
        }
        if (vd != null) { log("REFRESH " + name + " 请求 " + s.fps + "fps（VirtualDisplayConfig 生效）"); return vd; }
      } catch (Throwable t) {
        log("WARN " + name + " 带帧率创建失败，退回默认帧率：" + t);
      }
    } else {
      log("REFRESH " + name + " 未指定帧率（系统默认）");
    }
    return dm.createVirtualDisplay(name, s.w, s.h, s.dpi, s.reader.getSurface(), flags, null, new Handler(ht.getLooper()));
  }

  public static void main(String[] args) throws Exception {
    if (args != null && args.length > 0) { try { PORT = Integer.parseInt(args[0].trim()); } catch (Throwable t) {} }
    Looper.prepareMainLooper();
    Context ctx = shellCtx();
    ht = new HandlerThread("vsd"); ht.start();
    DisplayManager dm = (DisplayManager) ctx.getSystemService(Context.DISPLAY_SERVICE);
    for (int i = 0; i < SPEC.length; i++) {
      Slot s = new Slot(); s.w = SPEC[i][0]; s.h = SPEC[i][1]; s.dpi = SPEC[i][2];
      s.fps = SPEC[i].length > 3 ? SPEC[i][3] : 0;
      s.reader = ImageReader.newInstance(s.w, s.h, PixelFormat.RGBA_8888, 4);
      attach(s, s.reader);
      s.vd = createVd(dm, "dsh-trusted-" + i, s);
      s.id = s.vd.getDisplay().getDisplayId();
      slots[i] = s;
      System.out.println("TRUSTED[" + i + "] displayId=" + s.id + " " + s.w + "x" + s.h
          + " 请求帧率=" + (s.fps > 0 ? s.fps + "fps" : "默认"));
      System.out.flush();
    }
    startScreenWatch();
    serve(ctx);
    System.out.println("READY port=" + PORT);
    System.out.flush();
    Thread.sleep(86400000L);
  }
}
