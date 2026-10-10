"""Pure-function checks. Nothing here starts cage, Sunshine, or PipeWire."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout
from pathlib import Path

from clippon.audio import AudioInstance, pipewire_dropin, wireplumber_dropin
from clippon.cage import cage_environment, client_argv, wayland_socket_on_line
from clippon.config import DEFAULT_RES, ClipponError, parse_args, resolution_ok
from clippon.ctl import format_list, parse_ctl
from clippon.daemon import clippon_argv
from clippon.gateway import (
    classify,
    external_app_id,
    may_use_host,
    merge_applists,
    replace_query_appid,
    rewrite_session_host,
    serverinfo_xml,
    xml_text,
)
from clippon.instance import (
    apps_document,
    instance_seat,
    seat_name,
    sunshine_conf,
    validate_name,
    write_instance,
)
from clippon.net_helper import orphan_namespaces, spawn_command
from clippon import net_helper
from clippon.pair import Pairing
from clippon.ports import allocate_host, docker_user_checks, lan_from_ip_json, nft_table
from clippon.proc import all_pids, env_has_pair, env_value, shell_status
from clippon.protocol import dumps, loads
from clippon.sunshine import fatal_lines, managed_env, managed_tray, tray_plan
from clippon.units import system_unit, user_unit


class ResolutionTests(unittest.TestCase):
    def test_accepts_the_forms_the_shell_script_accepted(self) -> None:
        for value in ("", "1920x1080", "1920X1080", "2560x1440@120", "2560x1440@120Hz", "2560x1440@120.5hz"):
            self.assertTrue(resolution_ok(value), value)

    def test_rejects_junk(self) -> None:
        for value in ("1080p", "1920x1080@", "1920x", "abc"):
            self.assertFalse(resolution_ok(value), value)


class ParseTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = parse_args(["--", "kitty"], environ={"HOME": "/home/me", "XDG_RUNTIME_DIR": "/run/user/1"})
        self.assertEqual(cfg.command, ("kitty",))
        self.assertEqual(cfg.resolution, DEFAULT_RES)
        self.assertEqual(cfg.sink, "cage-sink")
        self.assertEqual(cfg.audio, "enabled")
        self.assertEqual(cfg.tray, "auto")
        self.assertEqual(cfg.runtime_dir, Path("/run/user/1"))
        self.assertEqual(cfg.log_dir, Path("/home/me/.local/state/cage-sunshine"))

    def test_res_flag_overrides_environment_and_empty_means_default_mode(self) -> None:
        env = {"STREAM_RES": "1920x1080", "HOME": "/home/me"}
        cfg = parse_args(["--res", "1280x720@60Hz", "kitty"], environ=env)
        self.assertEqual(cfg.resolution, "1280x720@60Hz")
        cfg = parse_args(["--res=", "kitty"], environ=env)
        self.assertEqual(cfg.resolution, "")

    def test_empty_stream_res_is_kept(self) -> None:
        cfg = parse_args(["true"], environ={"STREAM_RES": "", "STREAM_SINK": "", "HOME": "/home/me"})
        self.assertEqual(cfg.resolution, "")
        self.assertEqual(cfg.sink, "")

    def test_options_after_the_command_belong_to_the_command(self) -> None:
        cfg = parse_args(["kitty", "--res", "1920x1080"], environ={"HOME": "/home/me"})
        self.assertEqual(cfg.command, ("kitty", "--res", "1920x1080"))

    def test_double_dash_ends_options(self) -> None:
        cfg = parse_args(["--", "--res"], environ={"HOME": "/home/me"})
        self.assertEqual(cfg.command, ("--res",))

    def test_missing_command(self) -> None:
        with self.assertRaises(ClipponError) as caught:
            parse_args(["--verbose"], environ={"HOME": "/home/me"})
        self.assertEqual(caught.exception.code, 2)

    def test_bad_res_and_unknown_option(self) -> None:
        with self.assertRaises(ClipponError):
            parse_args(["--res", "1080p", "kitty"], environ={"HOME": "/home/me"})
        with self.assertRaises(ClipponError):
            parse_args(["--nope", "kitty"], environ={"HOME": "/home/me"})
        with self.assertRaises(ClipponError):
            parse_args(["--res"], environ={"HOME": "/home/me"})

    def test_help_exits(self) -> None:
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            parse_args(["--help"])
        self.assertEqual(caught.exception.code, 0)


class ClientArgvTests(unittest.TestCase):
    def test_preload_is_stripped_and_mode_is_applied_before_exec(self) -> None:
        argv = client_argv(("kitty", "-e", "sh"), "")
        self.assertEqual(argv[:4], ["env", "-u", "LD_PRELOAD", "--"])
        self.assertEqual(argv[4:], ["kitty", "-e", "sh"])

        argv = client_argv(("steam", "-tenfoot"), "1920x1080")
        self.assertEqual(argv[0], "env")
        self.assertIn("STREAM_RES_IN=1920x1080", argv)
        self.assertIn("sh", argv)
        self.assertEqual(argv[-2:], ["steam", "-tenfoot"])


class ProcTests(unittest.TestCase):
    def test_shell_status_matches_bash_wait(self) -> None:
        self.assertEqual(shell_status(0), 0)
        self.assertEqual(shell_status(1), 1)
        self.assertEqual(shell_status(-15), 143)
        self.assertEqual(shell_status(-2), 130)

    def test_env_pair_does_not_match_a_prefix(self) -> None:
        blob = b"A=1\0PIPEWIRE_RUNTIME_DIR=/run/user/1/cage-audio\0B=2"
        self.assertTrue(env_has_pair(blob, "PIPEWIRE_RUNTIME_DIR", "/run/user/1/cage-audio"))
        self.assertFalse(env_has_pair(blob, "PIPEWIRE_RUNTIME_DIR", "/run/user/1/cage-audio-extra"))
        self.assertFalse(env_has_pair(blob, "PIPEWIRE_RUNTIME_DIR", "/run/user/1"))


class SocketLineTests(unittest.TestCase):
    def test_pid_must_match_exactly(self) -> None:
        line = 'u_str LISTEN 0 0 /run/user/1/wayland-3 0 * 0 users:(("cage",pid=123,fd=7))'
        self.assertEqual(wayland_socket_on_line(line, 123), "wayland-3")
        self.assertIsNone(wayland_socket_on_line(line, 12))
        self.assertIsNone(wayland_socket_on_line(line, 1234))


class AudioConfigTests(unittest.TestCase):
    def test_sink_name_is_the_only_interpolation(self) -> None:
        text = pipewire_dropin("cage-sink")
        self.assertIn("name = cage-sink", text)
        self.assertIn("node.name        = cage-sink", text)
        self.assertNotIn("monitor.alsa", text)
        isolated = wireplumber_dropin()
        self.assertIn("monitor.alsa = disabled", isolated)
        self.assertNotIn("cage-sink", isolated)


class TrayTests(unittest.TestCase):
    def test_explicit_modes_do_not_ask_the_bus(self) -> None:
        self.assertEqual(tray_plan("disabled").args, ("system_tray=disabled",))
        self.assertTrue(tray_plan("disabled").disabled)
        self.assertEqual(tray_plan("enabled").args, ())
        self.assertFalse(tray_plan("enabled").disabled)
        self.assertEqual(managed_tray("auto"), "disabled")
        self.assertEqual(managed_tray("disabled"), "disabled")
        self.assertEqual(managed_tray("enabled"), "enabled")


    def test_env_value_reads_a_single_variable(self) -> None:
        blob = b"A=1\0PIPEWIRE_RUNTIME_DIR=/run/user/1/cage-audio-desk\0B=2\0"
        self.assertEqual(
            env_value(blob, "PIPEWIRE_RUNTIME_DIR"), "/run/user/1/cage-audio-desk"
        )
        self.assertIsNone(env_value(blob, "CLIPPON_SEAT"))


    def test_all_pids_sees_this_process(self) -> None:
        self.assertIn(os.getpid(), all_pids())


class ClientGatingTests(unittest.TestCase):
    """Only the pairing paths answer before a client certificate is known."""

    def test_pairing_paths_work_unpaired(self) -> None:
        for path in ("/serverinfo", "/pair"):
            self.assertTrue(may_use_host(path, paired=False), path)

    def test_everything_else_needs_a_known_client(self) -> None:
        for path in ("/applist", "/appasset", "/launch", "/resume", "/cancel", "/nope"):
            self.assertFalse(may_use_host(path, paired=False), path)
            self.assertTrue(may_use_host(path, paired=True), path)

    def test_both_fingerprint_readers_agree(self) -> None:
        from OpenSSL import crypto

        from clippon.clients import fingerprint, openssl_fingerprint
        from clippon.identity import ensure

        with tempfile.TemporaryDirectory() as directory:
            identity = ensure(Path(directory))
            pem = identity.client_cert.read_text(encoding="utf-8")
            stored = fingerprint(pem).hex()
            read_back = openssl_fingerprint(
                crypto.load_certificate(crypto.FILETYPE_PEM, pem.encode())
            )
        self.assertEqual(stored, read_back)


class ReclaimTests(unittest.TestCase):
    def test_orphan_namespaces_are_the_ones_we_would_have_made(self) -> None:
        existing = [
            "clippon-alpha",   # still recorded
            "clippon-gone",    # recorded nowhere
            "clippon-Bad",     # not a name we hand out
            "clippon-",        # same
            "docker0",         # not ours
        ]
        self.assertEqual(
            orphan_namespaces(existing, {"clippon-alpha"}), ["clippon-gone"]
        )


class SeatTests(unittest.TestCase):
    """The per-instance seat: one cage reads one instance's devices."""

    def test_seat_name_follows_the_instance(self) -> None:
        self.assertEqual(seat_name(""), "seat-cage")
        self.assertEqual(seat_name("desk"), "seat-cage-desk")

    def test_instance_seat_needs_the_name_shim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            shim = Path(directory) / "libclippon_vhid_name.so"
            missing = Path(directory) / "absent.so"
            self.assertEqual(instance_seat("desk", missing), "")
            self.assertEqual(instance_seat("", shim), "")
            shim.write_bytes(b"")
            self.assertEqual(instance_seat("desk", shim), "seat-cage-desk")

    def test_cage_environment_reports_the_instance_seat(self) -> None:
        env = cage_environment({}, seat="seat-cage-desk")
        self.assertEqual(env["CLIPPON_SEAT"], "seat-cage-desk")
        self.assertIn("libseat_seat_cage.so", env["LD_PRELOAD"])
        plain = cage_environment({})
        self.assertNotIn("CLIPPON_SEAT", plain)

    def test_managed_sunshine_env_tags_the_device_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vhid = Path(directory) / "libclippon_vhid_name.so"
            vhid.write_bytes(b"")
            env = managed_env(vhid, "@desk")
        self.assertEqual(env["CLIPPON_VHID_TAG"], "@desk")
        self.assertEqual(env["LD_PRELOAD"], str(vhid))

    def test_missing_vhid_shim_leaves_devices_untagged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "not-built.so"
            env = managed_env(missing, "@desk")
        self.assertNotIn("CLIPPON_VHID_TAG", env)
        self.assertNotIn("LD_PRELOAD", env)

    def test_sunshine_is_spawned_from_a_capability_free_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sunshine"
            copy = root / "clippon" / "sunshine"
            source.write_bytes(b"first")
            with mock.patch.object(net_helper, "SUNSHINE_BIN", source), mock.patch.object(
                net_helper, "SUNSHINE_SPAWN", copy
            ):
                self.assertEqual(net_helper.sunshine_spawn_path(), str(copy))
                self.assertEqual(copy.read_bytes(), b"first")
                source.write_bytes(b"second")
                os.utime(source, (1, 1))
                self.assertEqual(net_helper.sunshine_spawn_path(), str(copy))
                self.assertEqual(copy.read_bytes(), b"second")
            with mock.patch.object(net_helper, "SUNSHINE_BIN", root / "absent"):
                self.assertEqual(net_helper.sunshine_spawn_path(), "sunshine")

    def test_seat_tag_helper_maps_the_device_name(self) -> None:
        helper = Path(__file__).resolve().parent.parent / "assets" / "clippon-seat-tag"
        with tempfile.TemporaryDirectory() as directory:
            sysfs = Path(directory)
            for kernel, name in (("event9", "libvirtualhid Keyboard@desk"),
                                 ("event10", "libvirtualhid Mouse")):
                (sysfs / kernel / "device").mkdir(parents=True)
                (sysfs / kernel / "device" / "name").write_text(name, encoding="utf-8")
            env = dict(os.environ, CLIPPON_SYSFS=str(sysfs))
            tagged = subprocess.run(
                [str(helper), "event9"], env=env, capture_output=True, text=True, check=True
            )
            plain = subprocess.run(
                [str(helper), "event10"], env=env, capture_output=True, text=True, check=True
            )
            unknown = subprocess.run(
                [str(helper), "event99"], env=env, capture_output=True, text=True, check=True
            )
        self.assertEqual(tagged.stdout.strip(), "ID_SEAT=seat-cage-desk")
        self.assertEqual(plain.stdout.strip(), "ID_SEAT=seat-cage")
        self.assertEqual(unknown.stdout.strip(), "ID_SEAT=seat-cage")


class FatalLineTests(unittest.TestCase):
    def test_last_matching_lines_are_kept_by_the_caller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sunshine.log"
            path.write_text("ok\nFatal: failed to bind\nAddress already in use\n", encoding="utf-8")
            lines = fatal_lines(path)
        self.assertEqual(lines, ["Fatal: failed to bind", "Address already in use"])


class InstanceLayoutTests(unittest.TestCase):
    def test_named_instance_uses_its_own_log_and_audio_dirs(self) -> None:
        cfg = parse_args(
            ["--instance", "desk", "--", "sleep", "30"],
            environ={"HOME": "/home/me", "XDG_RUNTIME_DIR": "/run/user/1"},
        )
        self.assertEqual(cfg.instance, "desk")
        self.assertEqual(cfg.log_dir, Path("/home/me/.local/state/clippon/instances/desk"))
        audio = AudioInstance(Path("/run/user/1"), "cage-sink", io.BytesIO(), name="desk")
        self.assertEqual(audio.root, Path("/run/user/1/cage-audio-desk"))
        plain = AudioInstance(Path("/run/user/1"), "cage-sink", io.BytesIO())
        self.assertEqual(plain.root, Path("/run/user/1/cage-audio"))

    def test_name_is_short_enough_for_an_interface(self) -> None:
        validate_name("desk")
        validate_name("a")
        with self.assertRaises(ValueError):
            validate_name("Desk")
        with self.assertRaises(ValueError):
            validate_name("this-name-is-too-long")
        with self.assertRaises(ClipponError):
            parse_args(["--instance", "Nope", "true"], environ={"HOME": "/home/me"})

    def test_sunshine_conf_is_one_app_and_does_not_publish_upnp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_instance(root, "desk", ["sleep", "30"], "1280x720", "cage-sink", "enabled", "auto", False)
            text = (root / "sunshine.conf").read_text(encoding="utf-8")
            apps = json_apps(root / "apps.json")
        self.assertIn("upnp = disabled", text)
        self.assertIn("sunshine_name = desk", text)
        self.assertIn(str(root / "apps.json"), text)
        self.assertEqual(apps["apps"][0]["name"], "desk")
        self.assertNotIn("cmd", apps["apps"][0])
        self.assertIn(f"file_apps = {root / 'apps.json'}", sunshine_conf(root, "desk"))
        self.assertIn('"name": "desk"', apps_document("desk"))


class PortTableTests(unittest.TestCase):
    def test_expose_dnats_only_the_stream_ports(self) -> None:
        exposed = nft_table("10.200.0.2")
        self.assertIn("dnat to 10.200.0.2", exposed)
        for port in (47998, 47999, 48000, 48010):
            self.assertIn(str(port), exposed)
        for port in (47984, 47989, 47990):
            self.assertNotIn(str(port), exposed)
        self.assertNotIn("dnat to", nft_table(None))
        self.assertEqual(allocate_host(set()), 2)
        self.assertEqual(allocate_host({2, 3}), 4)

    def test_local_clients_get_the_same_dnat_on_the_output_hook(self) -> None:
        def output_chain(text: str) -> str:
            # The address set contains "}", so cut on the chain's own terminator.
            return text.split("chain output {")[1].split("\n  }")[0]

        text = nft_table("10.200.0.2", ["192.168.4.4", "10.0.0.7"])
        output = output_chain(text)
        self.assertIn("ip daddr { 10.0.0.7, 192.168.4.4 } tcp dport", output)
        self.assertIn("dnat to 10.200.0.2", output)
        for port in (47998, 47999, 48000, 48010):
            self.assertIn(str(port), output)
        # Without a LAN address there is nothing to match on locally, and a
        # bare "tcp dport 48010" would hijack traffic to other hosts.
        self.assertNotIn("dnat", output_chain(nft_table("10.200.0.2")))
        self.assertNotIn("dnat", output_chain(nft_table(None, ["192.168.4.4"])))

    def test_lan_addresses_skip_the_bridge_and_docker(self) -> None:
        payload = [
            {
                "ifname": "enp0s1",
                "addr_info": [{"family": "inet", "local": "192.168.1.20", "scope": "global"}],
            },
            {
                "ifname": "cbr0",
                "addr_info": [{"family": "inet", "local": "10.200.0.1", "scope": "global"}],
            },
            {
                "ifname": "lo",
                "addr_info": [{"family": "inet", "local": "127.0.0.1", "scope": "host"}],
            },
            {
                "ifname": "docker0",
                "addr_info": [{"family": "inet", "local": "172.17.0.1", "scope": "global"}],
            },
        ]
        self.assertEqual(lan_from_ip_json(payload), ["192.168.1.20"])

    def test_docker_user_rules_accept_the_subnet(self) -> None:
        (check, insert), _sources = docker_user_checks()
        self.assertEqual(check[:3], ["iptables", "-C", "DOCKER-USER"])
        self.assertIn("10.200.0.0/24", insert)


class GatewayPureTests(unittest.TestCase):
    def test_merged_applist_roundtrips_and_keeps_titles_as_instance_names(self) -> None:
        alpha = "<root><App><AppTitle>ignored</AppTitle><ID>1</ID><IsHdrSupported>0</IsHdrSupported></App></root>"
        beta = "<root><App><AppTitle>also</AppTitle><ID>1</ID></App></root>"
        merged, table = merge_applists([("alpha", alpha), ("beta", beta)])
        self.assertIn(">alpha<", merged)
        self.assertIn(">beta<", merged)
        self.assertEqual(len(table), 2)
        self.assertEqual({pair[0] for pair in table.values()}, {"alpha", "beta"})
        for external, (instance, internal) in table.items():
            self.assertIn(f"<ID>{external}</ID>", merged)
            self.assertEqual(internal, "1")
            self.assertEqual(external, external_app_id(instance, internal))

    def test_serverinfo_uses_the_dialed_address(self) -> None:
        xml = serverinfo_xml(local_ip="192.168.4.4", unique_id="ABC", hostname="clippon")
        self.assertIn("<LocalIP>192.168.4.4</LocalIP>", xml)
        self.assertIn("<uniqueid>ABC</uniqueid>", xml)
        self.assertIn("<hostname>clippon</hostname>", xml)
        self.assertNotIn("10.200.0.2", xml)
        self.assertIn("<PairStatus>0</PairStatus>", xml)
        self.assertIn("<PairStatus>1</PairStatus>", serverinfo_xml(local_ip="1.2.3.4", unique_id="A", paired=True))
        self.assertEqual(classify("/serverinfo?uniqueid=1"), "local")
        self.assertEqual(classify("/launch?appid=9"), "forward")
        self.assertEqual(
            replace_query_appid("/launch?appid=9&mode=1", "42"),
            "/launch?appid=42&mode=1",
        )

    def test_session_url_host_is_replaced_and_the_port_stays(self) -> None:
        xml = "<root><sessionUrl0>rtsp://10.200.0.2:48010</sessionUrl0></root>"
        rewritten = rewrite_session_host(xml, "192.168.1.20")
        self.assertIn("rtsp://192.168.1.20:48010", rewritten)
        self.assertNotIn("10.200.0.2", rewritten)
        encrypted = "<root><sessionUrl0>rtspenc://10.200.0.3:48010</sessionUrl0></root>"
        self.assertIn("rtspenc://192.168.1.20:48010", rewrite_session_host(encrypted, "192.168.1.20"))


class CtlProtocolTests(unittest.TestCase):
    def test_start_is_one_json_line_and_stops_at_the_command(self) -> None:
        request = parse_ctl(
            ["start", "desk", "--res", "1920x1080", "--", "kitty", "-e", "sh"],
            environ={"HOME": "/home/me"},
        )
        line = dumps(request)
        self.assertTrue(line.endswith(b"\n"))
        self.assertNotIn(b"\n", line[:-1])
        parsed = loads(line)
        self.assertEqual(parsed["op"], "start")
        self.assertEqual(parsed["name"], "desk")
        self.assertEqual(parsed["command"], ["kitty", "-e", "sh"])
        self.assertEqual(parsed["resolution"], "1920x1080")

    def test_list_hide_expose_stop(self) -> None:
        self.assertEqual(parse_ctl(["list"]), {"op": "list"})
        self.assertEqual(parse_ctl(["hide"]), {"op": "hide"})
        self.assertEqual(parse_ctl(["expose", "desk"]), {"op": "expose", "name": "desk"})
        self.assertEqual(parse_ctl(["stop", "desk"]), {"op": "stop", "name": "desk"})
        with self.assertRaises(ClipponError):
            parse_ctl(["expose"])
        with self.assertRaises(ClipponError):
            parse_ctl(["start", "Nope", "--", "true"])

    def test_format_list_marks_the_exposed_instance(self) -> None:
        text = format_list(
            {
                "lan": ["192.168.1.20"],
                "exposed": "desk",
                "instances": [
                    {"name": "desk", "ip": "10.200.0.2", "pid": 40, "command": ["sleep", "30"]},
                    {"name": "other", "ip": "10.200.0.3", "pid": None, "command": ["true"]},
                ],
            }
        )
        self.assertIn("192.168.1.20", text)
        self.assertIn("desk", text)
        self.assertIn("exposed", text)
        self.assertIn("10.200.0.3", text)


class SpawnCommandTests(unittest.TestCase):
    def test_nsenter_does_not_fork_and_setpriv_keeps_groups(self) -> None:
        command = spawn_command("/run/netns/clippon-desk", "acite", 1000, ["python", "clippon"])
        self.assertEqual(command[:3], ["nsenter", "-F", "--net=/run/netns/clippon-desk"])
        self.assertIn("--reuid=acite", command)
        self.assertIn("--regid=1000", command)
        self.assertIn("--init-groups", command)
        self.assertEqual(command[-2:], ["python", "clippon"])

    def test_child_argv_selects_the_instance_before_the_command(self) -> None:
        argv = clippon_argv("desk", ["kitty", "-e", "sh"], "1920x1080", True)
        self.assertIn("--instance", argv)
        self.assertEqual(argv[argv.index("--instance") + 1], "desk")
        self.assertIn("--verbose", argv)
        self.assertEqual(argv[-6:], ["--res", "1920x1080", "--", "kitty", "-e", "sh"])

    def test_managed_sunshine_env_keeps_sunshine_off_the_system_bus(self) -> None:
        env = managed_env()
        self.assertTrue(env["DBUS_SYSTEM_BUS_ADDRESS"].startswith("unix:path="))
        self.assertNotIn("LD_PRELOAD", env)

    def test_units_name_the_launchers(self) -> None:
        system = system_unit("/usr/bin/python3", "/repo/bin/clippon-net")
        user = user_unit("/usr/bin/python3", "/repo/bin/clippond")
        self.assertIn("ExecStart=/usr/bin/python3 /repo/bin/clippon-net", system)
        self.assertIn("RuntimeDirectory=clippon", system)
        self.assertIn("ExecStart=/usr/bin/python3 /repo/bin/clippond", user)
        self.assertIn("WantedBy=default.target", user)


class PairingRoundTripTests(unittest.TestCase):
    def test_a_client_that_knows_the_pin_gets_paired(self) -> None:
        import hashlib
        import os
        import threading

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
        from cryptography import x509 as x509mod
        import datetime

        def certificate(name: str):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509mod.Name([x509mod.NameAttribute(x509mod.NameOID.COMMON_NAME, name)])
            cert = (
                x509mod.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(1)
                .not_valid_before(datetime.datetime.now(datetime.UTC))
                .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
                .sign(key, hashes.SHA256())
            )
            return (
                cert.public_bytes(Encoding.PEM).decode(),
                key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
                cert,
                key,
            )

        server_pem, server_key, server_cert, _server_private = certificate("clippon")
        client_pem, _client_key, client_cert, client_private = certificate("moonlight")
        pairing = Pairing(server_pem, server_key)
        pin = "1234"
        salt = os.urandom(16)
        aes_key = hashlib.sha256(salt + pin.encode()).digest()[:16]

        def aes(data: bytes, encrypt: bool) -> bytes:
            cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
            transform = cipher.encryptor() if encrypt else cipher.decryptor()
            return transform.update(data) + transform.finalize()

        held: dict[str, str] = {}

        def first():
            held["body"] = pairing.handle(
                "/pair?uniqueid=client-1&phrase=getservercert"
                f"&salt={salt.hex()}&clientcert={client_pem.encode().hex()}",
                pin_timeout=5,
            )

        worker = threading.Thread(target=first)
        worker.start()
        self.assertEqual(pairing.submit_pin(pin), "pin 1234 sent")
        worker.join(5)
        self.assertIn("paired>1<", held["body"].replace("<paired>", "paired>"))
        plain = bytes.fromhex(xml_text(held["body"], "plaincert") or "")
        self.assertEqual(plain.decode(), server_pem)

        challenge = os.urandom(16)
        challenge_xml = pairing.handle(
            f"/pair?uniqueid=client-1&clientchallenge={aes(challenge, True).hex()}"
        )
        response = aes(bytes.fromhex(xml_text(challenge_xml, "challengeresponse") or ""), False)
        server_hash, server_challenge = response[:32], response[32:48]
        self.assertEqual(len(server_hash), 32)
        self.assertEqual(len(server_challenge), 16)

        client_secret = os.urandom(16)
        stage3 = server_challenge + client_cert.signature + client_secret
        stage3_hash = hashlib.sha256(stage3).digest()
        secret_xml = pairing.handle(
            f"/pair?uniqueid=client-1&serverchallengeresp={aes(stage3_hash, True).hex()}"
        )
        pairing_secret = bytes.fromhex(xml_text(secret_xml, "pairingsecret") or "")
        server_secret, server_signature = pairing_secret[:16], pairing_secret[16:]
        server_cert.public_key().verify(server_signature, server_secret, padding.PKCS1v15(), hashes.SHA256())
        self.assertEqual(
            hashlib.sha256(challenge + server_cert.signature + server_secret).digest(),
            server_hash,
        )
        client_signature = client_private.sign(client_secret, padding.PKCS1v15(), hashes.SHA256())
        done = pairing.handle(
            f"/pair?uniqueid=client-1&clientpairingsecret={(client_secret + client_signature).hex()}"
        )
        self.assertIn("<paired>1</paired>", done)
        final = pairing.handle("/pair?uniqueid=client-1&phrase=pairchallenge")
        self.assertIn("<paired>1</paired>", final)


class PairedClientTests(unittest.TestCase):
    def test_a_paired_certificate_survives_a_restart(self) -> None:
        import datetime

        from cryptography import x509 as x509mod
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from clippon.clients import PairedClients, fingerprint

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509mod.Name([x509mod.NameAttribute(x509mod.NameOID.COMMON_NAME, "moonlight")])
        cert = (
            x509mod.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(7)
            .not_valid_before(datetime.datetime.now(datetime.UTC))
            .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        pem = cert.public_bytes(serialization.Encoding.PEM).decode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clients.json"
            first = PairedClients(path)
            self.assertFalse(first.knows(fingerprint(pem).hex()))
            first.add(pem)
            self.assertTrue(first.knows(fingerprint(pem).hex()))
            second = PairedClients(path)
        self.assertTrue(second.knows(fingerprint(pem).hex()))
        self.assertEqual(second.all(), [pem])


SS_SAMPLE = """\
u_str LISTEN 0      128     /run/user/1000/wayland-proxy-612646 4935749      * 0    users:(("firefox",pid=612646,fd=12))
u_str LISTEN 0      128         /run/user/1000/wayland-2 5062167      * 0    users:(("cage",pid=627654,fd=29),("cage",pid=627654,fd=28))
u_str LISTEN 0      128         /run/user/1000/wayland-0 5111993      * 0    users:(("cage",pid=630532,fd=29))
u_str LISTEN 0      128         /run/user/1000/wayland-1 4474356      * 0    users:(("kwin_wayland_wr",pid=552957,fd=7))
u_str LISTEN 0      1              @/tmp/.X11-unix/X1 4474358      * 0    users:(("Xwayland",pid=553013,fd=9))
"""


class ArtworkTests(unittest.TestCase):
    """Box art is a screenshot of the instance, composed into Moonlight's card."""

    def test_socket_lines_are_read_for_wayland_names_and_pids(self) -> None:
        from clippon.artwork import parse_sockets

        found = parse_sockets(SS_SAMPLE)
        self.assertEqual(
            [(Path(path).name, pids) for path, pids in found],
            [("wayland-2", [627654]), ("wayland-0", [630532]), ("wayland-1", [552957])],
        )

    def test_the_seat_tag_names_the_instance_behind_a_socket(self) -> None:
        from clippon import artwork

        def env(pid: int, key: str) -> str | None:
            table = {
                (630532, "CLIPPON_SEAT"): "seat-cage-alpha",
                (630532, "XDG_RUNTIME_DIR"): "/run/user/1000",
                (627654, "CLIPPON_SEAT"): "seat-cage-beta",
                (627654, "XDG_RUNTIME_DIR"): "/run/user/1000",
                (552957, "CLIPPON_SEAT"): None,
            }
            return table.get((pid, key))

        with mock.patch.object(artwork, "socket_table", return_value=SS_SAMPLE), mock.patch.object(
            artwork, "process_env_value", side_effect=env
        ):
            self.assertEqual(artwork.display_for("alpha"), ("/run/user/1000", "wayland-0"))
            self.assertEqual(artwork.display_for("beta"), ("/run/user/1000", "wayland-2"))
            self.assertIsNone(artwork.display_for("gamma"))

    def test_a_screenshot_is_fitted_into_the_card_without_stretching(self) -> None:
        from PIL import Image, ImageDraw

        from clippon.artwork import CARD, compose

        shot = Image.new("RGB", (1600, 900), (255, 0, 0))
        ImageDraw.Draw(shot).rectangle((800, 0, 1599, 899), fill=(0, 0, 255))
        raw = io.BytesIO()
        shot.save(raw, "PNG")
        card = compose(raw.getvalue())
        self.assertIsNotNone(card)
        with Image.open(io.BytesIO(card or b"")) as picture:
            self.assertEqual(picture.size, CARD)
            row = picture.height // 2
            left = picture.getpixel((5, row))
            right = picture.getpixel((CARD[0] - 6, row))
            corner = picture.getpixel((0, 0))
        # The whole frame is inside the card, left half red and right half blue.
        self.assertGreater(left[0], 150)
        self.assertLess(left[2], 100)
        self.assertGreater(right[2], 150)
        self.assertLess(right[0], 100)
        # The card is filled: no black border where the blurred copy sits.
        self.assertGreater(sum(corner), 60)

    def test_a_screenshot_is_cached_between_requests(self) -> None:
        from clippon.artwork import LiveArtwork

        art = LiveArtwork()
        with mock.patch.object(art, "_capture", return_value=b"png") as capture:
            self.assertEqual(art.box_art("alpha"), b"png")
            self.assertEqual(art.box_art("alpha"), b"png")
        self.assertEqual(capture.call_count, 1)

    def test_a_failed_capture_is_not_cached(self) -> None:
        from clippon.artwork import LiveArtwork

        art = LiveArtwork()
        with mock.patch.object(art, "_capture", side_effect=[None, b"png"]) as capture:
            self.assertIsNone(art.box_art("alpha"))
            self.assertEqual(art.box_art("alpha"), b"png")
        self.assertEqual(capture.call_count, 2)

    def test_capture_asks_grim_for_the_instance_display(self) -> None:
        from clippon.artwork import LiveArtwork

        def fake_run(argv: list[str], **kwargs: object) -> object:
            Path(argv[-1]).write_bytes(self.small_png())
            return mock.Mock(returncode=0, stderr=b"")

        art = LiveArtwork()
        with mock.patch(
            "clippon.artwork.display_for", return_value=("/run/user/1000", "wayland-0")
        ), mock.patch("clippon.artwork.subprocess.run", side_effect=fake_run) as run:
            picture = art.box_art("alpha")
        self.assertIsNotNone(picture)
        argv, kwargs = run.call_args
        self.assertEqual(argv[0][:3], ["grim", "-t", "jpeg"])
        self.assertEqual(kwargs["env"]["WAYLAND_DISPLAY"], "wayland-0")
        self.assertEqual(kwargs["env"]["XDG_RUNTIME_DIR"], "/run/user/1000")

    def test_a_missing_display_leaves_the_request_to_the_backend(self) -> None:
        from clippon.artwork import LiveArtwork

        art = LiveArtwork()
        with mock.patch("clippon.artwork.display_for", return_value=None):
            self.assertIsNone(art.box_art("alpha"))

    def small_png(self) -> bytes:
        from PIL import Image

        raw = io.BytesIO()
        Image.new("RGB", (320, 180), (10, 200, 30)).save(raw, "PNG")
        return raw.getvalue()

    def test_only_box_art_requests_are_answered_here(self) -> None:
        from clippon.front import box_art_request

        self.assertEqual(box_art_request("/appasset?appid=42&AssetType=2&AssetIdx=0"), 42)
        self.assertIsNone(box_art_request("/appasset?appid=42&AssetType=0&AssetIdx=0"))
        self.assertIsNone(box_art_request("/appasset?appid=none&AssetType=2"))
        self.assertIsNone(box_art_request("/appasset?AssetType=2"))
        self.assertIsNone(box_art_request("/applist"))


def json_apps(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
