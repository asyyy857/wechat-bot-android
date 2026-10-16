package com.dsh.hud;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;

/**
 * 启动入口：把浮窗服务拉起来，然后立刻退出。
 * 走 Activity 而不是 start-foreground-service，是为了在 Android 8+ 的
 * 后台启动限制下也一定能起来（Activity 在前台时启动服务是合法的）。
 */
public class BootActivity extends Activity {
    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        try {
            startService(new Intent(this, HudService.class));
        } catch (Throwable ignored) {
        }
        finish();
    }
}
