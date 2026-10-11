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

### 八、实例退出的清理

`clippond` 被 `SIGKILL` 时，实例进程按设计继续跑（它们本来就比守护进程活得久），但没有任何东西回收它们的 netns；更常见的是另一类残留：cage 死了、应用还活着。实测 `~/Downloads` 那批视频之前，alpha 的 `sleep infinity` 应用就在 6 次重启后各留下一个（`ppid=1`，带着旧的 `PIPEWIRE_RUNTIME_DIR`），另外 6 个 `sleep` 是更早的实例留下的。

现在清理集中在 helper 侧，三个入口：

- **helper 启动时对账** `_reclaim()`：记录里 `exited` 的实例直接 `destroy`；磁盘上存在但没有记录的 `clippon-*` netns 删掉；再用 `leftover_instances()` 扫一遍 `/proc`，把"座位或音频目录指向某个没有记录的实例"的进程全部收掉。
- **`destroy` 时清扫** `kill_instance_leftovers()`：任何进程只要 `CLIPPON_SEAT` 等于该实例的座位、或 `PIPEWIRE_RUNTIME_DIR` 以 `/cage-audio-<实例>` 结尾，就整组 `SIGKILL`。这一条同时覆盖正常 `clipponctl stop`（应用可能比 cage 活得久）和守护进程被 `SIGKILL` 之后的情况。
- **实例启动前清扫**：helper 在 spawn 该实例的 clippon 之前先跑一次同样的清扫，保证新的运行不会继承上一次残留的应用。
- 另外 `_signal_and_wait` 的超时分支改成对进程组 `SIGKILL`：clippon 忽略 `SIGTERM` 时才走到这里，此时它仍然握着 cage 与应用的进程组。

座位标记（`CLIPPON_SEAT`）只能覆盖改动之后的进程；更早的残留只有音频目录那个标记，所以两套标记都要匹配 —— 这正是第一版实现漏掉的地方（探测用两个标记、清扫只匹配守护进程名，日志报"removed 6"但一个都没杀掉）。

验证（2026-10-10 21:44）：

```text
kill -9 alpha 的 clippon 之后:  sunshine=2  netns=clippon-alpha, clippon-beta
helper 重启之后:              sunshine=1  netns=clippon-beta
alpha 残留: 座位进程 0 个，音频标记进程 0 个
```

历史残留也在同一次对账里清掉：日志 `removing 6 leftover process(es) of alpha` → `stopped 6 leftover process(es) of alpha`，之后 `beta` 完全未受影响。

### 九、本机客户端也能串流

`nftables` 的 DNAT 只在 `prerouting`，而本机产生的流量走 `output` 钩子，所以在这台机器上直接连 `192.168.4.4` 的流端口不会被转发进实例：应用列表正常、点启动也返回成功，但 RTSP 那一步连不上。

`ports.nft_table()` 现在接收本机的局域网地址，多写一条 `output` 链（`type nat hook output priority dstnat`），规则用 `ip daddr { <本机地址> }` 限定，避免把去往其他主机的 48010 也重定向。地址表在 helper 启动与每次 expose 时刷新。`127.0.0.1` 仍然不行：netns 回不到宿主的 loopback，同机客户端要用局域网地址。

验证：本机连 `192.168.4.4:48010`，netns 里看到入站连接 `10.200.0.2:48010 ← 192.168.4.4:33620`，conntrack 里也能读到被翻译的元组。

### 十、按客户端校验证书

之前 47984 用 `ssl.CERT_NONE`，配对的 `PairStatus` 又是全局的（只要 `clients.json` 里有一条记录，任何客户端都被告知已配对）。后果是：用 Moonlight 添加主机会停在配对（HTTP 的 `serverinfo` 报 `PairStatus=0`，需要 PIN），但用脚本直接打 HTTPS 端点不需要任何凭据，就能拿应用列表、启动、取消，还能把流端口切走。

CPython 的 `ssl` 做不到这件事：自签名客户端证书过不了链校验，`CERT_OPTIONAL` 在握手阶段就以 `unknown_ca` 失败，而这个模块（3.10 / 3.12 / 3.14）没有校验回调。

现在改成用 pyOpenSSL 接 47984：

- `front._tls_context()`：握手时请求客户端证书并一律接受（`set_verify(VERIFY_PEER, 永真回调)`），把判断留给应用层。
- `front.handle()`：每条请求读对端证书指纹（`_TlsConnection.peer_fingerprint`）。`/serverinfo` 与 `/pair` 任何客户端都能用——前者是客户端得知"需要配对"的入口，后者是配对本身；其余路径要求指纹已在 `clients.json` 里，否则 403。`PairStatus` 随之变成按客户端回答：已配对给 1，未配对给 0。
- 细节两处：pyOpenSSL 的 `Connection` 没有 `makefile`，也没有 `getpeername` 之类的快捷方法，所以加了 `_TlsConnection` / `_TlsStream` 两个薄适配层给 `http.server` 用；accepted socket 因为设了超时处于非阻塞模式，`do_handshake()` 会抛 `WantReadError`，所以握手改成 `select` + 10 秒截止的循环。
- 指纹口径：网关用 pyOpenSSL 读客户端证书，`PairedClients` 用 cryptography 存，两边都是对 DER 做 SHA-256；单测把两者钉在一起，从旧 Sunshine 导入的那批 Moonlight 证书不需要重配。

验证（本机探测，网关自己的客户端证书刻意不在 `clients.json` 里，代表局域网里的陌生机器）：

```text
                                   未配对客户端        已配对客户端
HTTPS /serverinfo                 200 PairStatus=0   200 PairStatus=1
HTTPS /applist                    403                200 apps=beta,alpha
HTTPS /pair (getservercert)       允许（等 PIN，不回复）
HTTPS 不带证书 /applist            403
HTTP  /applist                    403
```

`clients.json` 恢复成原来的 10 条之后，未配对客户端再次被拒。47990 后面仍是 Sunshine 自己的 Web UI，有它自己的登录。

### 十一、KDE 钱包弹窗的来源

先前记成“重启 `clippond` 就会弹”。对照全天日志，这个说法不成立：钱包那一行和 `settings: (vn-beta): created default wired connection 'Wired connection 1'` 成对出现，和 `clippond` 的启停对不上。22:08 在钱包已经打开的前提下重启 `clippond`、单独起一套会抢 `org.pulseaudio.Server` 的 pipewire，以及 22:12 起了一个完整的 `walletprobe` 实例，总线上看不到任何发往 `kwalletd6` / `ksecretd` 的调用。

22:14 把一条临时 veth 交给 NetworkManager 管理，复现了同一条连接配置，对话框随之出现。调用者是 `kded6` 的 `networkmanagement` 模块，应用名参数是 `Network Management`，窗口 id 是 0；`kwalletd6` 再用空窗口向 `ksecretd` 要解锁。细节和当天的时间戳在 `docs/OPEN-ISSUES.md`。

### 十二、封面改成实例的实时截图

Moonlight 要封面时请求 `/appasset?appid=<外部 id>&AssetType=2&AssetIdx=0`（`app/backend/nvhttp.cpp:392` 的 `getBoxArt`），读到图之后按固定的 200x267 画（`app/gui/AppView.qml:105-110` 把宽高都写死），所以 16:9 的截图原样返回会被拉成竖条。之前 `/appasset` 直接转发给后端，而我们的应用条目没有图片，Sunshine 就回它的占位图。

现在 `clippon/artwork.py` 接管这个端点：

- 用 appid 反查实例（`Front._route_for`，与 `/launch` 共用一张表），再找这个实例的 cage 显示：`ss -xlp` 里每个 `wayland-N` socket 的持有 pid，读 `/proc/<pid>/environ` 的 `CLIPPON_SEAT` 是否等于 `seat-cage-<实例>`，并从同一份 environ 取 `XDG_RUNTIME_DIR`。按座位标记认领而不是按 socket 名，因为 socket 名每次开机都重新分配，多实例并行时只有座位能区分。
- `grim` 截该显示，Pillow 合成 400x534 卡片（Moonlight 显示尺寸的两倍）：整幅画面等比居中放前面，同一张图放大铺满卡片、高斯模糊后作背景。画面完整且不变形，卡片也不留黑边。
- grim 用 `-t jpeg -q 92`：1440p 一帧 PNG 编码要 1.3s，JPEG 0.3s，而卡片反正要重新采样。
- 截图按实例缓存 20 秒，每个实例同时只跑一次 grim；`AssetType` 不是 2、实例查不到、显示找不到或 grim 失败时返回 None，请求按原路转发给后端，拿到占位图而不是报错。
- `grim` 与 `python-pillow` 加进了 `install.py` 和 `install-daemon.py` 的依赖清单，`pyproject.toml` 也声明了 pillow。

验证（2026-10-10 22:21，用 `clients.json` 里临时登记的网关客户端证书请求，测完已还原成原来的 10 条）：

```text
/applist                       beta 99903777, alpha 462510448
/appasset(appid=462510448)     200 image/png 400x534 153561 B（alpha 的《大清疯人院》画面）
/appasset(appid=99903777)      200 image/png 400x534 178074 B（beta 的《二次元顶级战犯》画面，与 alpha 不同）
未配对客户端                    403
未知 appid                     404 no backend for this request
AssetType=0                    200，仍由后端回答（Sunshine 自己的 600x800 占位图）
首次请求                        0.58s（grim + compose）
20 秒内再请求                   0.007s（缓存）
```

局限：Moonlight 自己会把封面缓存到磁盘（`app/backend/boxartmanager.cpp`，按主机 uuid 与 appId 存 PNG），客户端要清掉那份缓存才会重新请求；截图是请求那一刻的画面，应用停掉之后卡片保持最后一次的结果。

### 十三、`--env` 把变量交给会话

`clippond` 是用户服务，它继承的是登录会话的环境，跟用户当前这个终端无关，所以输入法这类变量得单独指定：`GTK_IM_MODULE=fcitx clipponctl start ...` 只会改 clipponctl 自己的环境。

现在 `clippon` 与 `clipponctl start` 都接受可重复的 `--env KEY=VALUE`（也认 `--env=KEY=VALUE`，值里可以再出现 `=`）：

- `clippon/config.py` 解析成 `Config.env`，`env_pair()` 拒绝没有 `=`、名字为空、名字含空白或含 NUL 的写法。
- 前台路径由 `cage_environment()` 的新参数 `extra` 落进 cage 的环境，`session.py` 把 `cfg.env` 传下去。
- daemon 路径不走命令行（值会被 `ps` 看到），而是 `clipponctl` 把变量放进请求，`clippond` 用 `_child_env()` 并进会话进程的环境，再被 `cage_environment()` 的 `os.environ.copy()` 继承。
- 覆盖顺序两边一致：`--env` 覆盖继承来的同名变量；sink、音频模式与 tray 由请求字段决定，显示、私有音频 socket、`LD_PRELOAD` 与 `CLIPPON_SEAT` 由会话自己决定。daemon 的 `_child_env()` 仍会丢掉传进来的 `LD_PRELOAD`，避免有人往会话里预载东西。

验证（2026-10-10 22:53，daemon 重启后）：

```text
clipponctl start envprobe --env GTK_IM_MODULE=fcitx --env XMODIFIERS=@im=fcitx --env SDL_IM_MODULE=fcitx -- sh -c 'env > /tmp/...'
  应用与 cage 的环境里都出现这三个变量；SDL_IM_MODULE 在 clippond 自己的环境里并不存在，只能来自 --env
  同一实例再带 --env GTK_IM_MODULE=xyz --env STREAM_SINK=hacked：应用看到 GTK_IM_MODULE=xyz，STREAM_SINK 仍是 cage-sink
clippon --env PROBE=frontend -- sh -c 'env > /tmp/...'
  前台路径同样把 PROBE=frontend 交给了 cage 启动的应用
```

第一次探测之所以“看起来生效”，是因为当时 `clippond` 还是 22:22 启动的旧进程，而那些变量本来就在它的环境里；重启服务之后才验出了真正的差异。

### 十四、多个实例同时串流

按 `docs/DESIGN-multi-session.md` 实施。此前每个实例都有自己的 netns，但宿主上只有一组 GameStream 端口，靠"切换"把这一组 DNAT 指向当前那一台，所以同一时刻只能有一路。现在两块都改了：

**每实例一组端口。** Sunshine 的 base port 可配（`src/config.cpp` 的 `int_between_f(..., {1024 + nvhttp::PORT_HTTPS, 65535 - rtsp_stream::RTSP_SETUP_PORT})`，即 `1019..65514`），其余端口都是它的固定偏移（HTTP +0、HTTPS −5、Web UI +1、视频 UDP +9、控制 +10、音频 +11、RTSP +21；`src/stream.h`、`src/rtsp.h`、`src/nvhttp.h`、`src/confighttp.h`）。

- `clippon/ports.py`：新增 `PortGroup`（由 base 推出整组端口）与 `allocate_base_port()`，组间隔 1000（`47989`、`48989`、`49989`…）。
- helper 在 `create()` 里分配 base 并记进实例记录（`/var/lib/clippon/state.json` 的 `port` 字段），`clipponctl start` 拿到之后写进该实例的 `sunshine.conf`（`port = <base>`）与 `meta.json`。旧记录没有 `port` 时，helper 启动对账会给它补一个。
- `nft_table()` 从"一组随时切换"改成"每实例一组静态 DNAT"：每个实例各写 `tcp dport {base+21}` 与 `udp dport {base+9,+10,+11}` → 它自己的 `10.200.0.N`，`output` 链同理。`expose`/`hide` 不再动任何 DNAT，只决定 47990 的 Web UI 代理指向谁。
- 网关访问后端时按该实例的 base 计算（`front.py` 的 `Backend.ports`）。

**网关按客户端回答。** 客户端读到的 `currentgame`/`state` 是网关自己写的，所以可以每个客户端写一份：

- `clippon/sessions.py`：`指纹 -> 实例` 的会话表。`/launch`、`/resume` 转发成功后写入，`/cancel` 成功后删除，回答 `serverinfo` 时对账——后端已经不报这个应用就把记录清掉，客户端异常退出也不会永久占用。
- `/serverinfo`：已配对（HTTPS，带客户端证书）的请求只报它自己那条记录，没有记录就是 `SUNSHINE_SERVER_FREE`。这一步是必须的：`quitApp()` 在 `/cancel` 之后还会再查一次 `serverinfo`（`nvhttp.cpp:249-269`），网关若因别的实例在跑而继续报 BUSY，客户端会以为自己退不掉、抛 599。明文 47989 没有证书，保持原来的全局回答（配对前才走这条路）。
- `/applist`：别的客户端正在串的实例从列表里去掉（`merge_applists(hidden=...)`），id 仍然留在路由表里，所以客户端拿缓存的旧 id 来点也能得到明确回答。
- `/launch`：目标实例被别人占用就回 `503 another client is already streaming this application`，不转发；只有残留应用挡路时先向该后端发一次 `/cancel` 再转发，让重复启动幂等——没有 `cmd` 的 placebo 应用不会自己结束。`clipponctl release NAME` 是手动版：停掉该实例的应用并丢掉所有认领它的会话记录。

顺带核实的客户端源码事实：`getServerInfo()` 在有 pin 过的服务端证书时优先走 HTTPS（`app/backend/nvhttp.cpp:134-190`），`getAppList()` 也只走 HTTPS（`app/backend/nvhttp.cpp:301-306`），所以这两条路径都带着客户端证书，网关能分辨是谁；`currentGameId` 决定客户端发 `/resume` 还是 `/launch`（`app/streaming/session.cpp:1614`），一个 Moonlight 进程只有一个会话（`app/streaming/session.cpp:66-67`）。

验证（2026-10-11 07:52–08:00，本机两个 moonlight-qt 客户端各自在一个实例的 cage 里，`XDG_CONFIG_HOME` 分开，用真实配对流程拿到两张不同的客户端证书）：

```text
clipponctl list            alpha 10.200.0.2 base 47989 / beta 10.200.0.3 base 48989
netns 内监听               alpha: 47984 47989 47990 48010   beta: 48984 48989 48990 49010
nft                       tcp dport 48010 -> .2, 49010 -> .3; udp 47998/99/48000 -> .2, 48998/99/49000 -> .3
客户端 A 串 alpha、B 串 beta  两边同时 CLIENT CONNECTED，两个客户端都解出画面并收到音频包
                            conntrack: 客户端->192.168.4.4:48010 译到 .2；->192.168.4.4:49010 译到 .3
A 的 serverinfo            currentgame=462510448 state=SUNSHINE_SERVER_BUSY
B 的 serverinfo            currentgame=99903777  state=SUNSHINE_SERVER_BUSY
B 的 applist               只有 beta（alpha 被 A 占着）；A 的 applist 只有 alpha
A 点 beta                  HTTP 503 another client is already streaming this application
强杀 A 的客户端             alpha 后端仍报 BUSY（placebo），A 的 serverinfo 也仍报 BUSY
clipponctl release alpha   后端 currentgame=0，A 的 serverinfo 归零，B 的 applist 恢复两条
强杀 B 的客户端后 B 发 /cancel  /cancel 按会话表路由到 beta（此时没有 expose），后端归零
网关重启后再串 alpha        日志 "alpha still reports an app; cancelling it first" 之后正常开始
47990 代理                expose beta -> 307（beta 的 Web UI），hide -> 连接被关
```

两个实例的实时封面、输入座椅、私有音频都沿用原有机制，未改动。单元测试 74 项通过。

## 当前状态

- `clippon-net` 与 `clippond` 都在运行（`clippon-net` 与 `clippond` 各重启过一次以加载本次改动）。
- 目前没有正在运行的实例；验证用的两个临时客户端（`/tmp/ml-a`、`/tmp/ml-b`）与它们的证书已经删掉，`clients.json` 复原成原来的 10 条。
- 网关在另一台机器的 Moonlight 上表现为一台主机，应用列表里每个实例一条；同机两个客户端可以同时各串一路（见十四）。
- 封面是这个实例的实时截图（见十二）；客户端把封面缓存到磁盘，旧客户端要清一次缓存。
- 单元测试 74 项通过。

## 目录

```text
README.md   面向使用者的说明：这有什么用、原理、安装与命令
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
