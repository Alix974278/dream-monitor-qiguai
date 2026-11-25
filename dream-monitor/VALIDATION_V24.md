# v2.4 验证记录

2026-11-11，Windows，随包 Python 3.14.10。验证对象为新筛选版，不连接真实平台。

- `check_local.py`：145 项通过（含原有 118 项、26 项新增资格/缓存/时间/列表数据链路检查，以及 1 项 v2.3 关键文件整文件保护检查）。
- `node tests/test_board_ui.cjs`：14 项断言通过。Node 仅供开发验证，用户运行无需安装。
- `node --check ui/app.js` 与 `node --check ui/board.js`：语法检查通过。
- Edge headless，真实 `gui_server.Handler`/`Hub.state`/`/api/activity` 路由，外部客户端由模拟数据替代。1440×1000、390×844，浏览器时区设为纽约以检查界面的北京时间格式。
- 核对默认资格筛选包含未来符合条件活动；开抢时间优先排序；已满/已报/无资格/截止排除；资格未知单列；计数一致；筛选保存；详情按钮；名额与资格更新；缺开抢时间不允许设置定时；移动端无横向溢出且列表可滚动；关闭抽屉隐藏；临近既有定时任务不自动刷新全列表。
- 浏览器没有 JavaScript 异常；没有提交报名、取消报名、登录或定时任务请求。

## 原包自检的兼容处理

原 ZIP 内的 AST 哈希与该 ZIP 自带 Python 3.14.10 的 AST 输出不匹配，在未修改 `dmclient.py` 上已复现。保留原始 `FUNCTION_SOURCE_BASELINE.json`，使用精确源码 SHA-256 继续保护原认证、表单、手动报名和用户方法；增加 `V23_PROTECTED_BASELINE.json` 校验从用户上传 ZIP 提取的用户管理、手动报名 API、定时和校时核心整文件哈希。

`monitor.py` 为资格数据接入而修改，不再声称整个文件未改；原哈希及授权改动原因记在 `UPSTREAM_BASELINE.json`。其他仍受保护文件/方法均通过校验。

## 边界

未用真实账号登录，未实测每类活动返回的 `ableJoinFlag` / `unableJoinReason` 组合及学校限制，未验证真实报名成功率。没有明确个人资格证据的活动始终留在“资格待确认”，不猜测其资格；未知字段取值不会被当作 true。

附 `V24_BROWSER_VALIDATION.json`、`V24_OFFLINE_TESTS.txt` 和 `V24_CHANGES.json`。
