# C3WEB 远程查看

C3WEB 是 sunnypilot 的远程查看功能模块，通过浏览器实时查看 C3 设备的摄像头画面、浏览历史行车记录并下载视频文件。

## 功能

- **实时画面**：同时观看前摄、驾驶员、广角三路摄像头画面，支持流畅/高清/超清三档画质
- **历史录像**：浏览所有行车路线，在线预览录像片段，下载原始视频文件
- **远程访问**：通过 FRP 内网穿透，在任何有网络的设备上访问 C3 设备
- **PIN 码保护**：4 位数字 PIN 码认证，24 小时会话保持

## 架构

```
浏览器 ──HTTPS──▶ FRP 服务器 ──WSS隧道──▶ C3 设备 (c3_webd :8082)
```

- `c3_webd.py`：核心 Web 服务，提供 REST API 和 WebSocket 实时流
- `frpc_launcher.py` + frpc：FRP 客户端，建立从公网到设备的隧道
- `static/`：前端单页应用（纯 HTML/CSS/JS，无外部依赖）

## 使用前提

**⚠️ 本仓库不包含 FRP 服务端配置和密钥，无法直接克隆运行。**

使用 C3WEB 远程查看功能需要你自行完成以下准备：

### 1. 搭建 FRP 服务器

你需要一台有公网 IP 的服务器，安装 FRP 服务端（frps）。

FRP 项目地址：https://github.com/fatedier/frp

frps 最小配置参考：

```toml
# frps.toml
bindPort = 7000
auth.token = "your-auth-token"
vhostHTTPPort = 8080
```

### 2. 配置 frpc.toml

修改 `selfdrive/c3_web/frpc.toml`，填入你自己的 FRP 服务器信息：

| 配置项 | 说明 |
|--------|------|
| `serverAddr` | 你的 FRP 服务器 IP 或域名 |
| `serverPort` | FRP 服务器端口（默认 7000） |
| `auth.token` | 与 frps 一致的认证令牌 |
| `customDomains` | 用于访问 C3WEB 的域名 |

### 3. 配置 DNS（可选）

将 `customDomains` 中设置的域名解析到 FRP 服务器的公网 IP。

### 4. 启用 C3WEB

在 sunnypilot 的「开发者设置」中打开 **C3 Web Service** 开关，设备会自动启动 `c3_webd` 和 `frpc` 进程。

## 使用方式

配置完成后，通过浏览器访问你在 `customDomains` 中设置的域名，输入 PIN 码即可使用。

### 默认 PIN 码

默认 PIN 为 `0909`，可在 sunnypilot 设置中修改 `C3WebPin` 参数。

> ⚠️ 建议首次使用后立即修改默认 PIN 码。
