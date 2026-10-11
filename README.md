<p align="center">
  <!-- Logo left blank on purpose. Drop an image next to this file and put its
       name in src: <img src="clippon-logo.png" alt="Clippon logo" width="168"/> -->
</p>

<h1 align="center">Clippon</h1>

<p align="center">
  <strong>Off-screen, and out of each other's way</strong><br/>
  Run several programs side by side on one machine, each on a screen of its own
  with its own keyboard, mouse and sound, while the desktop you are sitting at
  stays untouched. Moonlight sees all of them as Applications of a single host.
</p>

<p align="center">
  Arch Linux · one headless cage compositor and Sunshine per session · private network, input and audio · userspace GameStream gateway
</p>

---

## What this is

Clippon turns a normal Linux program into something Moonlight can open. It runs the program inside a headless [cage](https://github.com/cage-kiosk/cage) compositor and starts a [Sunshine](https://github.com/LizardByte/Sunshine) backend against that compositor's Wayland socket, so the program's window, its audio and the input coming back from the client all behave as if the machine were a GameStream host running that one application. The rendering really happens in that compositor: the program draws into a display of its own, which exists only for the stream. The desktop session running on the machine is not involved in it, and its own windows stay where they are.

The reason the project exists is that a Sunshine host is a single point of contention. Sunshine binds a fixed set of GameStream ports — 47989 for the plain HTTP control plane, 47984 for HTTPS, 47990 for the web UI, and a handful of stream ports — so a second Sunshine on the same machine cannot start, and the first one only ever has one session running at a time (`/launch` refuses with `An app is already running on this host`). Many people work around this by owning several machines, or by serializing their sessions on one machine.

Clippon keeps one machine and gives each session its own everything:

- **Its own network stack.** Every session lives in its own network namespace with its own address and its own group of GameStream ports, so several Sunshine backends can listen at the same time without colliding. A root helper owns the namespaces, the bridge between them, and one static DNAT rule per session that maps that session's stream ports to its address.
- **Its own screen, input and sound.** Every session has a private cage compositor, its own virtual keyboard and mice, and a private PipeWire/pipewire-pulse/WirePlumber stack whose sink only that session's Sunshine can see. Two clients can therefore stream two different programs at the same time — one playing a game, the other watching a video — and each client hears only its own program, while the machine's desktop keeps its windows, its keyboard, its mouse and its speakers to itself.
- **One shared GameStream front.** A userspace gateway running in the host namespace answers 47984 and 47989 itself. It merges the backends' application lists, so Moonlight sees one host with one Application per session, and it routes `/launch`, `/resume`, `/cancel` and the stream ports to the right backend. Each client is answered from its own session, so opening an Application does not make the other clients see a busy host. Pairing is implemented in the gateway too, so a client that has paired with the host stays paired no matter which session it opens.

The foreground mode of the original shell script is still there: `clippon -- <command>` runs one session on the host network with no daemon, no namespaces and no gateway.

## How it works

The picture below is the whole system. On the left is the host, on the right are the sessions, and in the middle is the root helper that connects them.

```text
Moonlight ──► 47989 / 47984 (gateway, HTTPS+HTTP, userspace)
              │        merges applists, answers each client from its own session,
              │        routes launch, rewrites sessionUrl0
              ├──► 48010/47998/47999/48000 ── nftables DNAT ──► 10.200.0.2 (the first group)
              ├──► 49010/48998/48999/49000 ── nftables DNAT ──► 10.200.0.3 (the next group)
              └──► 47990 ── TCP proxy ──► the web UI of the exposed session

host                                    sessions (one network namespace each)
cbr0 10.200.0.1  ◄── veth ──────────►   clippon-alpha   10.200.0.2   cage + app + sunshine, base port 47989
                 ◄── veth ──────────►   clippon-beta    10.200.0.3   cage + app + sunshine, base port 48989
```

### A session: cage plus Sunshine

`clippon` creates one private audio stack, starts cage headless, asks wlroots for the output name, sets the requested mode with `wlr-randr`, and then starts Sunshine against the compositor's socket. The application itself is started by cage, so it always has a display to draw on. Resolution defaults to `2560x1440@120Hz` and is settable with `--res` or `STREAM_RES`.

Shutdown is ordered deliberately: cage is signalled first, and the private audio stack is torn down only after Sunshine has exited, because Sunshine restores the default sink over that Pulse socket on its way out and hangs when the socket is already gone.

### Isolation: one namespace per session

Each session gets a network namespace (`clippon-<name>`), a veth pair, an address on a bridge owned by the host (`cbr0`, `10.200.0.1/24`, sessions from `10.200.0.2`) and a base port of its own — the first session gets 47989, the next 48989, and every other port it needs is a fixed offset from that base (HTTP +0, HTTPS −5, web UI +1, video +9, control +10, audio +11, RTSP +21). The helper allocates the group when it creates the session and the daemon writes it into that session's `sunshine.conf`. The default route points back at the host, and an nftables table (`ip clippon`) maps every session's stream ports to its address at once, plus the masquerading. `clippon-net` is a root system service that does this work; `clippond` is a user service that asks for it over a Unix socket, authenticated by `SO_PEERCRED` against the owner recorded at install time. The helper runs with `KillMode=process`, so a helper restart does not kill sessions — and because sessions can therefore outlive both services, the helper reconciles on start and sweeps leftovers by two markers: the seat a process was given, and the private audio directory it points at.

Cage deliberately stays in the host namespace. udev delivers hotplug events over a netlink socket that only exists in the initial namespace, and a compositor that cannot see input devices appear is a compositor with no input.

### The gateway: several backends, one host

`clippond` binds 47984, 47989 and 47990 in the host namespace and behaves like a GameStream host:

- `/serverinfo` is answered by the gateway, with `LocalIP` set to the address the client actually dialed and `PairStatus` derived from the client's certificate. `currentgame` and `state` are answered per client, from a table of client certificate to session: a client is told about the session it launched and about nothing else, and a record is dropped as soon as that session's backend reports the application gone. That table is what lets a second client start another Application while the first one is streaming. The plain HTTP port carries no certificate, so it keeps the old global answer — it is only used before pairing. `/applist` is a merge of the backends' lists, where the title of each entry is the session name and the external id is a CRC32 of session and internal id, because Moonlight stores those ids and hands them back later; sessions another client is streaming are left out of the list, though their ids stay resolvable.
- `/launch`, `/resume` and `/cancel` are forwarded to the backend the id belongs to — a cancel goes to the session the client itself launched. On a launch the gateway rewrites `sessionUrl0` to the address the client dialed (a backend only knows its own `10.200.0.N`) and records the session. If another client is streaming that session the launch is refused with a clear message; if only a leftover application is in the way, the gateway cancels it first, which makes launch idempotent (`An app is already running on this host` never reaches a client). `clipponctl release NAME` does the same by hand for a session whose client vanished.
- Port 47984 is TLS terminated with pyOpenSSL. CPython's `ssl` module cannot verify a self-signed client certificate — a Moonlight certificate fails the handshake with `unknown_ca`, and the module exposes no verify callback — so the gateway asks for a certificate, accepts whatever arrives, and then authorizes each request by comparing the SHA-256 of that certificate against `clients.json`. `/serverinfo` and `/pair` stay open to unknown clients, because that is how a client discovers it has to pair.
- Pairing is implemented in the gateway: the five phases of the GameStream handshake, with the PIN that Moonlight shows submitted by `clipponctl pin NNNN`.

Box art is answered by the gateway as well. Moonlight asks for `AssetType=2` and draws the answer at a fixed 200×267, so the gateway finds the session's cage display (by matching the seat tag of the process that holds each Wayland socket), screenshots it with `grim`, and composes a 3:4 card: the whole frame centred and undistorted over a blurred copy of itself. Anything it cannot answer — an unknown id, another asset type, a session with no display — falls through to the backend.

### Input: one seat per session

Sunshine's virtual keyboard and mice are ordinary `uinput` devices, and by default every compositor on the machine would read them, which is how keystrokes end up on the host desktop instead of in the session. Clippon separates them by seat:

- A small `LD_PRELOAD` shim renames each virtual device to `<name>@<session>`. Sunshine is packaged with a file capability, which sets `AT_SECURE` and makes the loader ignore `LD_PRELOAD` entirely — ambient capabilities do not help — so the helper spawns a capability-free copy of the binary for the session.
- A udev rule runs a helper that reads that tag and assigns the device to `seat-cage-<session>`, and a second shim makes cage report that seat name.
- Cage then opens that seat and no other, so each session sees only its own keyboard and mice, and the host desktop sees neither.

One more shim exists because wlroots 0.20 never reads the `WL_OUTPUT` property that would map a pointer onto the headless output: absolute pointer events would leave the cursor where it is. The shim maps every attached input device onto the output layout's centre output. It also hides that cursor while a touchscreen is the latest input, and shows it again on the next mouse move, click or scroll. Cage otherwise keeps the pointer image on screen, and with software cursors that image is part of the captured frame.

### Audio: one stack per session

Every session runs its own `pipewire`, `pipewire-pulse` and `wireplumber` inside its own D-Bus session, with `PIPEWIRE_RUNTIME_DIR` pointing into `cage-audio-<session>`. The application plays into a sink that exists only there, Sunshine captures that sink, and the host's own audio stack is never involved. Running the daemons on a private bus also keeps `pipewire-pulse` from fighting the desktop for the D-Bus name `org.pulseaudio.Server`.

## Getting started

Clippon targets Arch Linux. `install.py` and `install-daemon.py` check `/etc/os-release` and refuse anything else.

### Install

```bash
git clone <this repository> ~/Clippon
cd ~/Clippon

python3 install.py          # packages, seat shim, udev rule  (uses sudo)
python3 install-daemon.py   # clippon-net (system) + clippond (user)
```

`install.py` installs the official packages (cage, grim, wlr-randr, PipeWire, seatd, iproute2, Python with cryptography, pyOpenSSL and Pillow), builds the seat shim into `/usr/local/lib`, installs the udev rule and links `~/.local/bin/clippon`. Sunshine is not in the official repositories; when `sunshine` is not on `PATH` the script installs the AUR package `sunshine-beta-bin` with `yay` or `paru`.

`install-daemon.py` builds the remaining shims (the cursor mapping and the device-name tag) and the seat-tag helper, writes `/var/lib/clippon/owner.json`, installs `/etc/systemd/system/clippon-net.service` and `~/.config/systemd/user/clippond.service`, and enables both. Run it as your own user, or through `sudo` from that user; both installers print every command and support `--dry-run`.

### Run a session

```bash
clipponctl start alpha --res 1920x1080 -- /path/to/program
clipponctl list
```

`clipponctl start` asks the user daemon to create the session; the daemon writes the per-session Sunshine configuration and the helper creates the namespace, the veth pair, the address and the port group before the compositor starts. `list` shows each session with its address, its base port, its pid and the command it runs. A session stops with `clipponctl stop alpha`.

`--encoder` chooses that session's Sunshine video encoder: `vaapi`, `vulkan`, or `soft` (Sunshine's software encoder). The default is `vaapi`. The choice is written into that session's `sunshine.conf` as `encoder = vaapi`, `encoder = vulkan`, or `encoder = software`.

`--quick` 套用一份预设，并且可以不写会话名。名字取命令的可执行文件名：`/usr/bin/dolphin` 得到 `dolphin`，`wlc /path/to/game.exe` 得到 `wlc`。名字写在选项前面时，仍用那个名字。

```bash
clipponctl start --quick -- dolphin
clipponctl start alpha --quick -- /usr/bin/dolphin
```

预设的出厂内容是 `--res 1920x1080@120Hz`、四条 fcitx 变量（`GTK_IM_MODULE`、`QT_IM_MODULE`、`XMODIFIERS=@im=fcitx`、`SDL_IM_MODULE`）和 `--encoder vaapi`。同一条 `start` 上再写的 `--res`、`--env`、`--encoder` 只对这一次生效。`clipponctl set-quick` 不带参数时打印当前预设；带上的选项会改掉对应的那一项并保存。给出 `--env` 会换掉整组环境变量，不给则保留。配置在 `${XDG_CONFIG_HOME:-~/.config}/clippon/quick.json`，删掉这个文件就回到出厂预设。`set-quick` 不经过 `clippond`。文件名不符合 `[a-z][a-z0-9-]{0,11}` 时，把名字写在 `--quick` 前面。

To run one session without the daemon, on the host network:

```bash
clippon --res 1920x1080 -- /path/to/program
```

### Pair with Moonlight, then open an Application

Add a host in Moonlight with the machine's LAN address (the gateway listens on every address of the host). The first connection asks for a PIN; Moonlight shows a four-digit one, and

```bash
clipponctl pin 1234
```

completes the pairing. The client's certificate is recorded in the gateway's `clients.json`, so later connections — and connections after a gateway restart — do not pair again.

The application list then contains one entry per session, named after the session, minus any session another client is already streaming. Opening one starts that session's Sunshine and hands the client a session URL for the host it already knows about, on that session's own stream ports. Several clients can do this at once, each with a different Application. Box art for each entry is a screenshot of that session, taken when Moonlight asks for it.

## Command reference

| Command | What it does |
| --- | --- |
| `clippon [--verbose] [--res AxB[@C[Hz]]] [--env K=V]... [--] <command>` | Run one session in the foreground, on the host network. |
| `clipponctl start NAME [--verbose] [--res ...] [--encoder vaapi\|vulkan\|soft] [--env K=V]... [--] <command>` | Create a session in its own namespace and start it. |
| `clipponctl start --quick [--res ...] [--encoder ...] [--env K=V]... [--] <command>` | Start with the saved preset. The session name is the command's file name unless a NAME is written first. |
| `clipponctl set-quick [--res ...] [--encoder ...] [--env K=V]...` | Print the `--quick` preset, or replace the parts you name. |
| `clipponctl list` | Sessions, their addresses, base ports, pids and commands. |
| `clipponctl stop NAME` | Stop a session, its application and its audio stack. |
| `clipponctl release NAME` | Stop what a session is running and forget the client sessions claiming it. |
| `clipponctl expose NAME` / `hide` | Point the 47990 web UI proxy at a session, or at nothing. |
| `clipponctl pin NNNN` | Submit the PIN Moonlight is showing. |

| Environment variable | Meaning |
| --- | --- |
| `STREAM_RES=WxH[@R[Hz]]` | Output mode inside cage. Default `2560x1440@120Hz`; `--res ''` keeps wlroots' 1280x720. |
| `STREAM_SINK=NAME` | Name of the private sink the application plays into. Default `cage-sink`. |
| `STREAM_TRAY=auto\|enabled\|disabled` | Sunshine's tray. `auto` disables it when no StatusNotifier host is present, which is the usual case on a headless machine. |
| `STREAM_AUDIO=disabled` | Skip the private audio stack; the application then uses the host's default sink. |
| `STREAM_LOG_DIR=DIR` | Where logs go. Default `$XDG_STATE_HOME/clippon/instances/<name>`, and `$XDG_STATE_HOME/cage-sunshine` without `--instance`. |

`--env KEY=VALUE` sets one more variable for cage and for the program it starts. It can be repeated, and it is how a session gets something the daemon's own environment does not have — input methods are the usual case:

```bash
clipponctl start alpha \
  --env GTK_IM_MODULE=fcitx \
  --env QT_IM_MODULE=fcitx \
  --env XMODIFIERS=@im=fcitx \
  --env SDL_IM_MODULE=fcitx \
  -- kitty
```

A name given this way beats the same name in the daemon's environment. The session's own settings are not up for grabs: the sink, the audio mode and the tray come from the start request, and the display, the private audio socket and the shims are set by the session itself.

`clippond` is a user service, so it does not inherit the graphical session. Each session copies the desktop identity from the systemd user manager when that name is not already set: `XDG_CURRENT_DESKTOP`, `XDG_SESSION_DESKTOP`, `DESKTOP_SESSION`, `KDE_FULL_SESSION`, `KDE_SESSION_VERSION` and `XDG_CONFIG_DIRS`. Qt uses the desktop name to load the KDE platform theme, and that theme reads `~/.config/kdeglobals`, which is where Plasma stores the color scheme, the icon theme, the widget style and the fonts. A program started here follows the same theme as one started on the desktop. `--env` overrides any of these names.

## Files and state

| Path | Contents |
| --- | --- |
| `${XDG_CONFIG_HOME:-~/.config}/clippon/quick.json` | The `--quick` preset. Absent means the built-in 1920x1080@120Hz, fcitx, vaapi preset. |
| `~/.local/state/clippon/gateway/` | `identity.json`, the gateway's TLS material, and `clients.json` with the paired client certificates. |
| `~/.local/state/clippon/instances/<name>/` | Per-session `sunshine.conf` and logs. |
| `/run/user/<uid>/clippon/daemon.sock` | The user daemon's control socket. |
| `/run/clippon/net.sock` | The root helper's control socket. |
| `/var/lib/clippon/owner.json` | The user allowed to talk to the helper. |
| `/run/user/<uid>/cage-audio-<name>/` | A session's private PipeWire runtime directory. |
| `/usr/local/lib/lib{seat_seat_cage,wlr_cursor_map,clippon_vhid_name}.so` | Shims for the seat name, the cursor mapping and the device tags. |
| `/usr/local/libexec/clippon-seat-tag` | The udev helper that maps a device tag to a seat. |
| `/etc/udev/rules.d/99-sunshine-cage.rules` | The rule that calls it. |
| `/usr/local/lib/clippon/sunshine` | The capability-free copy of Sunshine the helper spawns. |

## Limitations

- **Arch Linux only.** The installers are Arch-specific on purpose and reject other systems.
- **One application per session at a time.** The session's Sunshine refuses a second launch, exactly like an ordinary host. Two clients cannot share a session either: while one is streaming it, the other's launch of that Application is refused, and its list entry is hidden until the stream ends or the session is released.
- **A client that dies leaves its session occupied.** Sunshine's application entries have no command, so they keep reporting "running" until someone stops them. The gateway clears that on the next launch of the same Application, and `clipponctl release NAME` clears it by hand; until then the session stays hidden from other clients.
- **Client certificates are fingerprinted, not verified.** The gateway cannot validate a self-signed certificate chain, so a client is trusted as soon as its certificate is listed in `clients.json` — which means anyone who can write that file can add a certificate of their own.
- **Two streams on one device need two clients.** One Moonlight process holds one session, so watching two sessions from the same machine means two processes with separate configuration directories (`XDG_CONFIG_HOME=/tmp/moonlight-a moonlight ...` on Linux, a portable install on Windows).
- **Box art is a moment in time.** It is a screenshot taken when Moonlight asks, cached for 20 seconds in the gateway, and cached again by Moonlight itself, so a client with an old cached cover keeps showing it until that cache is cleared.
- **Docker was never tested alongside this.** The helper inserts a forwarding rule for the session network when `DOCKER-USER` exists, but Docker was inactive on the machine where the rule was written.
- **Sessions belong to the helper's cgroup.** Because the helper keeps `KillMode=process`, instance processes show up as systemd leftovers in its unit; nothing is limited today, but a `MemoryMax` on that unit would apply to all sessions together.

## Development

```bash
python3 -m pytest tests/test_core.py
```

The tests cover argument parsing, the session and audio configuration, the namespace and reclaim logic, the gateway documents and DNAT table, pairing, client gating, and the box-art composition; they do not start cage or Sunshine.

## Code layout

```text
bin/        clippon, clippond, clippon-net, clipponctl launchers
assets/     the LD_PRELOAD shims, the udev rule and its tag helper
clippon/    the package
docs/       work log, open issues and the design notes
tests/      unit tests
install.py         Arch dependencies, shims, udev rule
install-daemon.py  root helper, user daemon and their units
```

Inside `clippon/`:

```text
config.py      options, STREAM_* environment, paths
session.py     one session: order of start and shutdown
cage.py        the compositor's environment, its client wrapper, socket discovery
audio.py       the private PipeWire stack
sunshine.py    Sunshine's environment, tray plan and capability-free copy
net_helper.py  root helper: namespaces, veth, nftables, spawn, reclaim
daemon.py      user daemon: session lifecycle, the exposed session
ctl.py         the clipponctl client
front.py       the GameStream gateway: listeners, gating, routing, TLS
gateway.py     the documents it owns and the rewrites it applies
sessions.py    which client is streaming which session
artwork.py     live box art: find the display, screenshot it, compose the card
pair.py        the pairing state machine
clients.py     paired client certificates
identity.py    the gateway's certificate and unique id
ports.py       port arithmetic and the nftables table
proc.py        process and /proc helpers
```

## Docs

- `docs/WORKLOG.md` — what was built, with the evidence for each step.
- `docs/OPEN-ISSUES.md` — what is still open, and the decisions to leave things alone.
- `docs/DESIGN-multi-session.md` — how several sessions came to run at once: per-client answers and per-session port groups.
- `docs/DESIGN-ondemand-backend.md` — a banked design: starting a backend only while a session is being streamed.
