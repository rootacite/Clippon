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

### 两个 cage 会同时打开同一组虚拟设备

udev 规则只能按设备名（`libvirtualhid*`）打 `ID_SEAT`，区分不出设备属于哪个实例。所以两个实例都在跑时，两边的 cage 会各自打开全部设备。同一时刻只有一路在推流，实际影响是另一侧的 cage 白拿一份事件。要彻底分开得让每个实例有独立的 seat 名，那需要让 udev 知道设备归属，目前做不到。

### 光标渲染是正常的

之前误判过一次。cage 用 `wlr_scene_output_commit` 渲染，headless 输出上画得出光标（用 `grim` 截图确认过）。所以"看不到指针"不是渲染问题。

### 用 `grim` 加写事件做端到端自检

在一个实例的 Wayland socket 上截图，往它的虚拟鼠标设备写事件，再截一张对比：

```bash
WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 grim /tmp/before.png
# 用 evdev 打开 /dev/input/eventN 写 ABS_X/ABS_Y 或 REL_X/REL_Y
WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 grim /tmp/after.png
```

画面有变化就说明 `uinput → libinput → cage → 应用` 是通的。`weston-eventdemo` 适合当被测应用，它的方块会跟着指针事件变色。

## 配对

### 真实 Moonlight 的整条配对还要再走一遍

网关侧的配对状态机已经接上（`getservercert`、`clientchallenge`、`serverchallengeresp`、`clientpairingsecret`、`pairchallenge`），PIN 从 `clipponctl pin NNNN` 进来，配对成功的客户端证书会写进 `state/gateway/clients.json` 并在重启后读回。

没做的：配对一次真实客户端，确认重启网关后不用重新配对。

### 网关没法校验 Moonlight 的客户端证书

`47984` 现在用 `ssl.CERT_NONE`，任何能连到这台机器的客户端都能拿到应用列表并启动实例。

试过的路都不通：Moonlight 的客户端证书是自签名的，`ssl.CERT_OPTIONAL` 会在握手阶段以 `unknown_ca` 失败，而 CPython 的 `ssl` 模块（3.10 / 3.12 / 3.14 都确认过）没有 `set_verify` 回调，`VERIFY_X509_PARTIAL_CHAIN` 和把已配对证书 `load_verify_locations(cadata=...)` 也都不接受自签名身份。所以配对状态只能来自网关自己的记录（`clients.json`），握手层管不了。

要真正恢复校验，得换掉 `ssl` 这一层：自己用 OpenSSL 的 `SSL_CTX_set_verify` 接一个永真回调再在应用层比对指纹，或者把 47984 交给一个能这么做的独立进程。

### 已配对的客户端证书需要重配一次

`state/gateway/clients.json` 里现在是从宿主机旧 Sunshine（`~/.config/sunshine/sunshine_state.json`）导入的 9 张证书。如果 Moonlight 那边不是其中任何一张，就要重新配一次——网关会自己收下 PIN，不需要后端网页。

## 网络

### 从这台机器自己访问局域网地址不走 DNAT

内核不会把本地产生的流量送进 `prerouting`，所以在本机连 `192.168.4.4:48010` 或三路 UDP 不会命中 `ip clippon`。从别的机器访问是正常的。如果以后想在同一台机器上跑 Moonlight 客户端，需要补 OUTPUT 链的规则并处理回流。

### 没有实测过 Docker 在跑时的转发

`clippon-net` 会在 `DOCKER-USER` 存在时插入放行 `10.200.0.0/24` 的规则。Docker 现在是 `inactive`，这段没跑过。

### SIGKILL 之后会留下网络命名空间

销毁 netns 的只有 `clippond`。它被 `SIGKILL` 时，实例进程继续活着，netns 没人回收。重新拉起 `clippond` 之后 `_reap` 会清掉已经退出的实例；还在运行的会保留，直到手工 `clipponctl stop`。

### 实例进程留在 `clippon-net` 的 cgroup 里

`clippon-net.service` 用 `KillMode=process`，重启服务不会杀掉实例。代价是 systemd 记为 `Found left-over process ... in control group`，这些进程的资源限制和日志归属挂在 helper 的 unit 上。要更干净的话得给它们一个独立的 scope。

### 重装服务会弹 KDE 钱包解锁框

`systemctl --user restart clippond` 会让 KDE 那边的组件去请求会话 secret service，`ksecretd` 因为没有父窗口，直接把 `kdewallet` 的解锁对话框丢到桌面上。实测：只重启服务会弹，单纯用 `clipponctl` 启停实例不会弹。

`install-daemon.py` 现在只在 unit 文本真的变了才重启，`enable --now` 覆盖首次安装和启动，所以重复安装不再触发。想彻底不弹，只能把 `kdewallet` 设成空密码。

## 音频

### 每个实例的 pipewire-pulse 在真实会话总线上抢注 D-Bus 名字

启动第二个实例时会看到：

```text
mod.protocol-pulse | D-Bus name org.pulseaudio.Server already taken.
```

`pipewire` 和 `pipewire-pulse` 继承了 host 的会话总线，只有第一个实例能注册成功；`wireplumber` 已经单独套了一层 `dbus-run-session`，这两个没有。目前 socket 仍然可用（`pactl info` 能连上，Sunshine 也正常用它），所以没动。要干净的话，把这两个也套上 `dbus-run-session`，和 `wireplumber` 保持一致。

## 工具

### 直接跑 `clippon --instance` 需要 clippond 先写过文件

`--instance NAME` 只负责挑配置和日志目录；`sunshine.conf`、`apps.json`、`sunshine_state.json` 都是 `clippond` 启动前写好的。手工拿这个参数调试时，要先让守护进程写一遍，或者自己准备这三个文件。网络命名空间只能由 `clippon-net` 创建（需要 root），所以手工路径进不了隔离。

### `cryptography` 靠 pacman 装

`clippon/pair.py` 和配对测试都用 `cryptography`。`pyproject.toml` 里已经声明，`install-daemon.py` 会通过 pacman 装 `python-cryptography`。缺了它 `clippond` 起得来，但 `open_front` 会失败，只能记一条日志，配对照样不可用。
