# 华山医院（浦东院区）抢号监测器

GitHub Actions 云端每 5 分钟查一次号，出现可约号源（放号新释放 / 他人退号弃号）时，经方糖（Server酱）推送微信提醒。**电脑关机也照常运行。**

## 监测对象（仅浦东院区 hospitalid=209）

| 医生 | 科室 | 114 id | 目标班次 | 价格 |
|---|---|---|---|---|
| 齐思思 | 皮肤科 | 56654 | 周二上午 / 周四下午 | 40 元专家门诊 |
| 周丽诺 | 内分泌科 | 45011 | 周四上午 / 周四下午 | 50 元专家门诊 |

## 数据源

114 名医导航移动端**免登录**接口：

```
GET https://m.114-91.com/common/api?cmd=DoctorList&sr=1&keyword=<姓名>&hospitalid=209&ipage=1&pagesize=20&sectiontype=0
```

信号：`remainnum > 0`（剩余可约号数）。响应约 0.1 秒，稳定。

## 配置 Secrets

仓库 `Settings → Secrets and variables → Actions → New repository secret`：

| 名称 | 值 |
|---|---|
| `SERVERCHAN_SENDKEY` | 方糖（Server酱）SendKey（SCT 开头）。微信内「方糖」服务号菜单查。 |

> SendKey 获取（sct.ftqq.com 在手机微信浏览器里跳登录打不开，请勿用网页）：
> **首选**：微信 → 通讯录 → 公众号/服务号 → 搜「方糖」→ 进入 → 底部菜单「我的 / 推送 / SendKey」查 SCT 开头密钥；或翻绑定成功当天方糖发来的推送消息（通常含密钥）。
> 备选网页：`sc.ftqq.com`（旧版，登录后首页显示 32 位密钥）——但旧版与 Turbo 版 SCT 密钥是两套系统，若绑的是方糖 Turbo 版则此键不匹配。

## 本地测试

```bash
# 空跑（不真正推送，只验证流程）
SERVERCHAN_SENDKEY=x SKIP_PUSH=true python monitor.py

# 强制「齐思思有号」真推送（需真实 SendKey）
SERVERCHAN_SENDKEY=<你的key> FORCE_REMAIN=1 python monitor.py

# 指定医生强制有号
SERVERCHAN_SENDKEY=<你的key> FORCE_REMAIN=45011:1 python monitor.py
```

## 手动触发（GitHub 网页 / 手机）

`Actions → huashan-slot-monitor → Run workflow`，可填 `force_remain`、`skip_push`。

## 候补机制

约满后在医院 App 点「候补」即进入排队，有人退号系统**自动补位**，无需手动刷新。本监测器只报「可直接预约」的号源（放号新释放 + 候补未覆盖的瞬时弃号）。

## 注意事项

- 号数为 114 聚合值，可能含特需（600 元）/ 涉外档或其它班次，具体班次与价格请以 App 内显示为准。
- 接口一旦变更/失效，workflow 会以非零码失败 → GitHub 向仓库管理员发邮件（天然哨兵）。
- 公开仓库 60 天无活动，`scheduled` 会自动禁用；每月手动 `workflow_dispatch` 一次即可保活。
- 免费版 Server酱 5 条/天：内置 15 分钟冷却 + 每日 5 条硬顶；不够可订阅 39 元/年（1000 条/天）。

## 文件说明

- `monitor.py`：监测主脚本，Python3 标准库零依赖。
- `state.json`：跨 run 状态（actions/cache 持久化），已 gitignore。
