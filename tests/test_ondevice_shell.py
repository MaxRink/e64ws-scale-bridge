"""End-to-end tests for the on-device shell scripts, in a throwaway container.

uninstall.sh, install.sh and S95scalebridge work on absolute device paths
(/usr/local/www, /db/scale-bridge, /etc/lighttpd, /log), so the only honest way
to exercise them is to run them for real in a root filesystem that is not the
host's.  Docker is the same harness the PHP endpoint tests use; these tests
skip when it is unavailable.

`lighttpd -t` is a stub here: the subject is the installer's control flow (does
it notice that the /api/v2/ exemption survived, does it say so, does it exit
non-zero), not the vendor's config parser.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IMAGE = "python:3.13-slim"

VENDOR = '''# force redirection from http to https
$HTTP["scheme"] == "http" {
    $HTTP["host"] =~ ".*" {
        url.redirect = (".*" => "https://%0$0")
    }
}
'''

PATCHED = VENDOR + '''# BEGIN E64 scale bridge Xenia HTTP
# TRADE-OFF (deliberate): /api/v2/ is plain HTTP on the LAN.
$HTTP["scheme"] == "http" {
    $HTTP["url"] !~ "^/api/v2/" {
        $HTTP["host"] =~ ".*" {
            url.redirect = (".*" => "https://%0$0")
        }
    }
}
# END E64 scale bridge Xenia HTTP
'''

TRUNCATED = "# force redirection from http to https\n"

SETUP = textwrap.dedent("""
    set -eu
    mkdir -p /usr/local/www/api/v2 /usr/local/www/scale /etc/init.d \\
             /db/scale-bridge /storage/scale-bridge/ondevice /etc/lighttpd
    # The routes the installer owns, plus the marker that says so.
    : > /usr/local/www/api/v2/.scale-bridge
    echo '<?php // mako' > /usr/local/www/api/v2/mako.php
    echo '<html>scale</html>' > /usr/local/www/scale/index.html
    printf '#!/bin/sh\\necho stop >> /tmp/init-calls\\n' > /etc/init.d/S95scalebridge
    printf '#!/bin/sh\\necho restart >> /tmp/init-calls\\n' > /etc/init.d/S50lighttpd
    chmod +x /etc/init.d/S95scalebridge /etc/init.d/S50lighttpd
    # lighttpd -t stub: validates by counting braces, like a real parse would
    # notice a cut-off file, and records that it was called.
    cat > /usr/sbin/lighttpd <<'STUB'
    #!/bin/sh
    echo "lighttpd $*" >> /tmp/lighttpd-calls
    conf=$(printf '%s\\n' "$@" | tail -1)
    open=$(tr -cd '{' < "$conf" | wc -c)
    close=$(tr -cd '}' < "$conf" | wc -c)
    [ "$open" = "$close" ]
    STUB
    chmod +x /usr/sbin/lighttpd
    # install.sh installs it 0755; mirror that so the guard matches a device.
    cp /src/ondevice/patch_lighttpd.py /storage/scale-bridge/ondevice/
    chmod 0755 /storage/scale-bridge/ondevice/patch_lighttpd.py
""")


def docker_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        return subprocess.run(
            [docker, "info"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=60, check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


INSTALL_SCRIPT = textwrap.dedent("""
    set -eu
    mkdir -p /usr/local/www /etc/init.d /storage /db /run /log
    set +e
    sh /src/ondevice/install.sh --no-lighttpd > /tmp/first.log 2>&1
    echo "first=$?"
    set -e
    cfg=/db/scale-bridge/config.json
    [ -f "$cfg" ] && echo "config=present" || echo "config=missing"
    echo "mode=$(stat -c %a "$cfg" 2>/dev/null || echo none)"
    python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$cfg" 2>/dev/null \
        && echo "valid=yes" || echo "valid=no"
    [ -e "$cfg.new" ] && echo "leftover=yes" || echo "leftover=no"
    set +e
    sh /src/ondevice/install.sh --no-lighttpd > /tmp/second.log 2>&1
    echo "second=$?"
    grep -q 'Keeping existing' /tmp/second.log && echo "kept=yes" || echo "kept=no"
    # A crash during the write leaves a truncated config. The installer used to
    # keep it forever and never say anything was wrong.
    printf '{"host": "hds.local", "ingest_tok' > "$cfg"
    sh /src/ondevice/install.sh --no-lighttpd > /tmp/third.log 2>&1
    echo "third=$?"
    grep -q 'not valid JSON' /tmp/third.log && echo "loud=yes" || echo "loud=no"
    set -e
""")


class InstallerShellTests(unittest.TestCase):
    def setUp(self) -> None:
        if not docker_available():
            self.skipTest("docker is not available; run tools/run_shell_tests.sh")

    def _run(self, script: str) -> dict:
        result = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{REPO}:/src:ro",
             IMAGE, "sh", "-c", script],
            capture_output=True, text=True, timeout=300, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )

    def test_the_default_config_is_written_atomically_and_kept_valid(self) -> None:
        values = self._run(INSTALL_SCRIPT)
        self.assertEqual(values.get("first"), "0")
        self.assertEqual(values.get("config"), "present")
        # 0640 from the moment it exists: the ingest token lives in this file.
        self.assertEqual(values.get("mode"), "640")
        self.assertEqual(values.get("valid"), "yes")
        self.assertEqual(values.get("leftover"), "no")
        self.assertEqual(values.get("second"), "0")
        self.assertEqual(values.get("kept"), "yes")
        # A truncated config stops the installer instead of being kept forever.
        self.assertEqual(values.get("third"), "1")
        self.assertEqual(values.get("loud"), "yes")


class InstallerSourceTests(unittest.TestCase):
    """Source checks for the property a black-box run cannot observe."""

    def test_the_installer_never_writes_the_config_in_place(self) -> None:
        # `cat > config.json` leaves a truncated live config after a crash and
        # creates the ingest token world-readable; the write must go to a
        # sibling with a restrictive umask and be renamed into place.
        text = (REPO / "ondevice" / "install.sh").read_text(encoding="utf-8")
        # assertFalse, not assertNotIn: a failure should not dump the whole
        # installer into the CI log.
        self.assertFalse('cat > "$DB/config.json"' in text,
                         "the installer writes the live config in place")
        self.assertIn('cat > "$CONFIG_NEW"', text)
        self.assertIn('mv -f "$CONFIG_NEW" "$DB/config.json"', text)
        self.assertIn("umask 077", text)


INIT_SCRIPT = textwrap.dedent("""
    set -eu
    mkdir -p /log /run/scale-bridge /storage/scale-bridge/ondevice /db/scale-bridge
    cp /src/ondevice/scalebridged.py /storage/scale-bridge/ondevice/scalebridged.py
    # start-stop-daemon is a stub: the subject here is the log cap, not the
    # vendor's supervisor.
    for path in /sbin/start-stop-daemon /usr/sbin/start-stop-daemon /bin/start-stop-daemon; do
        mkdir -p "$(dirname "$path")"
        printf '#!/bin/sh\nexit 0\n' > "$path"
        chmod +x "$path"
    done
    # 2 MiB of old log, well over the 1 MiB cap.
    dd if=/dev/zero bs=1024 count=2048 2>/dev/null | tr '\\0' 'x' > /log/scalebridged.log
    before=$(wc -c < /log/scalebridged.log)
    sh /src/ondevice/S95scalebridge start >/dev/null 2>&1 || true
    echo "before=$before"
    if [ -f /log/scalebridged.log.1 ]; then
      echo "rotated=$(wc -c < /log/scalebridged.log.1)"
    else
      echo "rotated=missing"
    fi
    [ -f /log/scalebridged.log ] && echo "fresh=$(wc -c < /log/scalebridged.log)" || echo "fresh=missing"
    # A small log must not be rotated again.
    rm -f /log/scalebridged.log.1
    : > /log/scalebridged.log
    sh /src/ondevice/S95scalebridge start >/dev/null 2>&1 || true
    [ -f /log/scalebridged.log.1 ] && echo "small-rotated=yes" || echo "small-rotated=no"
""")


class UninstallShellTests(unittest.TestCase):
    def setUp(self) -> None:
        if not docker_available():
            self.skipTest("docker is not available; run tools/run_shell_tests.sh")

    def _run(self, *, config: str, backup: str | None) -> tuple[int, str]:
        """Lay out the device tree, run uninstall.sh, report exit code+output."""

        files = {
            "/etc/lighttpd/lighttpd.conf": config,
        }
        if backup is not None:
            files["/db/scale-bridge/lighttpd.conf.vendor"] = backup
        writes = "\n".join(
            "cat > %s <<'E64EOF'\n%sE64EOF\n" % (path, body)
            for path, body in files.items()
        )
        probe = textwrap.dedent("""
            echo "---probe---"
            echo "exit=$code"
            if [ -f /etc/lighttpd/lighttpd.conf ]; then
              echo "marker=$(grep -cF '# BEGIN E64 scale bridge Xenia HTTP' /etc/lighttpd/lighttpd.conf || true)"
            else
              echo "config=missing"
            fi
            [ -d /storage/scale-bridge ] && echo "code=kept" || echo "code=removed"
            [ -d /usr/local/www/api/v2 ] && echo "routes=kept" || echo "routes=removed"
            [ -f /tmp/lighttpd-calls ] && echo "lighttpd-test-ran=yes" || echo "lighttpd-test-ran=no"
            [ -f /tmp/init-calls ] && echo "init=$(tr '\\n' ',' < /tmp/init-calls)" || echo "init=none"
        """)
        script = f"""{SETUP}
{writes}
echo "---uninstall---"
set +e
sh /src/ondevice/uninstall.sh --yes 2>&1
code=$?
set -e
{probe}"""
        result = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{REPO}:/src:ro",
             IMAGE, "sh", "-c", script],
            capture_output=True, text=True, timeout=300, check=False,
        )
        # The uninstaller's own stderr is folded into stdout inside the
        # container, so the two streams keep their real order here.
        output = result.stdout + result.stderr
        marker = "---uninstall---"
        uninstall_output = output.split(marker, 1)[-1]
        probe_at = uninstall_output.find("---probe---")
        if probe_at >= 0:
            uninstall_output = uninstall_output[:probe_at]
        if result.returncode != 0:
            self.fail(f"the test container itself failed ({result.returncode}):\n{output}")
        self.last_probe = output.split("---probe---", 1)[-1]
        # The uninstall exit status, relayed by the probe: the container's own
        # status is the harness's, not the script's.
        return int(self._probe("exit")), uninstall_output

    def _probe(self, key: str) -> str:
        for line in self.last_probe.splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        self.fail(f"probe {key!r} missing from:\n{self.last_probe}")

    def test_a_missing_backup_is_loud_and_exits_non_zero(self) -> None:
        # The measured defect: the guard `[ -f ... ] || true` skipped the
        # restore silently, "Uninstalled." was printed and the cleartext
        # /api/v2/ exemption stayed installed.
        code, output = self._run(config=PATCHED, backup=None)
        self.assertNotEqual(code, 0, output)
        self.assertIn("STILL INSTALLED", output)
        self.assertIn("lighttpd.conf.vendor", output)
        self.assertNotIn("Uninstalled.", output)
        # The exemption really is still there, and the code was kept so the
        # operator can fix it and re-run.
        self.assertEqual(self._probe("marker").strip(), "1")
        self.assertEqual(self._probe("code"), "kept")

    def test_a_corrupt_backup_exits_non_zero_and_is_named(self) -> None:
        code, output = self._run(config=PATCHED, backup=TRUNCATED)
        self.assertNotEqual(code, 0, output)
        self.assertIn("STILL INSTALLED", output)
        self.assertIn("refusing to restore", output)
        self.assertNotIn("Uninstalled.", output)
        self.assertEqual(self._probe("code"), "kept")

    def test_a_valid_restore_uninstalls_and_exits_zero(self) -> None:
        code, output = self._run(config=PATCHED, backup=VENDOR)
        self.assertEqual(code, 0, output)
        self.assertIn("Uninstalled.", output)
        self.assertNotIn("STILL INSTALLED", output)
        self.assertEqual(self._probe("marker").strip(), "0")
        self.assertEqual(self._probe("code"), "removed")
        self.assertEqual(self._probe("routes"), "removed")
        self.assertEqual(self._probe("lighttpd-test-ran"), "yes")
        # lighttpd was restarted only after the config validated.
        self.assertIn("restart", self._probe("init"))

    def test_the_init_script_caps_the_daemon_log(self) -> None:
        # The daemon is started with >>, so the log only grows; /log is on the
        # rootfs of an embedded unit.
        result = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{REPO}:/src:ro",
             IMAGE, "sh", "-c", INIT_SCRIPT],
            capture_output=True, text=True, timeout=300, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        values = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        self.assertEqual(values.get("before"), str(2 * 1024 * 1024))
        self.assertEqual(values.get("rotated"), str(2 * 1024 * 1024))
        # A fresh log file exists (the stub's >> creates it) and is not the old
        # 2 MiB.
        self.assertIn(values.get("fresh"), {"0", "missing"})
        self.assertEqual(values.get("small-rotated"), "no")

    def test_an_unpatched_config_is_left_alone(self) -> None:
        # Nothing to restore: no backup is needed and nothing is reported.
        code, output = self._run(config=VENDOR, backup=None)
        self.assertEqual(code, 0, output)
        self.assertIn("Uninstalled.", output)
        self.assertNotIn("STILL INSTALLED", output)


if __name__ == "__main__":
    unittest.main()
