# TRAE 每日自动签到（GitHub Actions 版）

在 GitHub 云端定时执行 TRAE 每日签到，**与本机是否开机、是否联网完全无关** ——
这就是「关机 / 休眠导致漏签」的根本解。

- 每天北京时间 **08:00** 跑一次，**10:00** 兜底再跑一次（已签到会自动跳过，不会重复领取）
- 支持 **多账号**：配 `TRAE_SESSION_2` / `TRAE_SESSION_3` … 即可
- 支持 **飞书机器人** 推送结果（可选）

---

## 一、接口原理

签到接口不认 `Bearer`，社区验证过的正确链路是三段：

```
X-Cloudide-Session (HttpOnly Cookie)
   │  POST https://www.trae.cn/cloudide/api/v3/common/GetUserToken
   ▼
短期 JWT
   │  Authorization: Cloud-IDE-JWT <jwt>
   │  X-User-Region: cn
   │  x-device-id: <16 位数字>        ← 缺失会返回 9004
   ▼
GET  /trae/api/v2/ug/checkin_credits/status   查询今日积分
POST /trae/api/v2/ug/checkin_credits/claim    领取
```

已处理的异常码：`1001` 凭证失效（需重抓会话）、`9074` / `429` 限流（15~30s 随机退避，最多重试 10 次）。

---

## 二、获取会话凭证 `X-Cloudide-Session`

> ⚠️ 这是唯一的敏感信息，等同于账号登录态。**只存进 GitHub Secrets，不要提交到仓库。**

1. 用浏览器打开 <https://www.trae.cn> 并**登录**
2. `F12` 打开开发者工具 → `Application`（应用）面板
3. 左侧 `Storage` → `Cookies` → `https://www.trae.cn`
4. 找到名为 **`X-Cloudide-Session`** 的那一行，复制它的 **Value**
5. `device-id` 可以不填（缺省会随机生成）；要填的话在 Console 执行
   `localStorage` 里找，或任意 16 位数字即可

会话有效期约 **14 天**，过期后 Actions 会跑红，重抓一次更新 Secret 即可。

---

## 三、配置 Secrets

仓库页面 → `Settings` → `Secrets and variables` → `Actions` → `New repository secret`：

| Secret 名 | 必填 | 说明 |
|---|---|---|
| `TRAE_SESSION` | ✅ | 账号 1 的 `X-Cloudide-Session` 值 |
| `TRAE_DEVICE_ID` | ❌ | 账号 1 的设备号，缺省随机 |
| `TRAE_SESSION_2` | ❌ | 账号 2 的会话；填了才会读 `TRAE_SESSION_3`，依次类推 |
| `TRAE_DEVICE_ID_2` | ❌ | 账号 2 的设备号 |
| `FEISHU_WEBHOOK` | ❌ | 飞书机器人 webhook，用于推送签到结果 |

---

## 四、启用定时

1. 推送后 Actions 会自动跑一次（`push` 触发），用于验证凭证是否正确
2. 如果定时任务没出现在列表中，去 `Actions` 页面点一次 `Run workflow` 手动触发即可激活
3. 之后每天自动执行，无需干预

> 注意：GitHub 对**长期无活动的仓库**会停用定时触发器。
> 若连续 60 天仓库无任何提交，schedule 会被静默关闭 —— 偶尔改点东西或手动跑一次即可保持活跃。

---

## 五、安全建议

- 本仓库建议设为 **Private**。公开仓库本身不会泄露 Secrets，但会暴露你的账号行为模式
- 一旦怀疑会话泄露：在 trae.cn 退出登录即可让其立即失效，然后重抓并更新 Secret
- 本仓库不包含、也不需要任何本地 IDE 文件

---

## 六、本地版

如果你还想在本机跑（作为第二重保险），需要的是另一个脚本 `trae_checkin.py`（Windows 版），
它支持从浏览器登录态自动抓取会话，不在本仓库维护范围内。
