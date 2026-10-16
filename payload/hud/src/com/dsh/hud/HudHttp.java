package com.dsh.hud;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.util.HashMap;
import java.util.Map;

/**
 * 极简 HTTP 服务（纯 JDK，无依赖）。
 * 容器与手机共享 127.0.0.1，所以容器里一条 curl 就能更新浮窗 —— 不用 adb。
 *
 *   POST /say    body: "action=正在打开微信\ndetail=点击底部第一个会话"
 *   GET  /say?action=...&title=...&detail=...
 *   GET  /ping   返回当前状态（容器脚本用它判断浮窗在不在）
 *   GET  /hide   隐藏浮窗      GET /show 重新显示
 *   GET  /quit   停止服务
 */
public class HudHttp {

    public static void start(final HudService svc) {
        Thread t = new Thread(new Runnable() {
            public void run() { loop(svc); }
        }, "hud-http");
        t.setDaemon(true);
        t.start();
    }

    private static void loop(HudService svc) {
        ServerSocket ss;
        try {
            ss = new ServerSocket(HudService.PORT, 16, InetAddress.getByName("127.0.0.1"));
        } catch (Throwable t) {
            return;   // 端口占用：浮窗照常显示，只是收不到推送
        }
        while (true) {
            Socket s = null;
            try {
                s = ss.accept();
                handle(svc, s);
            } catch (Throwable ignored) {
            } finally {
                if (s != null) {
                    try { s.close(); } catch (Throwable ignored) { }
                }
            }
        }
    }

    private static void handle(HudService svc, Socket s) {
        try {
            s.setSoTimeout(5000);
            InputStream in = s.getInputStream();

            ByteArrayOutputStream headBuf = new ByteArrayOutputStream();
            int b;
            while ((b = in.read()) >= 0) {
                headBuf.write(b);
                byte[] a = headBuf.toByteArray();
                int n = a.length;
                if (n >= 4 && a[n - 4] == 13 && a[n - 3] == 10 && a[n - 2] == 13 && a[n - 1] == 10) break;
                if (n > 32768) break;
            }
            String head = new String(headBuf.toByteArray(), "UTF-8");
            String[] lines = head.split("\r\n");
            if (lines.length == 0 || lines[0].length() == 0) { respond(s, "ERR"); return; }

            String[] req = lines[0].split(" ");
            String method = req.length > 0 ? req[0] : "GET";
            String path = req.length > 1 ? req[1] : "/";

            int len = 0;
            for (String ln : lines) {
                int c = ln.indexOf(':');
                if (c > 0 && ln.substring(0, c).trim().equalsIgnoreCase("Content-Length")) {
                    try { len = Integer.parseInt(ln.substring(c + 1).trim()); } catch (Throwable ignored) { }
                }
            }
            String body = "";
            if (len > 0) {
                byte[] buf = new byte[len];
                int got = 0;
                while (got < len) {
                    int r = in.read(buf, got, len - got);
                    if (r < 0) break;
                    got += r;
                }
                body = new String(buf, 0, got, "UTF-8");
            }

            respond(s, route(svc, method, path, body));
        } catch (Throwable ignored) {
        }
    }

    private static String route(HudService svc, String method, String path, String body) {
        String p = path, query = "";
        int q = path.indexOf('?');
        if (q >= 0) {
            p = path.substring(0, q);
            query = path.substring(q + 1);
        }

        Map<String, String> kv = new HashMap<String, String>();
        parseKv(query, kv);
        parseKv(body, kv);

        if (p.equals("/say")) {
            String action = kv.get("action");
            String title = kv.get("title");
            String detail = kv.get("detail");
            if (action == null && title == null && detail == null && body != null) {
                String raw = body.trim();
                if (raw.length() > 0) action = raw;   // 裸 body 当成一句话
            }
            svc.set(title, action, detail);
            return "OK " + svc.status();
        }
        if (p.equals("/hide")) { svc.hide(); return "OK hidden"; }
        if (p.equals("/hidein")) {
            long ms = 2500L;
            try {
                String v = kv.get("ms");
                if (v != null) ms = Long.parseLong(v.trim());
            } catch (Throwable ignored) { }
            svc.hideIn(ms);
            return "OK hideIn=" + ms + "ms";
        }
        if (p.equals("/show")) { svc.show(); return "OK shown"; }
        if (p.equals("/vibrate")) {
            // ★ 2026-10-08（用户要求）：让手机震一下 —— 干完活的"叫你"信号。
            //   参数：?ms=400&times=1（都可不传）
            long ms = 400L;
            int times = 1;
            try {
                String v = kv.get("ms");
                if (v != null) ms = Long.parseLong(v.trim());
                String t = kv.get("times");
                if (t != null) times = Integer.parseInt(t.trim());
            } catch (Throwable ignored) { }
            String r = svc.vibrate(ms, times);
            return "vibrate " + ms + "ms x" + times + " -> " + r;
        }
        if (p.equals("/alarm")) {
            // ★ 2026-10-10：抢屏提醒 —— 高优先级通知（弹横幅 + 响 + 震），
            //   一直挂到 /alarmoff 为止。参数：?title=&text=&ms=&times=
            String title = kv.get("title");
            String text = kv.get("text");
            long ms = 400L;
            int times = 3;
            try {
                String v = kv.get("ms");
                if (v != null) ms = Long.parseLong(v.trim());
                String n = kv.get("times");
                if (n != null) times = Integer.parseInt(n.trim());
            } catch (Throwable ignored) { }
            return "alarm -> " + svc.alert(title, text, ms, times);
        }
        if (p.equals("/alarmoff")) {
            return "alarmoff -> " + svc.alertOff();
        }
        if (p.equals("/cost")) {
            // ★ 2026-10-09：查「现在是波峰（全价）还是波谷（半价）」。
            //   ?at=<毫秒时间戳> 可以问任意时刻（测试/预判用），不传就是现在。
            long at = System.currentTimeMillis();
            try {
                String v = kv.get("at");
                if (v != null) at = Long.parseLong(v.trim());
            } catch (Throwable ignored) { }
            return "cost=" + (HudService.isOffPeak(at) ? "offpeak(半价)" : "peak(全价)")
                    + " nextSwitch=" + HudService.nextSwitch(at)
                    + " line=" + HudService.costLine(at);
        }
        if (p.equals("/quit")) { svc.shutdown(); return "OK bye"; }
        return svc.status();
    }

    private static void parseKv(String s, Map<String, String> out) {
        if (s == null || s.length() == 0) return;
        String[] parts = s.split("[&\\r\\n]+");
        for (String p : parts) {
            if (p.length() == 0) continue;
            int e = p.indexOf('=');
            if (e <= 0) continue;
            out.put(dec(p.substring(0, e).trim()), dec(p.substring(e + 1)));
        }
    }

    private static String dec(String s) {
        try { return URLDecoder.decode(s, "UTF-8"); } catch (Throwable t) { return s; }
    }

    private static void respond(Socket s, String body) {
        try {
            byte[] out = body.getBytes("UTF-8");
            String head = "HTTP/1.1 200 OK\r\n"
                    + "Content-Type: text/plain; charset=utf-8\r\n"
                    + "Content-Length: " + out.length + "\r\n"
                    + "Connection: close\r\n\r\n";
            OutputStream os = s.getOutputStream();
            os.write(head.getBytes("UTF-8"));
            os.write(out);
            os.flush();
        } catch (Throwable ignored) {
        }
    }
}
