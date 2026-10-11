# 设计：同时串流多个实例

目标：两台设备（或同一台设备上的两个 Moonlight 进程）可以同时各串一个实例，各自独立画面、输入与声音，互不干扰。

**状态：已实施（2026-10-11）**。实施记录与验证见 `docs/WORKLOG.md` 十四；下面保留原始方案，与落地结果不同的地方在对应小节里标注。

改动之前：每个实例都有自己的 netns 与一套 Sunshine，能同时存在，但宿主上只有一组 GameStream 端口，靠动态 DNAT 指向"当前暴露的实例"，所以同一时刻只有一路能串起来。要解除这个限制，需要两块改动，它们可以分开做、分开验证：

1. **网关按客户端回答**：让每个客户端只看到自己的会话状态，别的设备就看不到"这台主机上有应用在跑"。
2. **路由改成每实例一组端口**：每个实例的 Sunshine 用自己的 base port，宿主把这些端口静态映射到各自后端。

## 已核实的源码事实

Sunshine 源码取 `~/Downloads/Sunshine-2026.1008.155609`，Moonlight 取 `~/Downloads/moonlight-qt-6.2.0`，客户端库取 `moonlight-stream/moonlight-common-c` 的 `master`。

| 事实 | 证据 |
| --- | --- |
| 客户端只在 `state` 以 `_SERVER_BUSY` 结尾时才认 `currentgame`，否则一律当作 0 | `app/backend/nvhttp.cpp:117-130` |
| `currentGameId` 决定客户端发 `/resume` 还是 `/launch` | `app/streaming/session.cpp:1614` |
| 列表里"哪个应用正在运行"的高亮也来自这个值 | `app/gui/appmodel.cpp:18`、`:276-300` |
| 退出应用时先发 `/cancel`，**再查一次 serverinfo**；若 currentgame 仍非 0，抛 599 并提示"这个流不是本机启动的" | `app/backend/nvhttp.cpp:249-269` |
| 配对失败的提示同样读 currentGameId | `app/backend/computermanager.cpp:607` |
| 一个 Moonlight 进程一次只有一个会话（单例 + 信号量 1） | `app/streaming/session.cpp:66-67` |
| 手工添加主机时地址按 URL 解析，端口缺省 47989 | `app/backend/computermanager.cpp:734` |
| 主机身份用 `uniqueid`，判等也用它 | `app/backend/nvcomputer.cpp:140`、`:107-120` |
| HTTP/HTTPS 端口来自 serverinfo 的 `ExternalPort`（主机级，覆盖用户输入的端口） | `app/backend/nvcomputer.cpp:186-189` |
| 流端口由服务端协商：客户端从 RTSP SETUP 响应的 `server_port=` 取值，47998/47999/48000 只是解析失败时的回退 | `RtspConnection.c:1276-1282`、`:1192-1198`、`:1320-1326` |
| RTSP 目标网址来自 `/launch` 响应的 `sessionUrl0` | `RtspConnection.c:955`、`:973-975` |
| Sunshine 的 base port 可配置（范围 `1019..65514`），HTTP/HTTPS/WebUI/RTSP/UDP 都是它的固定偏移（0 / −5 / +1 / +21 / +9 / +10 / +11） | `src/nvhttp.h:47`、`src/nvhttp.h:52`、`src/confighttp.h:29`、`src/config.cpp:1869`、`src/config.cpp:883` |
| 一个 Sunshine 后端一次只跑一个 App，`/launch` 会拒绝第二个 | `src/nvhttp.cpp:1376-1380` |
| 同一 App 允许多个客户端同时接入（`session_count`） | `src/rtsp.cpp:753`、`src/nvhttp.cpp:1388`、`:1497` |

三条结论从这些事实直接得出：

- 客户端的"拒绝"来自它读到的 `state`/`currentgame`，这两个字段是网关自己写的，所以可以按客户端分别回答。
- 客户端的流端口由服务端协商，所以后端换一组端口不会让客户端失联。
- 一个 Moonlight 进程一次只能串一路，所以"同一台设备同时开两路"需要两个客户端进程；多台设备各串一路没有客户端侧的障碍。

## 第一部分：网关按客户端回答

### 会话表

在 `clippon/front.py` 里加一张表，键是客户端证书指纹（`_peer_fingerprint()` 已经拿到，配对判定也在用），值是它自己的会话：

```text
fingerprint -> (instance, external app id)
```

（落地时值是实例名，外部 ID 由实例与后端报的 app id 现算 —— 它就是 `external_app_id(instance, internal)`，不必再存一份。表放在 `clippon/sessions.py`。）

- **写入**：`/launch`、`/resume` 转发成功之后。
- **删除**：`/cancel` 成功之后。
- **收敛**：每次回答 serverinfo 时核对后端真实状态 —— 记录的实例已经不在跑那个应用，就把这条记录清掉。这样异常退出、丢包、客户端不发 `/cancel` 都不会留下永久占用。

### `/serverinfo`

- HTTPS 上已配对的请求：只报告该客户端自己的会话。有会话且后端仍在跑 → `SUNSHINE_SERVER_BUSY` 加该实例的外部 id；其余情况 → `SUNSHINE_SERVER_FREE`。
- HTTP（发现阶段，没有客户端证书）：保持现在的全局回答。那时还没有会话，配对状态本来就是 0。

这一条不是锦上添花：`quitApp()` 在 `/cancel` 之后还会再查一次 serverinfo（`nvhttp.cpp:249-269`），如果网关因为别的实例在跑而继续报 BUSY，A 的客户端会以为自己的应用退不掉，弹 599。按客户端回答之后，A 退出时看到的是自己那条已经归零的记录。

### `/applist`

两种粒度，都不影响外部 id 的生成（`gateway.external_app_id` 只由实例名与内部 id 决定），所以客户端缓存的 id 不会错乱：

| 粒度 | 客户端 B 看到 | 改动 |
| --- | --- | --- |
| 只隐藏状态 | `A、B`，但只有启动者那一侧显示"运行中" | 最小，只改 serverinfo |
| 隐藏被占用的实例 | 只有 `B`；客户端 A 自己仍看到 `A、B` | 在合并 applist 时按会话表过滤 |

建议直接做第二种，它对应"第二个设备看到的是一台只有 B、没有被启动过的干净主机"。

两种粒度都要补一个兜底：客户端可能拿着缓存的旧列表去点已被别人占用的实例，网关在 `/launch` 那一步直接回一个明确的失败（等价于 Sunshine 的"这台主机上已有应用在运行"），不必把请求转给后端。

两条都做了（`merge_applists(hidden=...)` 与 `/launch` 的 503）。另外，被隐藏的实例的 id 仍然留在网关的路由表里，所以缓存了旧 id 的客户端得到的是"应用已被别人串流"这个明确回答，而不是 404。

### 客户端侧的缓存

Moonlight 会把主机身份与 applist 落盘（`app/backend/nvcomputer.cpp:96-105`，uuid 在 `:29`），被占用状态的变化要等那台设备重新进入主机时才刷新。这个延迟可以接受，但要知道它存在。

## 第二部分：路由改成每实例一组端口

每实例一组端口的做法：给每个实例一个自己的 Sunshine base port，整组端口随之平移（组内需要 22 个端口；建议组间留 1000，例如 `47989`、`48989`、`49989`），宿主把这些端口静态映射到各自后端。这样两个会话的端口天然错开，不需要再"切换"。

分配与记录：

- `clippon/daemon.py` 在创建实例时分配 base port，写进实例记录（`meta.json` 与 helper 的实例状态）。**落地时改成由 helper 分配**：它本来就是 IP 的分配者，两边各分一次会撞；helper 在 `create()` 里选出空闲组并返回，daemon 拿到之后才写 `sunshine.conf` 与 `meta.json`。
- `clippon/instance.py` 的 `sunshine_conf()` 现在硬编码 `port = 47989`，改成写该实例的 base。

网关访问后端：

- `clippon/front.py` 里现在用常量 `GAMESTREAM_HTTPS`（47984）/ `GAMESTREAM_HTTP`（47989）/ `WEB_UI`（47990）访问后端，改成按该实例的 base 计算（−5 / 0 / +1）。
- `sessionUrl0` 的改写保持"只改主机、保留端口"（`gateway.rewrite_session_host`），于是客户端拿到的是 `宿主地址:base+21`。

内核映射：

- `clippon/ports.py` 的 `nft_table()` 从"一组随时切换的 DNAT"改成"每实例一组的静态 DNAT"：TCP `base+21`、UDP `base+9/+10/+11` → `10.200.0.N`，`output` 链的本机地址规则同样按实例生成。规则数约 `5 × N × 2`，N = 4 时四十条。
- `clippon/front.py` 的 `_expose()` 不再切端口。`clipponctl expose` / `hide` 的语义变成"47990 的 Web UI 代理指向哪个实例"，其余端口对每个实例常开。

## 两部分怎么配合

客户端 B 点开应用 B → 请求打到网关的 47984（HTTPS）→ 网关转发给 B 后端（`10.200.0.3:base_B−5`）→ B 后端把 `sessionUrl0` 定成自己的 `base_B+21` → 网关只把主机改写成客户端拨号地址 → 客户端连 `宿主:base_B+21`、`宿主:base_B+9/+10/+11` → 宿主按端口静态映射到 `10.200.0.3`。同一时刻客户端 A 走的是另一组端口，两条流在端口层面完全不重叠。

## 分阶段实施

1. **每实例 base port**，网关访问后端按 base 计算，`sunshine.conf` 写入 base。此时路由仍是单路，用来确认单客户端串流与 `clipponctl` 行为没有退化。
2. **nftables 改成每实例静态映射**。这一步之后，服务端已经能同时承载两路，只是客户端还不知道可以这么做。
3. **按客户端回答 serverinfo/applist**（会话表）。这一步引入"隐藏"，需要和 placebo 的问题一起考虑。
4. **联调**：两台设备同时串两个不同实例，检查画面、输入与音频各自独立。
5. **收尾**：`expose`/`hide` 语义、`clipponctl list` 输出、README 与 `docs/OPEN-ISSUES.md` 同步。

先做 1、2 的理由：它们是纯服务端改动，单个客户端的行为不变，验证方式是"原来能串的现在仍然能串"，风险低。第 3 步才引入新的对外行为。

五步都已实施完（1–3、5 一次提交；4 在同机两个客户端上完成：两个实例各自的 `sunshine.log` 都出现 `CLIENT CONNECTED`，两个客户端都解出画面并收到音频包，conntrack 里两条流的端口各自映射到自己的后端）。

## 边界与风险

- **placebo 的残留 BUSY 会拖住占用判定**。我们的应用条目没有 `cmd`，客户端退出后后端会一直报告应用在跑（`docs/OPEN-ISSUES.md` 的"会话"一节）。占用判定要以后端状态收敛，就必须同时解决它：客户端打开 `quitAppAfter`、`/launch` 前先幂等 `/cancel`、或按内核计数器判断存活，任选其一，并补一个手动释放手段（例如 `clipponctl release`）。**落地时选了前两条中的后两条**：`/launch` 先幂等 `/cancel`（实测日志 `alpha still reports an app; cancelling it first`），并加了 `clipponctl release NAME`；按内核计数器判断存活仍然是没做的那条，记在 `docs/OPEN-ISSUES.md`。
- **端口规划**：每组 22 个端口，避开 Docker 与常用服务；`ports.py` 里往 `DOCKER-USER` 插入规则的逻辑已经有，但这台机器上 Docker 没跑过，属于既有未验证项。组间隔 1000，`47989`、`48989`、`49989`……`1019..65514` 之内共 18 组。
- **客户端是否总走协商端口**：源码显示是，实际要在第 4 步确认。判据是两路画面各自正确、互不串流；如果发现某一路被送回另一路的端口，说明走到了回退值，需要在宿主上按源地址分流作为兜底。**已确认走协商端口**：两路的 RTSP 分别落在 `base+21` 上，客户端解码正常，没有出现回退端口。
- **资源预算**：N 路并行 = N 个 VAAPI 编码器 + N 路带宽，实用上限取决于这台机器的 GPU 与网卡。实测三路（两个局域网客户端加一个本机客户端）同时串三个 Application 未见明显问题；再往上的上限没有量过。
- **同一台设备要同时看两路**：Moonlight 一个进程一个会话，需要跑两个客户端进程（Linux 上用不同的 `XDG_CONFIG_HOME`，Windows 上用 portable 模式），这一条属于客户端用法，不需要改服务端。
- **本机客户端**：`output` 钩子的规则数随实例数增长，`127.0.0.1` 仍然不通（netns 回不到宿主 loopback），本机客户端要用局域网地址。

## 改动清单（供实施时对照）

| 文件 | 改动 |
| --- | --- |
| `clippon/instance.py` | `sunshine_conf()` 写每实例 base port；base 记进 `meta.json` |
| `clippon/daemon.py` | 建立实例时先 `create` 拿端口再写配置；新增 `release`；`list` 带 base |
| `clippon/front.py` | 后端端口按 base 计算；会话表；`/serverinfo`、`/applist`、`/launch` 按客户端回答；去掉切端口 |
| `clippon/gateway.py` | applist 合并时按客户端过滤；`/launch` 被占用时的明确失败 |
| `clippon/sessions.py` | 新增：会话表（谁在串哪个实例，按后端状态收敛） |
| `clippon/ports.py` | `PortGroup`、`allocate_base_port()`；`nft_table()` 改为每实例静态映射 |
| `clippon/net_helper.py` | 实例记录里带 base port，`create` 分配、`_apply_nft` 按实例生成 |
| `clippon/ctl.py`、`clipponctl.py` | `expose`/`hide` 语义调整，新增 `release`；`list` 输出多一列 base |
| `tests/test_core.py` | 端口分配、端口组偏移、会话表、隐藏 applist、nft 规则生成的单测 |
| `README.md`、`docs/OPEN-ISSUES.md` | 用法与遗留问题同步 |
