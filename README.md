# TRAE 每日自动签到（GitHub Actions 版）

在 GitHub 云端定时执行 TRAE 每日签到，**与本机是否开机、是否联网完全无关** ——
这就是「关机 / 休眠导致漏签」的根本解。

- 每天北京时间 **08:07** 自动签到（**无需任何手动操作**）
- **08:47 / 12:07 / 20:07** 三次兜底（主跑若撞上限流或排队延误）
- 四次都跑是安全的：脚本先查 `status`，已签到直接跳过，**绝不会重复领取**

> **为什么不是 0 点？** 实测过：设 `00:05` 实际 `03:24` 才启动（延迟 3 小时 19 分），
> 设 `00:45` 实际 `03:33`。北京 0 点 = UTC 16:00，是 GitHub Actions 全球最拥堵的时段，
> 「准点 0 点」在云端根本做不到。而且签到积分是定额发放、不是先到先得，
> 抢 0 点没有任何额外收益，只换来排队 + 风控两个坏处。
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
| `TRAE_MACHINE_ID` | ⚠️ 强烈建议 | 账号 1 的机器号（`telemetry.machineId`），见下方「9074 排障」 |
| `TRAE_DEVICE_ID` | ⚠️ 强烈建议 | 账号 1 的**注册设备号**，取自 `storage.json` 里 `iCubeAuthInfo://icube-dc:<数字>` 的数字部分，见下方「9074 排障」 |
| `TRAE_SESSION_2` | ❌ | 账号 2 的会话；填了才会读 `TRAE_SESSION_3`，依次类推 |
| `TRAE_DEVICE_ID_2` | ❌ | 账号 2 的设备号 |
| `TRAE_MACHINE_ID_2` | ❌ | 账号 2 的机器号 |
| `FEISHU_WEBHOOK` | ❌ | 飞书机器人 webhook，用于推送签到结果 |

### 9074 排障：换取 JWT 正常，但领取积分一直被拒

现象：日志里 `已换取新 JWT` 和状态查询都正常，只有 `claim` 反复返回
`code=9074`，重试到超时后 Actions 标红。

这是**风控拦截**，不是凭证失效 —— 换 JWT、加退避都解决不了。按以下顺序排查：

1. **两个设备指纹都要填成真值（首要）**
   只配 `TRAE_SESSION` 时，脚本只能拿会话哈希伪造指纹，而服务端是按注册
   指纹校验的，陌生设备会被**更严格地限流**（这正是 `9074` 的常见成因）。

   - `TRAE_MACHINE_ID` ← `storage.json` 的 `telemetry.machineId`（64 位）
   - `TRAE_DEVICE_ID` ← **注册设备号**，即 `storage.json` 里
     `iCubeAuthInfo://icube-dc:<16位数字>` 这个**键名**中的数字部分

   在本机执行 `python capture_device.py --copy machine` /
   `--copy device` 分别取值并填入对应 Secret。

   ⚠️ 别把 `telemetry.devDeviceId`（UUID 形态）当成设备号；服务端不认 UUID，
   会触发更严格限流。设备号必须是纯数字。
2. **仍被拒 → 再考虑机房 IP 风控**
   GitHub 托管 runner 跑在 Azure 数据中心，服务端对机房 IP 收紧是常见做法。
   这种情况改代码没用，出路：换国内常开服务器（VPS/青龙/云函数），或
   以本机定时任务为主力、云端当可失败的备份。

> 判断依据：本机版 `trae_checkin.py` 一直能成功领取，云端失败 —— 差异在
> 「指纹」或「IP」两项；先把指纹补齐并验证，才能把 IP 这一项定为结论。

---

## 四、启用定时

1. 配好 Secret 后，到 `Actions` 页面点一次 `Run workflow` 手动触发，验证凭证是否正确
2. 之后**每天自动执行，完全无需干预**

> 附：仓库内还有一个 `keepalive.yml`，每月提交一次空 commit。
> 因为 GitHub 会对「连续 60 天无提交」的仓库静默禁用 schedule —— 不报错也不通知，
> 到时 Session 还有效但签到再也不跑了。保活工作流就是为堵这个坑，无需理会。

---

## 五、安全建议

- 本仓库建议设为 **Private**。公开仓库本身不会泄露 Secrets，但会暴露你的账号行为模式
- 一旦怀疑会话泄露：在 trae.cn 退出登录即可让其立即失效，然后重抓并更新 Secret
- 本仓库不包含、也不需要任何本地 IDE 文件

---

## 六、本地版

如果你还想在本机跑（作为第二重保险），需要的是另一个脚本 `trae_checkin.py`（Windows 版），
它支持从浏览器登录态自动抓取会话，不在本仓库维护范围内。
