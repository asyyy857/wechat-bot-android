package dsh;

import android.content.Context;
import android.provider.Settings;
import java.io.*;
import java.net.*;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** 以 shell(uid 2000) 身份常驻的最小命令/设置守护（HTTP，仅绑 127.0.0.1，X-Token 门控）。
 *  POST /exec      正文=shell 命令            → 输出（含 [EXIT=n]）
 *  GET  /settings?ns=global&key=K            → 值
 *  POST /settings?ns=global&key=K&value=V    → ok
 *  GET  /health                              → uid/端口
 *
 *  2026-10-05 按 v1.1 文档源码落地，并补上文档自列的「未修三件套」：
 *   ① 已修 Content-Length 按【字节】记账 —— 旧版拿字节数当字符数读，中文命令必挂死 30s；
 *   ② 已修 waitFor() 阻塞取代 Thread.sleep(40) 轮询 —— 旧版给每条命令强加 ~40ms 税；
 *   ③ 已修 每请求一线程 —— 旧版单线程 accept，一条卡住的命令永久堵死整个端点；
 *   ④ 已修 子进程 unset CLASSPATH —— 旧版子命令继承守护 dex，pm 等 app_process 命令会卡死；
 *   ⑤ 已修 超时清理 kill -9 整个进程组 —— 旧版只 destroy() 直接子进程，孙进程握着管道不放。
 *  实测（2026-10-05，本机同设备对跑）：echo hi 45.7ms → 13.6ms（纯文档版不带 setsid 是 7.5ms；
 *  我们为了「超时能杀干净整组进程」多包了一层 setsid，代价约 6ms）；中文命令 挂死 → 14ms 正常返回。
 */
public final class ShellDaemon {
  static Context ctx;
  static synchronized Context ctx() throws Exception {
    if (ctx == null) {
      Class<?> at = Class.forName("android.app.ActivityThread");
      Object thr = at.getMethod("systemMain").invoke(null);
      Context sys = (Context) at.getMethod("getSystemContext").invoke(thr);
      ctx = sys.createPackageContext("com.android.shell", 0);
      System.out.println("SHD_CTX ok"); System.out.flush();
    }
    return ctx;
  }
  static String token = "none";
  static boolean hasSetsid = false;
  static final ExecutorService POOL = Executors.newCachedThreadPool();

  public static void main(String[] a) throws Exception {
    int port = a.length > 0 ? Integer.parseInt(a[0]) : 18242;
    System.out.println("SHD_BOOT pid=" + android.os.Process.myPid() + " uid=" + android.os.Process.myUid());
    System.out.flush();
    try { BufferedReader r = new BufferedReader(new FileReader("/sdcard/dsh/.shd-token")); String l = r.readLine(); r.close(); if (l != null && l.length() > 8) token = l.trim(); } catch (Throwable t) { System.out.println("SHD_TOKEN_ERR " + t); }
    System.out.println("SHD_TOKEN " + (token.length() > 8 ? "ok" : "none"));
    System.out.flush();
    // ③ 前置探测：命令各自独立进程组（setsid），超时时才能整组清理而不误伤守护自己
    try {
      Process p = new ProcessBuilder("sh", "-c", "command -v setsid >/dev/null 2>&1 && echo yes || echo no").redirectErrorStream(true).start();
      BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream(), "UTF-8"));
      String l = r.readLine(); p.waitFor(); r.close();
      hasSetsid = l != null && "yes".equals(l.trim());
    } catch (Throwable t) { hasSetsid = false; }
    System.out.println("SHD_SETSID " + (hasSetsid ? "yes" : "no"));
    System.out.flush();
    ServerSocket ss = new ServerSocket(port, 50, InetAddress.getByName("127.0.0.1"));
    System.out.println("SHD_READY port=" + port + " uid=" + android.os.Process.myUid());
    System.out.flush();
    // ③ 每请求一线程：一条卡住的命令不再堵死整个端点
    while (true) {
      final Socket s = ss.accept();
      POOL.execute(new Runnable() { public void run() {
        try { handle(s); } catch (Throwable t) {} finally { try { s.close(); } catch (Exception e) {} }
      }});
    }
  }

  static void handle(Socket s) throws Exception {
    s.setSoTimeout(30000);
    BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), "UTF-8"));
    String req = in.readLine();
    if (req == null) return;
    String[] p = req.split(" ");
    String method = p[0], path = p.length > 1 ? p[1] : "/";
    int len = 0; String tok = null, line;
    while ((line = in.readLine()) != null && line.length() > 0) {
      String low = line.toLowerCase();
      if (low.startsWith("content-length:")) len = Integer.parseInt(line.substring(15).trim());
      if (low.startsWith("x-token:")) tok = line.substring(8).trim();
    }
    // ① Content-Length 是【字节数】，不能拿它当【字符数】去读：
    //    中文 1 字符 = 3 字节 → 声明的字节数永远大于会到达的字符数 → 读循环死等 → 挂到 30s 超时。
    //    按字节记账：逐字符读，累加该字符的 UTF-8 字节长度，读满 len 字节为止。
    StringBuilder bs = new StringBuilder();
    int bytes = 0;
    while (bytes < len) {
      int c = in.read();
      if (c < 0) break;
      char ch = (char) c;
      bs.append(ch);
      if (ch < 0x80) bytes += 1;
      else if (ch < 0x800) bytes += 2;
      else if (Character.isHighSurrogate(ch)) { /* 4 字节字符的高位，等低位一起计 */ }
      else if (Character.isLowSurrogate(ch)) bytes += 4;
      else bytes += 3;
    }
    String bodyStr = bs.toString();
    String out; int code = 200;
    if (!token.equals(tok)) { out = "DENIED\n"; code = 403; }
    else if (path.startsWith("/health")) out = "uid=" + android.os.Process.myUid() + " ok\n";
    else if (path.startsWith("/exec")) out = exec(bodyStr);
    else if (path.startsWith("/settings")) out = settings(path, bodyStr);
    else { out = "not found\n"; code = 404; }
    byte[] b = out.getBytes("UTF-8");
    OutputStream o = s.getOutputStream();
    o.write(("HTTP/1.1 " + code + " OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: " + b.length + "\r\nConnection: close\r\n\r\n").getBytes("UTF-8"));
    o.write(b); o.flush();
  }

  static String exec(String cmd) throws Exception {
    if (cmd == null || cmd.trim().isEmpty()) return "[EXIT=2] empty\n";
    java.util.List<String> argv = new java.util.ArrayList<String>();
    if (hasSetsid) argv.add("/system/bin/setsid");
    argv.add("sh"); argv.add("-c"); argv.add(cmd);
    ProcessBuilder pb = new ProcessBuilder(argv);
    // ④ 子进程不能继承守护的 CLASSPATH（否则内部再用 app_process 的命令会捡到守护 dex 卡死）
    try { pb.environment().remove("CLASSPATH"); } catch (Throwable t) {}
    final Process pr = pb.start();
    final int pid = pidOf(pr);
    final StringBuilder sb = new StringBuilder();
    Thread t1 = drain(pr.getInputStream(), sb), t2 = drain(pr.getErrorStream(), sb);
    // ② 别轮询：旧实现每 40ms 轮询 exitValue()，等于给每条命令强加约 40ms 税
    //    （实测 /exec "echo hi" 恒为 46~50ms，抖动几乎为 0，就是这个 40ms 的形状）。
    //    改成 waitFor() 阻塞 + 看门狗线程保留 20s 超时。
    final boolean[] killed = {false};
    Thread watchdog = new Thread(new Runnable() { public void run() {
      try {
        Thread.sleep(20000L);
        killed[0] = true;
        killGroup(pr, pid);   // ⑤ 整组清理，孙进程也带走
      } catch (Throwable t) {}
    }});
    watchdog.setDaemon(true); watchdog.start();
    boolean done = false;
    try { pr.waitFor(); done = true; } catch (InterruptedException ie) {}
    watchdog.interrupt();
    if (killed[0]) sb.append("[TIMEOUT]\n");
    if (!killed[0] && !done) { try { pr.destroy(); } catch (Throwable t) {} }
    t1.join(1500); t2.join(1500);
    sb.append("[EXIT=").append(killed[0] ? -9 : (done ? pr.exitValue() : -9)).append("]\n");
    return sb.toString();
  }

  // ⑤ 超时清理：kill -9 整个进程组（含孙进程），但绝不碰守护自己的进程组。
  //    只在【超时】时调用；正常结束的命令可能有故意留着的后台进程（如 nohup 起的守护），不能动。
  static void killGroup(Process pr, int pid) {
    try {
      int mine = pgidOf(android.os.Process.myPid());
      int g = pid > 0 ? pgidOf(pid) : -1;
      if (g > 0 && g != mine) {
        Process k = new ProcessBuilder("sh", "-c", "kill -9 -" + g).redirectErrorStream(true).start();
        k.waitFor();
      }
    } catch (Throwable t) {}
    try { pr.destroy(); } catch (Throwable t) {}
  }

  // /proc/<pid>/stat 第 5 个字段就是进程组 id；
  // comm 字段可能含空格/括号，所以从最后一个 ')' 之后切。
  static int pgidOf(int pid) {
    try {
      BufferedReader r = new BufferedReader(new FileReader("/proc/" + pid + "/stat"));
      String l = r.readLine(); r.close();
      if (l == null) return -1;
      int i = l.lastIndexOf(')');
      if (i < 0) return -1;
      String[] f = l.substring(i + 2).trim().split("\\s+");
      return Integer.parseInt(f[2]);
    } catch (Throwable t) { return -1; }
  }

  // Android libcore 的 Process 实现里有 pid 字段；拿不到就退化为 destroy()
  static int pidOf(Process p) {
    try {
      java.lang.reflect.Field f = p.getClass().getDeclaredField("pid");
      f.setAccessible(true);
      return f.getInt(p);
    } catch (Throwable t) { return -1; }
  }

  static Thread drain(final InputStream is, final StringBuilder sb) {
    Thread t = new Thread(new Runnable() { public void run() {
      try { BufferedReader r = new BufferedReader(new InputStreamReader(is, "UTF-8")); String l;
        while ((l = r.readLine()) != null) { synchronized (sb) { sb.append(l).append('\n'); } }
      } catch (Throwable e) {}
    }});
    t.start(); return t;
  }

  static String settings(String path, String body) throws Exception {
    String ns = q(path, "ns"), key = q(path, "key"), val = q(path, "value");
    if (key == null || key.isEmpty()) return "need key\n";
    if ("global".equals(ns)) {
      if (val == null) return String.valueOf(Settings.Global.getString(ctx().getContentResolver(), key)) + "\n";
      Settings.Global.putString(ctx().getContentResolver(), key, val); return "ok\n";
    } else if ("secure".equals(ns)) {
      if (val == null) return String.valueOf(Settings.Secure.getString(ctx().getContentResolver(), key)) + "\n";
      Settings.Secure.putString(ctx().getContentResolver(), key, val); return "ok\n";
    } else {
      if (val == null) return String.valueOf(Settings.System.getString(ctx().getContentResolver(), key)) + "\n";
      Settings.System.putString(ctx().getContentResolver(), key, val); return "ok\n";
    }
  }

  static String q(String path, String k) {
    int i = path.indexOf('?'); if (i < 0) return null;
    for (String kv : path.substring(i + 1).split("&")) {
      int e = kv.indexOf('=');
      if (e > 0 && kv.substring(0, e).equals(k)) {
        try { return URLDecoder.decode(kv.substring(e + 1), "UTF-8"); } catch (Exception x) { return kv.substring(e + 1); }
      }
    }
    return null;
  }
}
