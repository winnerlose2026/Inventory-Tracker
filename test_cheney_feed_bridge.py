"""Tests for the Cheney feed bridge (scripts/sync_cheney_feeds.py).

Covers the 2026-08-26 transport work: the landing is the GoDaddy cPanel host,
which can only give a distributor FTPS (explicit TLS on 21), not its own
SSH-SFTP user -- so FTPS is the default and SSH-SFTP is opt-in.

The pull path is exercised against Cheney's real 2026-08-04 drop with a stub
transport, so it proves the routing without touching a network: order guides
must never reach the on-hand endpoint (posting one would zero out every
warehouse), a genuine headed on-hand CSV must, and 810s are parsed only.

Runs standalone or under pytest.
"""
import importlib.util
import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, ".")

ROOT = Path(__file__).parent
FIX = ROOT / "tests" / "fixtures" / "cheney"
ON_HAND_EXAMPLE = ROOT / "integrations" / "examples" / "cheney_brothers_inventory.example.csv"


def _load_bridge():
    path = ROOT / "scripts" / "sync_cheney_feeds.py"
    spec = importlib.util.spec_from_file_location("_sync_cheney_feeds_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BRIDGE = _load_bridge()

_FEED_VARS = [k for k in (
    "CHENEY_FEED_TRANSPORT", "CHENEY_SFTP_HOST", "CHENEY_SFTP_PORT",
    "CHENEY_SFTP_USER", "CHENEY_SFTP_PASSWORD", "CHENEY_SFTP_KEY",
    "CHENEY_SFTP_DIR", "CHENEY_SFTP_PROCESSED_DIR", "CHENEY_SFTP_CSV_GLOB",
    "CHENEY_SFTP_810_GLOB", "SFTP_HOST", "SFTP_USERNAME_CHENEY",
    "SFTP_PASSWORD_CHENEY", "SFTP_INCOMING_DIR", "SFTP_PROCESSED_DIR",
)]


@contextmanager
def env(**kw):
    """Set the given feed vars, clear every other one, restore on exit."""
    saved = {k: os.environ.get(k) for k in _FEED_VARS}
    for k in _FEED_VARS:
        os.environ.pop(k, None)
    os.environ.update({k: v for k, v in kw.items() if v is not None})
    try:
        yield
    finally:
        for k in _FEED_VARS:
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in saved.items() if v is not None})


class FakeTransport:
    """Serves files from disk and records what was filed away."""

    scheme = "ftps"
    label = "ftps://fake"

    def __init__(self, files: dict[str, Path]):
        self._files = files
        self.filed: list[tuple[str, str, str]] = []
        self.closed = False

    def listdir(self, path):
        return list(self._files)

    def download(self, remote_path):
        return self._files[remote_path.rsplit("/", 1)[-1]].read_bytes()

    def file_away(self, remote_dir, name, processed_dir):
        self.filed.append((remote_dir, name, processed_dir))

    def close(self):
        self.closed = True


def _run(files, argv, **envkw):
    """Run main() with a stub transport + stub POST. Returns (rc, posts, conn)."""
    conn = FakeTransport(files)
    posts: list[tuple[str, int]] = []

    def fake_post(url, data_bytes, token, content_type="text/csv"):
        posts.append((url, len(data_bytes)))
        return 200, '{"ok": true}'

    real_open, real_post, real_argv = BRIDGE._open_transport, BRIDGE._post, sys.argv
    BRIDGE._open_transport = lambda: conn
    BRIDGE._post = fake_post
    sys.argv = ["sync_cheney_feeds.py", *argv]
    try:
        with env(CHENEY_SFTP_HOST="sftp.hhbagels.com", CHENEY_SFTP_USER="cheneybrothers",
                 CHENEY_SFTP_PASSWORD="x", **envkw):
            rc = BRIDGE.main()
    finally:
        BRIDGE._open_transport, BRIDGE._post, sys.argv = real_open, real_post, real_argv
    return rc, posts, conn


# --------------------------------------------------------------------------
# transport selection
# --------------------------------------------------------------------------

def test_transport_defaults_to_ftps():
    with env(CHENEY_SFTP_HOST="h", CHENEY_SFTP_USER="u"):
        assert BRIDGE._transport_name() == "ftps"
    print("ok: cPanel landing means FTPS is the default transport")


def test_private_key_or_port_22_implies_sftp():
    with env(CHENEY_SFTP_HOST="h", CHENEY_SFTP_USER="u", CHENEY_SFTP_KEY="/k/id_rsa"):
        assert BRIDGE._transport_name() == "sftp"
    with env(CHENEY_SFTP_HOST="h", CHENEY_SFTP_USER="u", CHENEY_SFTP_PORT="22"):
        assert BRIDGE._transport_name() == "sftp"
    print("ok: an SSH key or port 22 selects real SSH-SFTP")


def test_explicit_transport_wins_and_bad_value_is_loud():
    with env(CHENEY_SFTP_HOST="h", CHENEY_SFTP_USER="u",
             CHENEY_SFTP_KEY="/k/id_rsa", CHENEY_FEED_TRANSPORT="ftps"):
        assert BRIDGE._transport_name() == "ftps"
    with env(CHENEY_SFTP_HOST="h", CHENEY_SFTP_USER="u", CHENEY_FEED_TRANSPORT="scp"):
        try:
            BRIDGE._transport_name()
        except SystemExit as exc:
            assert "ftps" in str(exc)
        else:
            raise AssertionError("a bogus transport should not be silently ignored")
    print("ok: explicit transport wins; a typo fails loudly instead of defaulting")


def test_shared_sftp_env_names_are_accepted_as_fallbacks():
    """So the cPanel account only has to be configured once, for both pollers."""
    with env(SFTP_HOST="sftp.hhbagels.com", SFTP_USERNAME_CHENEY="cheneybrothers",
             SFTP_PASSWORD_CHENEY="pw"):
        assert BRIDGE._feed_configured()
        assert BRIDGE._host() == "sftp.hhbagels.com"
        assert BRIDGE._user() == "cheneybrothers"
        assert BRIDGE._password() == "pw"
    with env():
        assert not BRIDGE._feed_configured()
    print("ok: sftp_inbox's shared SFTP_* names work as fallbacks")


def test_unconfigured_run_is_a_clean_noop():
    real_argv, real_open = sys.argv, BRIDGE._open_transport

    def boom():
        raise AssertionError("must not connect when nothing is configured")

    BRIDGE._open_transport = boom
    sys.argv = ["sync_cheney_feeds.py"]
    try:
        with env():
            assert BRIDGE.main() == 0
    finally:
        sys.argv, BRIDGE._open_transport = real_argv, real_open
    print("ok: an unconfigured run exits 0 without connecting (safe to schedule)")


# --------------------------------------------------------------------------
# pull + routing, against the real 2026-08-04 drop
# --------------------------------------------------------------------------

def _real_drop():
    files = {p.name: p for p in sorted(FIX.glob("*.EDI"))}
    files.update({p.name: p for p in sorted(FIX.glob("OrderGuide-*.csv"))})
    return files


def test_order_guides_are_never_posted_to_the_on_hand_endpoint():
    files = _real_drop()
    assert sum(1 for n in files if n.startswith("OrderGuide")) == 2, files.keys()
    rc, posts, conn = _run(files, ["--commit"])
    assert rc == 0
    assert posts == [], f"order guide reached the on-hand endpoint: {posts}"
    print("ok: zeroed order guides are reported, never POSTed as on-hand")


def test_edi_files_are_pulled_and_parsed():
    files = _real_drop()
    assert sum(1 for n in files if n.endswith(".EDI")) == 7
    rc, posts, conn = _run(files, [])
    assert rc == 0
    # every file the bridge understood gets filed away on a commit run
    rc, _, conn = _run(files, ["--commit"])
    filed = {name for _, name, _ in conn.filed}
    assert filed == set(files), f"not all handled files were filed: {set(files) - filed}"
    assert all(d == "processed" for _, _, d in conn.filed)
    print("ok: all 7 real 810s + both guides pull, parse and get filed away")


def test_dry_run_leaves_the_drop_untouched():
    rc, posts, conn = _run(_real_drop(), [])
    assert rc == 0
    assert conn.filed == [], "a dry run must not move anything on the server"
    assert conn.closed, "the connection should always be closed"
    print("ok: a dry run moves nothing and still closes the connection")


def test_a_genuine_on_hand_csv_does_get_posted():
    """The whole point of the feed: when Cheney sends real quantities, it flows."""
    files = {ON_HAND_EXAMPLE.name: ON_HAND_EXAMPLE}
    rc, posts, conn = _run(files, ["--commit"])
    assert rc == 0
    assert len(posts) == 1, posts
    assert "dry_run=0" in posts[0][0] and ON_HAND_EXAMPLE.name in posts[0][0]
    print("ok: a real headed on-hand CSV is POSTed to the tracker")


def test_processed_dir_can_be_disabled():
    rc, posts, conn = _run(_real_drop(), ["--commit"], CHENEY_SFTP_PROCESSED_DIR="")
    assert rc == 0
    assert conn.filed == []
    print("ok: filing away can be turned off for a drop we don't own")


def test_non_matching_files_are_ignored():
    files = dict(_real_drop())
    files["README.pdf"] = ON_HAND_EXAMPLE  # content irrelevant; extension isn't
    rc, posts, conn = _run(files, ["--commit"])
    assert rc == 0
    assert "README.pdf" not in {name for _, name, _ in conn.filed}
    print("ok: files outside the CSV/EDI globs are left alone")


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
    print("\nall cheney_feed_bridge tests passed")
