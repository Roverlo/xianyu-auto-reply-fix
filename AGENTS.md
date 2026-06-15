# Codex 工作区规则

- 本地执行命令的终端是 PowerShell。
- 代码编译或测试过程中如果缺失工具，先询问用户是否需要下载安装；用户同意后再代为下载安装到本机。
- 每次代码更新后，更新 Git 并推送到用户的 GitHub fork；如果当前仓库还没有 fork，先为用户创建 fork。
- 本机可以通过本地代理访问 GitHub：`127.0.0.1:10808`。
- Git 默认没有读取当前 Windows 用户代理配置：WinHTTP 为 Direct access，未配置 `HTTP_PROXY` / `HTTPS_PROXY`，也未配置 `http.proxy` / `https.proxy`。
- GitHub 推送如果直连，可能出现 `Recv failure: Connection was reset` 或 `Failed to connect to github.com port 443`；需要时使用 `git -c http.proxy=http://127.0.0.1:10808 -c https.proxy=http://127.0.0.1:10808 ...`。
- 排查账号运行状态时，不要只看 `/health` 或容器状态；必须继续核对账号 runtime/status、`realtime.log`、`system_settings` 中的登录退避和风控状态。
