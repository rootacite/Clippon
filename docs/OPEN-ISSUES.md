# 未解决的问题

这份文档只列当前还没有解决的事。

## 输入

### cage 必须在 init_net，否则虚拟输入设备永远进不来

这条是根因，也是之前"指针不动"的真正原因。

udev 的设备热插拔事件由内核往 netlink 的 `NETLINK_KOBJECT_UEVENT` 组播组发送，而这个组播组**只存在于 init_net**。libinput 的 udev 后端就是靠它感知新设备。于是：

- 网络命名空间里的 cage 启动时能通过 sysfs 枚举到已有设备；
- 但在这之后 Sunshine 建的虚拟输入设备，它**永远收不到通知**；
- 而 Sunshine 恰恰是在 cage 起来之后才建设备的。

所以笼统地把整个实例塞进 netns，输入设备必然失效。现在的分工是：

| 进程 | 所在命名空间 | 为什么 |
| --- | --- | --- |
| `clippon`、`cage`、私有 PipeWire | host | cage 要收 udev 热插拔 |
| `sunshine` | 实例的 netns | 端口要独占，这是 netns 的唯一目的 |

uinput 设备和 `/dev/input` 都是内核全局对象，不是 per-netns 的，所以 Sunshine 在 netns 里建出来的设备，host 上的 cage 照常收得到。helper 为此分成了两个操作：`spawn`（host，给 clippon）和 `spawn-netns`（进 netns，给 sunshine）。

### keyd 抓走虚拟键盘，按键进不了应用

`/etc/keyd/default.conf` 的 `[ids]` 写的是 `*`，keyd 会匹配所有设备。它自己的日志把两类设备分得很清楚：

```text
DEVICE: match    1209:0002:dc9f44e3  /etc/keyd/default.conf   (libvirtualhid Keyboard)
DEVICE: ignoring 1209:0003:9faf35e3                           (libvirtualhid Mouse)
DEVICE: ignoring 1209:0003:684dc0b3                           (libvirtualhid Mouse (Absolute))
```

虚拟键盘是 `1209:0002`，keyd 匹配后对它 `EVIOCGRAB`，于是合成器的 libinput 再也收不到按键；keyd 把这些键转发到自己那个虚拟键盘上，而那个设备在 seat0，所以按键落在真实桌面上，应用一个都收不到。虚拟鼠标是 `1209:0003`，keyd 忽略，这就是鼠标一直正常、键盘"漏到桌面"的原因。

证据（2026-10-10）：

- keyd 在跑时，对两个虚拟键盘调 `EVIOCGRAB` 返回 `EBUSY`，对虚拟鼠标返回成功；停掉 keyd 后键盘的 `EVIOCGRAB` 成功。
- 往 `/dev/input/eventN` 写事件只在设备无人抓取时才进入输入核心。同一段代码写虚拟鼠标，另一个 reader 读到了 `REL_X 30`/`REL_Y 20`；写虚拟键盘，reader 什么都读不到。
- 停掉 keyd 后往虚拟键盘注入 `KEY_SPACE`，alpha 的 mpv 通过 `--input-ipc-server` 报告 `pause` 由 `False` 变 `True`。

处理方向是在 `[ids]` 里排除虚拟键盘，让 keyd 继续管真实键盘：

```ini
[ids]
*
-1209:0002
```

现状：keyd 是用户主动卸载的（二进制与 unit 文件都已不在磁盘上，`pacman -Qs keyd` 为空），本机不再需要它，所以这条不是待修项，只作为机制记录。若将来重新安装 keyd，必须同时加上面的 `[ids]` 排除，否则虚拟键盘会再次被它抓走。

副作用备注：keyd 不在时，`rightalt = rightmeta` 这条改键也不再生效。

### 光标渲染是正常的

之前误判过一次。cage 用 `wlr_scene_output_commit` 渲染，headless 输出上画得出光标（用 `grim` 截图确认过）。所以"看不到指针"不是渲染问题。

### 端到端自检：截图加注入事件

在一个实例的 Wayland socket 上截图，注入事件，再截一张对比：

```bash
export XDG_RUNTIME_DIR=/run/user/1000
WAYLAND_DISPLAY=wayland-0 grim /tmp/before.png
# 用 evdev 打开 /dev/input/eventN 写 ABS_X/ABS_Y 或 REL_X/REL_Y
WAYLAND_DISPLAY=wayland-0 grim /tmp/after.png
```

两点注意：

- 写 `/dev/input/eventN` 只在设备无人抓取时有效。虚拟键盘会被 keyd 抓走（见上一条），虚拟鼠标无人抓取，所以鼠标能这样测、键盘不能。
- 应用在动画面时，逐像素差值淹没在画面自身的变化里。更可靠的办法是给应用开一个可查询的状态出口，例如 `mpv --input-ipc-server=/run/user/1000/mpv-alpha.sock`，然后查 `mouse-pos`、`pause`：注入后属性变化就说明事件到了客户端，不依赖像素比较。

`weston-eventdemo` 适合当被测应用，它的方块会跟着指针事件变色。

## 网络

### 没有实测过 Docker 在跑时的转发

`clippon-net` 会在 `DOCKER-USER` 存在时插入放行 `10.200.0.0/24` 的规则。Docker 现在是 `inactive`，这段没跑过。

已记录，暂不处理（2026-10-10 决定）。影响：Docker 会把 `FORWARD` 策略设成 DROP 并接管 `DOCKER-USER`，我们的流端口属于转发流量，放行规则没落对位置时症状是「应用列表正常、点启动也成功，但串流连不上」。另外 Docker 每次重启会重建自己的链，可能冲掉插入的规则，正在串流时会话会当场断掉。不用 Docker 就没有影响。

### 实例进程留在 `clippon-net` 的 cgroup 里

`clippon-net.service` 用 `KillMode=process`，重启服务不会杀掉实例。代价是 systemd 记为 `Found left-over process ... in control group`，这些进程的资源限制和日志归属挂在 helper 的 unit 上。

已记录，暂不处理（2026-10-10 决定）。影响：unit 上没有设任何 `CPUQuota`/`MemoryMax`/`TasksMax`，所以现在没有实际后果；一旦给 unit 加限制，限制会作用在所有实例的总和上。要彻底干净得让每个实例跑在自己的 transient scope 里，helper 的 spawn 与 pid 跟踪都要改成从 systemd 取 `MainPID`。

### 新建实例 veth 时，NetworkManager 会弹 KDE 钱包

对话框原文是 “KDE has requested to open the wallet 'kdewallet'.”。它不是 `clippond` 重启引起的。重启这个用户单元不会创建网卡，钱包调用一次都没有。钱包已经开着时，后面的请求也不再弹窗，所以 21:27 和 21:54 那几次重启桌面上什么都没有。

真正的链条：

1. helper 在宿主机上先 `ip link add vb-<实例> type veth peer name vn-<实例>`，再把 `vn-<实例>` 送进 netns。
2. 若 NetworkManager 在设备被送走之前把它当成要管理的网卡，就会写一条 `Wired connection 1` 并自动激活。今天的弹出和这一行在同一秒：`settings: (vn-beta): created default wired connection 'Wired connection 1'`（19:02:20、19:19:49、19:25:56、19:42:32、19:43:46、19:46:56、20:03:19、21:00:42）。唯一对不齐的是 19:45:41 那次 `ksecretd` 日志：19:45:14 写过同一条连接，当时没有钱包日志，27 秒后才补上，中间没有第二次写连接。被写配置的一直是 `vn-beta`：它比 `vn-alpha` 在宿主机上多停留一会儿，日志里有 `error setting IPv4 forwarding`。22:12 的 `walletprobe` 被立刻送进 netns，NM 只记了 `new Veth device`，没有写连接，也没有钱包调用。
3. `kded6` 里已加载的 `networkmanagement` 模块（`plasma-nm` 6.7.5-1，`/usr/lib/qt6/plugins/kf6/kded/networkmanagement.so`）跟着去开钱包。22:14:43 的总线记录：`kded6`（pid 553067）对 `org.kde.kwalletd6` 调用 `networkWallet`，再 `open("kdewallet", wId=0, "Network Management")`。
4. `kwalletd6` 6.30.0 把这个请求转成对 `ksecretd` 的 `Unlock` 加 `Prompt("")`。窗口 id 是 0，应用名没有传下去，所以 `ksecretd` 打出 `Using kwallet without parent window!`，对话框落到桌面上。新连接没有对应的密码项，解锁之后 `kwalletd6` 再打 `Item not found`。

22:14 这次弹窗是为了把上面第 3 步抓住：`nmcli device set vn-wprobe managed yes` 让 NM 写下同一条 `Wired connection 1`。探测用的网卡和这条连接配置已经删掉。点 Cancel 即可，没有要保存的密码。

## 会话

### 客户端退出后，后端会一直报告 BUSY

Sunshine 的 app 条目没有 `cmd`，走的是 placebo 分支（`src/process.cpp:284-286`），`proc::proc.running()` 一直返回 appid，直到有人显式停掉它。唯一会停它的是 `/cancel`（`src/nvhttp.cpp:1554-1575`），placebo 自己不会结束。

而 Moonlight 只在与服务端"优雅结束"且设置项 `quitAppAfter` 为真时才发 `/cancel`（`app/streaming/session.cpp:1276`），这个设置项默认是 **false**（`app/settings/streamingpreferences.cpp:137`）。于是客户端正常退出后，后端的 placebo 应用继续存在，网关的 `serverinfo` 把它报成 `SUNSHINE_SERVER_BUSY` 加上那个应用的外部 ID。

实测（2026-10-10 20:32，alpha 的会话在 20:04:40 就断了）：

```text
alpha  http://10.200.0.2:47989/serverinfo   currentgame=994642064  state=SUNSHINE_SERVER_BUSY
beta   http://10.200.0.3:47989/serverinfo   currentgame=0          state=SUNSHINE_SERVER_FREE
```

直接后果：Moonlight 的主机项长期显示"正在运行某个应用"，`serverinfo` 的 `currentgame` 也不再归零。真正会出错的情形是 `/launch`：Sunshine 在已有应用运行时拒绝启动，返回 `An app is already running on this host`（`src/nvhttp.cpp:1376-1380`）。客户端按自己缓存的 `currentGameId` 决定发 `/launch` 还是 `/resume`，缓存过期时就会撞上这个拒绝。

两条处理路子：

- 客户端侧，零代码：在 Moonlight 设置里打开"会话结束后退出应用"（`quitAppAfter`），优雅退出时就会发 `/cancel`，网关照原样转发给对应后端。硬断线仍然不会发。
- 网关侧，加固：`/launch` 命中某个后端时，如果该后端 `currentgame > 0`，先向它发一次 `/cancel` 再转发，让重复启动变成幂等。这条不需要新的存活探测。
- 更彻底的：用 nft 计数器之类的内核侧信号判断流是否真的活着，不活跃就自动收掉后端。需要 helper 增加计数器操作。

## 工具

### 直接跑 `clippon --instance` 需要 clippond 先写过文件

`--instance NAME` 只负责挑配置和日志目录；`sunshine.conf`、`apps.json`、`sunshine_state.json` 都是 `clippond` 启动前写好的。手工拿这个参数调试时，要先让守护进程写一遍，或者自己准备这三个文件。网络命名空间只能由 `clippon-net` 创建（需要 root），所以手工路径进不了隔离。

### `cryptography` 靠 pacman 装

`clippon/pair.py` 和配对测试都用 `cryptography`。`pyproject.toml` 里已经声明，`install-daemon.py` 会通过 pacman 装 `python-cryptography`。缺了它 `clippond` 起得来，但 `open_front` 会失败，只能记一条日志，配对照样不可用。
