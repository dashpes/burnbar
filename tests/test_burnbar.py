"""Unit tests for burnbar's pure logic.

Deliberately small and dependency-free (stdlib unittest): they cover the bits
that have actually bitten us — version parsing (broke the release), the live-
session tier selection (showed closed sessions as live) — plus the token/format
helpers. Run with:  python3 -m unittest discover -s tests
"""
import importlib.util
import os
import unittest

# The plugin's filename ('burnbar.30s.py') isn't a valid module name, so load it
# by path. Importing only runs module-level constants + a self-version read; no
# network, no writes.
_PLUGIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "burnbar.30s.py")
_spec = importlib.util.spec_from_file_location("burnbar", _PLUGIN)
bb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bb)


class TestVersion(unittest.TestCase):
    def test_version_tuple(self):
        self.assertEqual(bb.version_tuple("0.6.0"), (0, 6, 0))
        self.assertEqual(bb.version_tuple("1.10.2"), (1, 10, 2))
        # non-numeric parts degrade to 0 so comparisons stay total
        self.assertEqual(bb.version_tuple("1.2.0rc1"), (1, 2, 0))

    def test_version_ordering(self):
        self.assertGreater(bb.version_tuple("0.10.0"), bb.version_tuple("0.9.9"))
        self.assertGreater(bb.version_tuple("1.0.0"), bb.version_tuple("0.99.0"))

    def test_parse_version_header(self):
        self.assertEqual(
            bb.parse_version_header("# <bitbar.version>0.6.0</bitbar.version>"),
            "0.6.0")
        # the exact shape that broke the release: extra <bitbar.version> mentions
        # in comments must not derail the parse.
        text = ("# <bitbar.version>0.6.0</bitbar.version>\n"
                "# the <bitbar.version> header at the top of the file\n")
        self.assertEqual(bb.parse_version_header(text), "0.6.0")
        self.assertIsNone(bb.parse_version_header("no header here"))
        self.assertIsNone(bb.parse_version_header(None))


class TestFormatting(unittest.TestCase):
    def test_compact(self):
        self.assertEqual(bb.compact(999), "999")
        self.assertEqual(bb.compact(1000), "1K")
        self.assertEqual(bb.compact(56000), "56K")
        self.assertEqual(bb.compact(1_200_000), "1.2M")
        self.assertEqual(bb.compact(2_000_000), "2.0M")

    def test_fmt_age(self):
        self.assertEqual(bb.fmt_age(0), "0m")
        self.assertEqual(bb.fmt_age(240), "4m")
        self.assertEqual(bb.fmt_age(3600), "1h")
        self.assertEqual(bb.fmt_age(9000), "2h30m")

    def test_model_short(self):
        self.assertEqual(bb.model_short("claude-opus-4-8"), "opus")
        self.assertEqual(bb.model_short("claude-haiku-4-5-20251001"), "haiku")
        self.assertEqual(bb.model_short(""), "?")
        self.assertEqual(bb.model_short(None), "?")


class TestContextWindow(unittest.TestCase):
    def test_explicit_modes(self):
        self.assertEqual(bb.context_window("claude-opus-4-8", 0, "200k"), bb.CTX_200K)
        self.assertEqual(bb.context_window("claude-sonnet-4-6", 0, "1m"), bb.CTX_1M)

    def test_auto(self):
        # Opus is the 1M-context model
        self.assertEqual(bb.context_window("claude-opus-4-8", 0, "auto"), bb.CTX_1M)
        # everything else is 200K...
        self.assertEqual(bb.context_window("claude-sonnet-4-6", 1000, "auto"),
                         bb.CTX_200K)
        # ...unless it has somehow crossed 200K (1M-beta Sonnet) -> high-water wins
        self.assertEqual(bb.context_window("claude-sonnet-4-6", bb.CTX_200K + 1,
                                           "auto"), bb.CTX_1M)


class TestTokens(unittest.TestCase):
    def test_add_and_weighted(self):
        u = {"input_tokens": 100, "output_tokens": 50,
             "cache_creation_input_tokens": 10, "cache_read_input_tokens": 1000}
        t = bb.new_tokens()
        bb.add_tokens(t, u)
        self.assertEqual(t["input"], 100)
        self.assertEqual(t["output"], 50)
        self.assertEqual(t["cache_read"], 1000)
        # ctx_one = prompt size sent (input + cache, no output)
        self.assertEqual(bb.ctx_one(u), 100 + 10 + 1000)
        # weighted_one discounts cache reads by CACHE_READ_WEIGHT
        self.assertEqual(bb.weighted_one(u),
                         100 + 50 + 10 + 1000 * bb.CACHE_READ_WEIGHT)

    def test_missing_keys_default_zero(self):
        self.assertEqual(bb.ctx_one({}), 0)
        self.assertEqual(bb.weighted_one({}), 0)


def _sv(mtime, cwd="/work", **kw):
    """A minimal by_session value as select_live_mains consumes it."""
    d = {"mtime": mtime, "cwd": cwd, "last_ctx": 5, "agent": False}
    d.update(kw)
    return d


class TestSelectLiveMains(unittest.TestCase):
    """The tier logic behind the 'live agents' panel — the part that used to show
    sessions that were already closed. Candidates arrive newest-first."""

    def setUp(self):
        self.now = 1_000_000
        self.cand = [
            ("s1", _sv(self.now - 10)),     # 10s ago
            ("s2", _sv(self.now - 300)),    # 5m ago
            ("s3", _sv(self.now - 3600)),   # 60m ago (same dir)
        ]

    def keys(self, mains):
        return [k for k, _ in mains]

    def test_nothing_running_shows_none(self):
        # processes readable, none alive -> (0, {}); closed sessions must vanish
        self.assertEqual(self.keys(bb.select_live_mains(self.cand, 0, {}, self.now)), [])

    def test_precise_one_process_one_row(self):
        mains = bb.select_live_mains(self.cand, 1, {"/work": 1}, self.now)
        self.assertEqual(self.keys(mains), ["s1"])

    def test_precise_two_processes_two_rows(self):
        mains = bb.select_live_mains(self.cand, 2, {"/work": 2}, self.now)
        self.assertEqual(self.keys(mains), ["s1", "s2"])

    def test_precise_budget_is_per_dir(self):
        cand = [("a", _sv(self.now, "/x")),
                ("b", _sv(self.now - 5, "/x")),
                ("c", _sv(self.now - 5, "/y"))]
        mains = bb.select_live_mains(cand, 2, {"/x": 1, "/y": 1}, self.now)
        self.assertEqual(self.keys(mains), ["a", "c"])  # newest in /x, plus /y

    def test_count_only_caps_and_recency_gates(self):
        # lsof blocked: we know 2 procs exist but not where. s3 (60m) is too old.
        mains = bb.select_live_mains(self.cand, 2, None, self.now)
        self.assertEqual(self.keys(mains), ["s1", "s2"])

    def test_blind_uses_short_window(self):
        # couldn't read processes at all -> recency window only (CONTEXT_ACTIVE_MIN)
        mains = bb.select_live_mains(self.cand, None, None, self.now)
        self.assertEqual(self.keys(mains), ["s1", "s2"])  # s3 outside the window

    def test_blind_drops_stale(self):
        old = [("z", _sv(self.now - 9999))]
        self.assertEqual(bb.select_live_mains(old, None, None, self.now), [])


class TestLimitView(unittest.TestCase):
    """The two-state model behind the live limits: real numbers while the window is
    open, an estimated fresh window once its reset has passed."""

    def setUp(self):
        self.now = 1_000_000
        self.window = 5 * 3600

    def test_live_window_uses_real_numbers(self):
        d = {"used_percentage": 42, "resets_at": self.now + 1800}  # resets in 30m
        v = bb.limit_view(d, self.now, self.window)
        self.assertFalse(v["estimated"])
        self.assertEqual(v["pc"], 42)
        self.assertEqual(v["remaining"], 1800)
        self.assertEqual(v["reset"], self.now + 1800)

    def test_past_reset_is_estimated_fresh(self):
        d = {"used_percentage": 88, "resets_at": self.now - 60}  # reset a minute ago
        v = bb.limit_view(d, self.now, self.window)
        self.assertTrue(v["estimated"])
        self.assertEqual(v["pc"], 0)                 # fresh, not the stale 88
        self.assertEqual(v["remaining"], self.window)  # full block, clock not started
        self.assertIsNone(v["reset"])                # no fixed reset time yet

    def test_exactly_at_reset_counts_as_fresh(self):
        d = {"used_percentage": 50, "resets_at": self.now}
        self.assertTrue(bb.limit_view(d, self.now, self.window)["estimated"])

    def test_no_reset_time(self):
        v = bb.limit_view({"used_percentage": 30}, self.now, self.window)
        self.assertFalse(v["estimated"])
        self.assertEqual(v["pc"], 30)
        self.assertIsNone(v["remaining"])

    def test_missing_percentage_defaults_zero(self):
        v = bb.limit_view({"resets_at": self.now + 100}, self.now, self.window)
        self.assertEqual(v["pc"], 0)


if __name__ == "__main__":
    unittest.main()
