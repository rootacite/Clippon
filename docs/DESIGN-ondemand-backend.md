# 备选路线：后端按需拉起（保留 netns）

结论：这条路线可行，但收益与代价不匹配，暂不实施。记录在此备用。

思路是让**实例定义**常驻，而**实例进程**只在会话期存在：clippon（cage + 应用 + 私有音频）、netns 里的 sunshine、流端口 DNAT 都在 `/launch` 时拉起，会话结束时撤掉。网络命名空间照旧保留，网关保持常驻在线。

## 动机

一个 sunshine 进程在启动时就建好自己那套虚拟键鼠（`input::init()`，`src/main.cpp:459`），而 udev 规则只能按设备名 `libvirtualhid*` 打 `ID_SEAT`，区分不出设备属于哪个实例，所以所有存活的 cage 会一起抓同一组设备。多个后端长期共存同时也意味着多套私有音频实例和多份端口占用。把后端降成"同一时刻最多一个"，这些并发问题就一起消失。

## 已核实的源码事实

Sunshine 源码取 `~/Downloads/Sunshine-2026.1008.155609`，Moonlight 取 `~/Downloads/moonlight-qt-6.2.0`。

| 事实 | 证据 |
| --- | --- |
| 一个 Sunshine 同时只跑一个 App，`/launch` 会拒绝第二个 | `src/nvhttp.cpp:1376-1380` "An app is already running on this host" |
| 多个 RTSP 会话是支持的（多个客户端可同看一个 App） | `src/rtsp.cpp` 的 `_session_slots` 集合、"New streaming session started [active sessions: N]" |
| 我们的 App 条目没有 `cmd`，Sunshine 走 placebo 分支，会话照常、`running()` 返回 appid | `src/process.cpp:284-286` "Executing [Desktop]"、`placebo = true` |
| `/launch` 把会话密码学状态建在进程内（`rikey`→`gcm_key`，`rikeyid`→`iv`），随后等 RTSP 客户端接入 | `src/nvhttp.cpp:476-533`、`1444` |
| 客户端从 `/launch` 到接入 RTSP 的窗口是 10 秒 | `src/config.cpp:816`（`ping_timeout` 默认 10s）、`src/rtsp.cpp:605` |
| Sunshine 从 exec 到对外服务约 145 ms | alpha 实例日志 20:03:18.860 → 20:03:19.004 `Configuration UI available at [https://localhost:47990]` |
| base 端口可配，其余端口都是它的固定偏移（−5/0/+1/+9/+10/+11/+21） | `src/config.cpp:1868-1870`、`src/network.cpp:245` |
| `/serverinfo` 同时挂在明文 HTTP 上，暴露 `currentgame`、`state`（BUSY/FREE） | `src/nvhttp.cpp:1719`、`1271-1273` |
| 流通道不是 TLS：RTSP 用 rikey 派生的 AES-GCM（`rtsp.cpp` 里没有 ssl context） | `src/rtsp.cpp` 全文 |
| 只有 HTTPS 控制通道校验客户端证书 | `src/nvhttp.cpp:95`（`verify_fail_if_no_peer_cert`）、`356`、`1662` |
| 后端已在用网关证书，交接不会破坏客户端 pin | 实例日志 `config: 'cert' = .../gateway/server.pem` |
| 实例 state 只信 `clippon-gateway` 一张证书，真实客户端证书在网关 `clients.json` | `instances/alpha/sunshine_state.json`、`gateway/clients.json`（10 张） |
| 客户端等 `/launch` 最长 120 秒 | `app/backend/nvhttp.cpp:17` `LAUNCH_TIMEOUT_MS 120000`；另有 `RESUME 30000`、`QUIT 30000`、`REQUEST 5000` |
| 客户端端口默认固定，但带端口添加主机时会整体偏移 | `app/backend/nvaddress.h:5-6`；`app/backend/nvcomputer.cpp:341` `port = (port - 47989) + basePort` |
| Moonlight 优雅退出会发 `/cancel`（条件 `quitAppAfter`），硬断线不发，应用继续跑 | `app/streaming/session.cpp:1300`；`src/stream.cpp:1377` 的循环等应用自己退出 |

## 设计（若实施）

常驻与会话期的分工：

| 对象 | 生命周期 |
| --- | --- |
| 每实例一个 netns | 常驻 |
| 网关（47984/47989/47990）、clippond | 常驻 |
| 实例的 clippon（cage + 应用 + 私有音频） | 会话期 |
| netns 里的 sunshine | 会话期 |
| 流端口 DNAT（TCP 48010、UDP 47998/47999/48000 → `10.200.0.N`） | 会话期 |

触发链路：网关在 47984 收到 `/launch` → 拉起该实例栈 → 探 `10.200.0.N:47989/serverinfo` 等就绪（明文 HTTP，不需要客户端证书）→ 装 DNAT → 转发 `/launch` 并改写 `sessionUrl0`。

结束判定：`/cancel`，或上面对 `currentgame` 的轮询归零，或后端进程退出。

驻留策略：同一时刻只驻留一个实例，客户端请求另一个 App 时先驱逐驻留者。会话结束时应用怎么办还需要一个决定，两种都可行：

- 驻留宽限期：sunshine 随会话起停，cage + 应用保留一段时间（60–120 秒）以便 `/resume` 快速重连。空闲期虚拟输入设备不存在，应用收不到输入；下次会话 sunshine 重建设备，cage 通过热插拔接管（这条已实测）。
- 立即销毁：整栈随会话起停，断开即杀应用，状态靠应用自身存档。

## 为什么暂不做

顺带消失的：

- 两个 cage 抢同一组虚拟设备——前提是同一时刻只驻留一个实例，所以按需要盖到 cage 这一层，不只是 sunshine 进程。
- 每个实例的 pipewire-pulse 抢注 `org.pulseaudio.Server`——同一前提。

不消失的：

- SIGKILL 后 netns 残留：窗口变窄，仍然存在，需要 helper 启动时对账（记录里已退出的实例 → 删对应 netns）。
- Docker `DOCKER-USER`：属于 DNAT/FORWARD 链路，保留 netns 就保留它，变化只是规则只在会话期存在。
- 从本机访问局域网地址绕过 DNAT：与实例生命周期无关。

代价：

- 新增一条会话监督器子系统：就绪探测、结束判定、驻留与驱逐策略，以及 `clipponctl` 语义改写。
- 放弃两个不同 App 同时串流的能力（这正是 netns 方案独有的能力）。
- 切换 App 会打断正在跑的会话。
- root helper 与 nft 表照旧保留，这一轮换到的是生命周期上的收益。

净效果：收益集中在一个少见的场景（两个实例同时跑），代价是一条新子系统。所以先记在这里。

## 若以后要做

改动集中在四处：

- `clippon/daemon.py`：增加会话监督器（拉起/等待就绪/结束回收）。
- `clippon/net_helper.py`：把 `create` 与 `spawn` 拆成 create（常驻）和 activate/deactivate（会话级），并在 helper 启动时对账 netns。
- `clippon/front.py`：`/launch` 改走监督器，DNAT 的安装时机跟着会话走。
- `clipponctl`：`start`/`stop` 语义改成"定义"/"驻留"。

可以直接复用的两个数字：客户端 `/launch` 预算是 120 秒（按需启动的 1–2 秒占得很少），以及客户端端口可以整体偏移（`nvcomputer.cpp:341`），需要错开端口时用得上。
