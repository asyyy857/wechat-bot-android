package com.dsh.clip;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.inputmethodservice.InputMethodService;
import android.util.Base64;
import android.view.View;
import android.view.inputmethod.InputConnection;

/**
 * DSH 无键盘输入法 —— 往「当前焦点输入框」提交文字 / 清空输入框。
 *
 * 为什么需要它：
 *   微信在虚拟屏上时，shell `input text` 只对 ASCII 可靠（中文进不去），
 *   剪贴板粘贴要长按唤菜单、极易误触；而输入法是系统合法通道，
 *   commitText() 能原样送进任意 Unicode，deleteSurroundingText() 能清空草稿。
 *
 * 用法：
 *   am broadcast -a com.dsh.clip.COMMIT --es text64 "<base64>"   # 提交文字
 *   am broadcast -a com.dsh.clip.COMMIT --es clear "1"          # 清空输入框
 *   两个 extra 同时给 = 先清空再提交（避免旧草稿拼在前面）
 *
 * 不提供任何键盘界面（onCreateInputView 返回 null），不会在物理屏弹键盘。
 */
public class DshIme extends InputMethodService {

    public static final String ACTION = "com.dsh.clip.COMMIT";

    private BroadcastReceiver rx;

    @Override
    public void onCreate() {
        super.onCreate();
        rx = new BroadcastReceiver() {
            @Override
            public void onReceive(Context c, Intent i) {
                String text = i != null ? i.getStringExtra("text") : null;
                String b64 = i != null ? i.getStringExtra("text64") : null;
                String clear = i != null ? i.getStringExtra("clear") : null;
                if (b64 != null && b64.length() > 0) {
                    try {
                        text = new String(Base64.decode(b64, Base64.DEFAULT), "UTF-8");
                    } catch (Throwable ignored) {
                    }
                }
                boolean doClear = "1".equals(clear) || "true".equalsIgnoreCase(clear);
                if (text == null && !doClear) return;
                try {
                    InputConnection ic = getCurrentInputConnection();
                    if (ic == null) return;
                    if (doClear) {
                        ic.deleteSurroundingText(2000, 2000);
                    }
                    if (text != null) {
                        ic.commitText(text, 1);
                    }
                } catch (Throwable ignored) {
                }
            }
        };
        registerReceiver(rx, new IntentFilter(ACTION));
    }

    /** 不画键盘：保持物理屏干净。 */
    @Override
    public View onCreateInputView() {
        return null;
    }

    @Override
    public void onDestroy() {
        if (rx != null) {
            try { unregisterReceiver(rx); } catch (Throwable ignored) { }
            rx = null;
        }
        super.onDestroy();
    }
}
