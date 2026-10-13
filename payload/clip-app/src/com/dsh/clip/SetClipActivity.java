package com.dsh.clip;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.util.Base64;

/**
 * 极小的剪贴板设置工具。
 *
 * 为什么需要它：
 *   微信在虚拟屏上时，shell 的 `input text` 对中文无效（Android 层面限制），
 *   而这台 OPPO/Android 16 又把 `cmd clipboard` 移除了，shell 无法设置剪贴板。
 *   剪贴板是唯一能"干净地"把中文送进微信输入框的通道（粘贴不经过输入法组词）。
 *
 * 用法（base64 传参，避免中文/引号/换行转义问题）：
 *   am start -n com.dsh.clip/.SetClipActivity --es text64 "<base64>"
 * 也支持明文：
 *   am start -n com.dsh.clip/.SetClipActivity --es text "你好"
 *
 * 透明主题 + noHistory + finish，一闪而过，不影响前台。
 */
public class SetClipActivity extends Activity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Intent it = getIntent();
        String text = it != null ? it.getStringExtra("text") : null;
        String b64 = it != null ? it.getStringExtra("text64") : null;

        if (b64 != null && b64.length() > 0) {
            try {
                text = new String(Base64.decode(b64, Base64.DEFAULT), "UTF-8");
            } catch (Throwable ignored) {
                // base64 解不开就退回明文 extra
            }
        }

        if (text != null) {
            try {
                ClipboardManager cm =
                        (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                if (cm != null) {
                    cm.setPrimaryClip(ClipData.newPlainText("dsh", text));
                }
            } catch (Throwable ignored) {
            }
        }
        finish();
    }
}
