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

### 五、真实客户端配对走通

另一台设备上的 Moonlight 删除主机后重新配对，网关日志里五个阶段都在 20:35:59–20:36:15 完成（`getservercert`、`clientchallenge`、`serverchallengeresp`、`clientpairingsecret`、`pairchallenge`），最后一行 `pairing result paired=1`。

`clients.json` 的内容没有变化（mtime 19:35:35、sha256 `f6aa8440…`），说明客户端出示的证书原本就在已知列表里：`on_paired` 在结果日志之前调用（`clippon/pair.py:156`），`add()` 只在 PEM 是新的才落盘（`clippon/clients.py:33`），所以从旧 Sunshine 导入的那 9 张里就有这一张。列表里第 10 张是 `CN=testsim`，来自配对单元测试的伪造客户端。

重启 `clippond` 之后，客户端仍然显示已配对、不再要求 PIN，`alpha` 与 `beta` 都在列表里且能正常进入，指针正常。

### 六、每个实例独立的 D-Bus 会话总线

`pipewire` 与 `pipewire-pulse` 之前继承宿主的会话总线，而宿主桌面的 pipewire-pulse（PID 18009）本身就占着 `org.pulseaudio.Server`，于是每个实例的 pipewire-pulse 注册都失败，日志里留下 `D-Bus name org.pulseaudio.Server already taken.`。原先记成"第一个实例能注册成功、第二个才冲突"，实测第一个实例也一样，名字是宿主占的。

`clippon/audio.py` 新增 `_own_session()`，三个守护进程都套进 `dbus-run-session`（`wireplumber` 原先就是这么起的；缺 `dbus-run-session` 时退回原样启动）。

重启 `beta` 后的验证：

- 实例音频日志里 `already taken` 出现 0 次。
- `busctl --address=<beta 私有总线> call … GetNameOwner org.pulseaudio.Server` 返回 `:1.0`，即 beta 自己的 pipewire-pulse（pid 611529）。
- `pactl` 连 beta 的 socket 能看到 `cage-sink` 与 `cage-sink.monitor`（Sunshine 抓取用的就是 monitor），播放测试时 sink 转为活动状态。
- 宿主音频未受影响：pulse PID 仍是 18009，默认 sink 仍是那块声卡。

`alpha` 没有重启，进程仍走旧路径，下次重启时生效。单元测试 35 项通过。

### 七、每个实例独立的 libinput seat

之前每个 cage 都会打开**全部**实例的虚拟键鼠：udev 把 `libvirtualhid*` 设备统统打上 `ID_SEAT=seat-cage`，而每个 cage 的座位名也是 `seat-cage`。现象是在 alpha 里按 `←`，beta 里正在播放的视频也倒放；`/proc/<pid>/fd` 实测两个 cage 各自持有 event25–event30 六个设备。设备本身没有任何可区分实例的信息：两套设备名完全相同，`phys`、`uniq` 都是空的，内核也不暴露 uinput 设备的创建者。

现在按实例分 seat，链路是：

- `assets/vhid_name.c`：`LD_PRELOAD` 进 Sunshine，拦截 `libevdev_set_name`，在设备名后追加 `@<实例名>`（设备名变成 `libvirtualhid Keyboard@alpha`）。Sunshine 动态链接 `libevdev.so.2` 并引用 `libevdev_set_name@LIBEVDEV_1`，符号可被拦截。
- `assets/clippon-seat-tag`：udev 的 `IMPORT{program}` 助手，从 `%k` 读 `/sys/class/input/<event>/device/name`，把后缀变成 `ID_SEAT=seat-cage-<实例名>`；没有后缀的设备仍落到共享的 `seat-cage`。
- `assets/seat_shim.c`：座位名改从 `CLIPPON_SEAT` 读取，回退到 `seat-cage`。
- `clippon/net_helper.py`：spawn Sunshine 时换用一个**去掉 file capabilities 的副本**（`/usr/local/lib/clippon/sunshine`）。打包的 `/usr/bin/sunshine` 带 `cap_sys_admin=p`，内核因此给进程打上 secure 标记，加载器会直接忽略 `LD_PRELOAD` —— 加 ambient caps 也没用（执行带 file caps 的二进制时 ambient 会被清掉）。headless cage 会话里采集走 wlr-screencopy，用不到这个 capability。副本按「大小+时间戳」与源比对，有变化才用临时文件加 `os.replace` 换上去，避免正在运行的实例被 `ETXTBSY` 挡住。
- `install-daemon.py` 现在也安装座位垫片。它此前只装地址/光标/名字垫片和 udev 规则，座位垫片依赖 `install.py`，于是改动后的垫片没有落盘，cage 仍报 `seat-cage`，改完标记后所有设备反而谁都看不见——排查时最费时间的一环。

验证（2026-10-10 21:34）：

```text
event23  libvirtualhid Keyboard@alpha          ID_SEAT=seat-cage-alpha
event26  libvirtualhid Keyboard@beta           ID_SEAT=seat-cage-beta
cage 627443（alpha）持有: event23, event24, event25
cage 627654（beta） 持有: event26, event27, event28
```

功能验证（往 alpha 的设备注入事件，读两边 mpv 自己的状态）：

- 键盘：`KEY_SPACE` 让 alpha 的 `pause` 由 `False` 变 `True`，beta 保持 `False`；再按一次恢复。
- 鼠标：相对位移让 alpha 的 `mouse-pos` 从 `(640,360)` 变 `(834,501)`，beta 停在 `(640,360)`。

兜底：名字垫片不在时 `instance_seat()` 返回空串，cage 留在共享 seat，退化成"串扰但可用"，而不是"收不到任何输入"。

顺带发现：地址垫片 `libsunshine_addr.so` 一直没生效（同样因为 file caps 让 `LD_PRELOAD` 被忽略），现在也不再预加载进 Sunshine —— `LocalIP` 与 `sessionUrl0` 由网关改写。

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
