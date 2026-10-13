# 更新日志

## v2.0.0（2026-10-13）

**这一版的核心是「正规化 + 可发布」**：程序与用户数据彻底分开，安装可重复
（升级不丢数据），并新增字体接口。

### 新增

- **程序 / 用户数据分离**。目录从 `wechat-auto/`（代码数据混装）改为：
  - `wechat-bot/app/` —— 程序（升级时整体覆盖）
  - `wechat-bot/data/` —— 你的数据（升级时一个字节都不动）
  - `wechat-bot/logs/` —— 日志（原来是 `/tmp/wxbot.log`，重启就没）
  - 新增统一路径层 `app/paths.sh` 与 `app/wxauto/paths.py`；数据结构不变，
    升级无需迁移数据（`data/.schema_version = 2`）。
- **字体接口**（`read.font` + `python3 -m wxauto.fontprofile`）：
  配置项 / 自动测量 / 字体档案三种用法，适配「别人的微信不是默认字体」。
  默认关闭，关着时行为与旧版完全一致。详见《字体与OCR.md》。
- **`{{me}}` 昵称占位符**：`rules.yaml` 里可以写 `拍了拍{{me}}`，
  安装者填了 `me.nicknames` 就自动生效（发布版不再写死作者昵称）。
- **`install.sh`**：环境自检 / 自动备份 / 铺程序 / 数据目录保留 /
  从旧版 `wechat-auto` 迁移（`--migrate`）/ 新增配置项提示 / `--dry-run`。
- **日志轮转**：Python 侧 `logs/bot.log`（2MB×3，可配）；
  shell 侧 `logs/console.log`（10MB×5，启动时滚动）。
- 数据模板 `app/defaults/`（含 `README-数据目录.md`，说明每个文件能否删）。

### 变更

- 脚本、配置里的路径全部改为**按自身位置推导**（不再写死 `/sdcard/dsh/wechat-auto`），
  安装位置因此可以整体搬移。
- 急停文件统一到 `data/STOP.txt`（原来是 `wechat-auto/STOP.txt`，面板却在 data 找，
  两边口径不一致）。
- DeepSeek Key 的第三种来源从 `app/.deepseek_key` 改为 `data/.deepseek_key`。

### 发布版（压缩包）额外做了

- **只保留「猫娘」一套人设**（其余四套不在发布包里），并清理了人设里的
  个人信息（作者昵称、个人游戏经历等）。
- `config.yaml` 的群名 / 昵称改为占位符；`rules.yaml` 的「拍了拍某某」改为 `{{me}}`。
- 知识库（`群知识.md` / `逆水寒常识.md` 等）**不带**，改为提示用户自行添加
  （模板见 `data/group-knowledge-可自行添加.md`）。
- 剔除：`chat_log.json`、`记忆库.json` 内容、名字库、密钥、开发文档与历史备份。

### 继承自 2026-10-12 / 10-13 的修复（本版之前的改动）

- OCR 换 **PP-OCRv6 small**（31MB，3.2 秒/帧），标题保留 v4 + 读空回退。
- 低置信行复核（A）+ 同帧昵称一致性校正（B）+ 名字库切分（C）。
- 气泡漏行补救（D）：气泡里换行的第二行被漏检时补读。
- 昵称被「头像列」过滤误杀的根治（`chat_log.json` 里 sender 大面积空串的元凶）。
- 重复发消息修复（截断前缀去重 + 回声字袋比对）。
- 「读不出来就别回」：回复里自白乱码/眼睛坏 → 直接丢弃不发。
- 发图抢屏补预告（你在打字时先弹浮窗 + 震动，3 秒后照抢）。

### 适配

- 微信 Android **8.0.78**（2026-10-13 实测）；虚拟屏守护不带
  `OWN_DISPLAY_GROUP` / `ALWAYS_UNLOCKED`（带了会冻死 system_server）。

---

## v1.x（2026-10-12 及更早）

见 `docs/`（发布包不含开发文档）与项目内的 `历史记录.md`。
