# GitHub 推送网络配置记录

更新时间：2026-05-28

## 当前结论

本机可以通过本地代理访问 GitHub。当前 Windows 用户代理为：

```text
127.0.0.1:10808
```

但 Git 默认没有读取这个用户代理配置：

- `WinHTTP`：Direct access，无代理。
- 环境变量：未配置 `HTTP_PROXY` / `HTTPS_PROXY`。
- Git 配置：未配置 `http.proxy` / `https.proxy`。

因此 GitHub 推送如果直连，可能出现：

```text
Recv failure: Connection was reset
Failed to connect to github.com port 443
```

## 推荐推送方式

当前本机 GitHub 凭据对应用户是 `Roverlo`。这个用户没有权限直接推送到：

```text
origin  https://github.com/GuDong2003/xianyu-auto-reply-fix.git
```

直接推 `origin` 会变成权限错误：

```text
remote: Permission to GuDong2003/xianyu-auto-reply-fix.git denied to Roverlo.
```

应该推送到已有 fork remote：

```text
fork  https://github.com/Roverlo/xianyu-auto-reply-fix.git
```

一次性代理推送命令：

```powershell
git -c http.proxy=http://127.0.0.1:10808 -c https.proxy=http://127.0.0.1:10808 push fork main
```

## 可选：写入仓库本地 Git 配置

如果后续一直使用这个代理，可以只对当前仓库设置 Git 代理：

```powershell
git config --local http.proxy http://127.0.0.1:10808
git config --local https.proxy http://127.0.0.1:10808
```

之后推送 fork 可直接执行：

```powershell
git push fork main
```

取消本仓库代理配置：

```powershell
git config --local --unset http.proxy
git config --local --unset https.proxy
```

## 排查命令

查看远端：

```powershell
git remote -v
```

查看 Git 代理配置：

```powershell
git config --show-origin --get-regexp "^(http|https)\..*proxy"
```

查看 Windows 用户代理：

```powershell
Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" |
  Select-Object ProxyEnable,ProxyServer,AutoConfigURL
```

测试代理访问 GitHub：

```powershell
curl.exe -I --connect-timeout 10 -x http://127.0.0.1:10808 https://github.com
git -c http.proxy=http://127.0.0.1:10808 -c https.proxy=http://127.0.0.1:10808 ls-remote fork HEAD
```

## 本次验证结果

已使用代理成功推送：

```text
fork/main 已更新到当前本地 main HEAD
```

`origin/main` 仍无法由当前凭据直接推送，需要原仓库 `GuDong2003/xianyu-auto-reply-fix` 给 `Roverlo` 写权限，或者继续走 fork 后再发起 Pull Request。
