# 工作记录

Clippon：在一台机器上并行运行多个 Sunshine 实例，让 Moonlight 把它们看成同一台主机上的多个 Application。

原始脚本 `~/.local/bin/start-sunshine.sh` 保持未改动。本仓库是它的工程化重写与扩展。

## 已完成

### 一、把 start-sunshine.sh 工程化

`python -m clippon` / `bin/clippon` 复刻原脚本的行为，日志分组件着色：

- `clippon/config.py` 解析 `--res`、`--verbose`、`--instance` 和 `STREAM_*` 环境变量，保留了 shell 里 `${VAR-default}` 与 `${VAR:-default}` 的区别。
- `clippon/cage.py` 起 headless cage，`LD_PRELOAD` 座位名垫片，只读 `ID_SEAT=seat-cage` 的设备；客户端用 `env -u LD_PRELOAD` 起，避免垫片漏进被串流的程序。
- `clippon/audio.py` 为每个实例起一套私有 PipeWire/pipewire-pulse/WirePlumber（`dbus-run-session`），互不干扰 host 音频。
- `clippon/session.py` 负责启停顺序：先发信号给 cage，等 Sunshine 完全退出后再停音频（Sunshine 会在退出时通过那个 Pulse socket 恢复默认 sink，socket 先没会挂住）。
- `install.py`（Arch）安装依赖并编译座位名垫片。

### 二、多实例网络隔离

每个实例一个网络命名空间，宿主机上一个 root helper 管理：

- `clippon/net_helper.py`（服务 `clippon-net`）建 netns、veth、桥 `cbr0`（10.200.0.1/24，实例从 .2 起），并通过 nftables 表 `ip clippon` 把流端口 DNAT 到选中的实例。
- `clippon/daemon.py`（用户服务 `clippond`，socket `$XDG_RUNTIME_DIR/clippon/daemon.sock`）管理实例生命周期。
- `clipponctl.py`：`list`、`start`、`stop`、`expose`、`hide`、`pin`。
- `install-daemon.py` 安装 helper、两个 systemd 单元、执行位修复。

### 三、网关把多个实例伪装成一台主机

`clippon/front.py` 在宿主机网络命名空间里接管 GameStream 控制面：

- TCP `47989`（明文 serverinfo）和 `47984`（HTTPS）由网关自己回答；`47990` 原样转到当前暴露实例的 Web UI。
- `serverinfo` 的 `LocalIP` 用网关 `accept` 到的地址，`uniqueid` 是网关自己的稳定值。
- `applist` 合并所有存活后端的应用列表，标题用实例名，外部 ID 由 `instance + 内部 id` 的 CRC32 生成（`clippon/gateway.py`）。
- `launch` / `resume` / `cancel` 按 Application 路由到对应后端，并把响应的 `sessionUrl0` 主机改写成客户端拨号地址，同时切换流端口的 DNAT。
- 流端口（TCP `48010`、UDP `47998`/`47999`/`48000`）继续走内核 DNAT，不进用户态代理。
- 配对由网关自己实现（`clippon/pair.py`，五个阶段），PIN 用 `clipponctl pin NNNN` 提交，已配对的客户端证书记进 `state/gateway/clients.json`。

### 四、输入设备修复

两条根因，都在 `docs/OPEN-ISSUES.md` 里有完整记录：

- **cage 不能待在 netns 里。** udev 的设备热插拔事件走 `NETLINK_KOBJECT_UEVENT`，这个组播组只存在于 `init_net`，netns 里的合成器启动时能枚举到已有设备，但之后新建的设备永远收不到。而 Sunshine 恰恰在 cage 起来之后才建虚拟输入设备。现在 clippon、cage、私有 PipeWire 都在 host，只有 Sunshine 进 netns；`/dev/input` 和 uinput 是内核全局对象，所以 netns 里建的设备 host 上的 cage 收得到。
- **虚拟鼠标没有输出映射。** cage 只在 `wlr_pointer.output_name` 有值时才映射输入设备，而 wlroots 0.20.2 不从 udev 的 `WL_OUTPUT` 属性填这个字段。`assets/wlr_cursor_map.c` 用 `LD_PRELOAD` 拦截 `wlr_cursor_attach_input_device`，把设备映射到输出布局的中心输出。

## 当前状态

- `clippon-net` 与 `clippond` 都在运行。
- `alpha`（10.200.0.2）与 `beta`（10.200.0.3）各自在自己的 netns 里，端口互不冲突。
- 网关在另一台机器的 Moonlight 上表现为一台主机，应用列表里是 `alpha` 和 `beta`，两个都能打开，指针和键盘正常。
- 单元测试 35 项通过。

## 目录

```text
assets/     C 垫片源码与 udev 规则
bin/        clippon、clippond、clippon-net、clipponctl 启动器
clippon/    包本体
docs/       文档
tests/      单元测试
install.py           依赖安装（Arch）
install-daemon.py    安装 helper 与两个 systemd 单元
clipponctl.py        仓库根目录的 ctl 入口
```

## 相关文档

- `docs/OPEN-ISSUES.md`：当前未解决的问题。
- 参考源码：`/home/acite/Downloads/Sunshine-2026.1008.155609` 与 `/home/acite/Downloads/moonlight-qt-6.2.0`。
