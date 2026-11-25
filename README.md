# 到梦空间活动看板

在 Windows 浏览器里查看自己能报名的校园活动。打开看板，最先看到的是**你符合条件、最近要开放报名**的活动，每张卡片都会显示报名开始时间、倒计时和剩余名额。

**[下载 v2.5.1 Windows 完整包](https://github.com/Alix974278/dream-monitor-qiguai/releases/download/v2.5.1/dream-monitor-v2.5.1-windows-x64.zip)** · [在线浏览源码](dream-monitor/) · [版本下载与更新记录](https://github.com/Alix974278/dream-monitor-qiguai/releases)

## 看板能做什么

- **挑出能报的活动。** “我能报名”同时收录已确认符合条件的待开放活动，以及现在仍可报名的活动。已报名、满员或截止的活动不会混入。平台暂未给出明确资格时，可到“资格待确认”查看。
- **看清什么时候开抢。** 列表按报名开始时间排列，即将开抢的在前，已开放的随后。卡片单独列出活动举行时间，时间统一按北京时间显示。
- **设定时报名。** 打开活动详情，可立即报名，也可为尚未开放且已知开抢时间的活动设置定时任务。任务由本机后台执行，等待期间需保持电脑开机联网、主程序运行。
- **安心浏览。** 后台发现变化时只提示“有新数据”。点一下才更新列表，并尽量保留当前活动的阅读位置。也可在“我的活动”和“定时报名”中查看自己的报名记录和任务状态。

## 在 Windows 上使用

1. 下载上方的 **Windows 完整包** 并解压。包内已包含运行环境。
2. 双击 `dream-monitor/01-启动原版优化助手.cmd`，在打开的本地看板登录自己的账号。
3. 升级已有版本时，先关闭旧程序，把新版程序文件覆盖到原目录，保留自己的 `data` 文件夹和 `config.yaml`。

## 从哪里看代码

[网页界面](dream-monitor/ui/)包含列表、筛选按钮和更新提醒。[报名资格判断](dream-monitor/board_rules.py)处理个人资格与时间，[本地服务](dream-monitor/gui_server.py)连接浏览器和后台。[定时任务](dream-monitor/autosign.py)、[校时与提交](dream-monitor/precision/)及[离线测试](dream-monitor/tests/)也可直接浏览。

## 来源与致谢

感谢 **LinXiaoAn（林小安）** 开源原项目 `dream-monitor`，也感谢上游贡献者。**奇怪同学**负责本衍生版的需求和维护，项目沿用了 Opus 修复版，后续改版与测试有 ChatGPT 和 Codex 辅助。[完整贡献说明](CREDITS.md) · [MIT 许可证](LICENSE)
