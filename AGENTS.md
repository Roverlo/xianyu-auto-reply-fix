# Codex 工作区规则

- 本地执行命令的终端是 PowerShell。
- 代码编译或测试过程中如果缺失工具，先询问用户是否需要下载安装；用户同意后再代为下载安装到本机。
- 每次代码更新后，更新 Git 并推送到用户的 GitHub fork；如果当前仓库还没有 fork，先为用户创建 fork。
- 本机可以通过本地代理访问 GitHub：`127.0.0.1:10808`。
- Git 默认没有读取当前 Windows 用户代理配置：WinHTTP 为 Direct access，未配置 `HTTP_PROXY` / `HTTPS_PROXY`，也未配置 `http.proxy` / `https.proxy`。
- GitHub 推送如果直连，可能出现 `Recv failure: Connection was reset` 或 `Failed to connect to github.com port 443`；需要时使用 `git -c http.proxy=http://127.0.0.1:10808 -c https.proxy=http://127.0.0.1:10808 ...`。
- 排查账号运行状态时，不要只看 `/health` 或容器状态；必须继续核对账号 runtime/status、`realtime.log`、`system_settings` 中的登录退避和风控状态。

## 自动发货与 RPA 兜底规则

- 官方 WebSocket/token 链路和 RPA 兜底链路要分开判断：`can_auto_deliver=false` 只说明官方消息流不可用，不等于已登录浏览器兜底也不可用。
- RPA 兜底入口在 `utils/rpa_delivery_worker.py`，由 `XianyuAutoAsync.py` 启动，配置在 `global_config.yml` / `config.py` 的 `RPA_DELIVERY`。
- 当前 RPA 画像目录是 `/app/browser_data/rpa_chrome`，和账号滑块恢复用的 `browser_data/user_<账号ID>` 不是同一个目录；不要随手删除 `browser_data/` 或 RPA profile。
- `RPA_DELIVERY.open_browser_on_start=true` 时，容器启动后会在 VNC/Xvfb 里预热闲鱼 IM 页面；无人值守依赖用户先在这个 VNC 浏览器里完成登录、滑块或其他人工验证。
- `RPA_DELIVERY.only_when_ws_unready=true` 是默认安全边界：官方 WebSocket 已经可发货时，RPA 不抢单。
- RPA 发货必须先确认浏览器、登录态、会话、输入框都可用，再调用 `_auto_delivery` 预占或消耗库存；不要把库存预占提前到页面确认之前。
- 当前 RPA 首版只处理单数量、文本发货步骤；图片、多数量、复杂步骤要跳过，除非同步补实现和测试。
- 如果页面发送结果无法确认，必须记录 `send_uncertain` / `delivery_finalization_states` 并阻止自动重复发送，不要为了“成功率”盲目重发。
- 修改 RPA 或发货收尾逻辑后，至少运行 `python -m unittest tests.test_rpa_delivery_worker tests.test_risk_control_logic`。
