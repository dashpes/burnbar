"""Unit tests for burnbar's pure logic.

Deliberately small and dependency-free (stdlib unittest): they cover the bits
that have actually bitten us — version parsing (broke the release), the live-
session tier selection (showed closed sessions as live) — plus the token/format
helpers and multi-provider detection. Run with:  python3 -m unittest discover -s tests
"""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

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

    def test_plugin_version_comes_from_the_header(self):
        """VERSION must be the parsed <bitbar.version>, never the "0.0.0" fallback —
        that fallback would make every install look older than the latest release and
        nag forever. Asserting the invariant, not a literal, so a release bump is a
        one-line change to the header alone (which CI already gates)."""
        self.assertRegex(bb.VERSION, r"^\d+\.\d+\.\d+$")
        self.assertNotEqual(bb.VERSION, "0.0.0")
        with open(bb.SELF, encoding="utf-8") as f:
            self.assertEqual(bb.VERSION, bb.parse_version_header(f.read(2048)))


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


class TestProviders(unittest.TestCase):
    def test_active_providers_modes(self):
        self.assertEqual(bb.active_providers({"providers": "claude"}),
                         {"claude": True, "cursor": False})
        self.assertEqual(bb.active_providers({"providers": "cursor"}),
                         {"claude": False, "cursor": True})
        self.assertEqual(bb.active_providers({"providers": "both"}),
                         {"claude": True, "cursor": True})

    def test_auto_uses_detect(self):
        with mock.patch.object(bb, "detect_claude", return_value=True), \
             mock.patch.object(bb, "detect_cursor", return_value=False):
            self.assertEqual(bb.active_providers({"providers": "auto"}),
                             {"claude": True, "cursor": False})

    def test_cursor_project_slug(self):
        self.assertEqual(bb._cursor_project_slug("Users-me-Dev-burnbar"), "burnbar")


class TestGatherCursor(unittest.TestCase):
    def test_gather_from_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, "projects", "Users-me-Dev-app")
            sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            txdir = os.path.join(proj, "agent-transcripts", sid)
            os.makedirs(txdir)
            tx = os.path.join(txdir, f"{sid}.jsonl")
            with open(tx, "w") as f:
                f.write(json.dumps({"role": "user", "message": {"content": []}}) + "\n")
                f.write(json.dumps({"role": "assistant",
                                    "message": {"content": []}}) + "\n")
            chats = os.path.join(tmp, "chats", "hash", sid)
            os.makedirs(chats)
            with open(os.path.join(chats, "meta.json"), "w") as f:
                json.dump({"title": "Fixture Chat", "cwd": "/tmp/app"}, f)
            live = os.path.join(tmp, "live.json")
            with open(live, "w") as f:
                json.dump({
                    "captured_at": 1_000_000,
                    "session_id": sid,
                    "session_name": "Fixture Chat",
                    "model": "Auto",
                    "context_window": {"used_percentage": 42,
                                       "context_window_size": 200000},
                }, f)

            old_proj = bb.CURSOR_PROJECTS
            old_chats = bb.CURSOR_CHATS
            old_live = bb.CURSOR_LIVE_PATH
            bb.CURSOR_PROJECTS = os.path.join(tmp, "projects")
            bb.CURSOR_CHATS = os.path.join(tmp, "chats")
            bb.CURSOR_LIVE_PATH = live
            try:
                data = bb.gather_cursor(datetime.now(timezone.utc), timezone.utc)
            finally:
                bb.CURSOR_PROJECTS = old_proj
                bb.CURSOR_CHATS = old_chats
                bb.CURSOR_LIVE_PATH = old_live

            self.assertEqual(data["n_sessions"], 1)
            self.assertEqual(data["sessions"][0]["title"], "Fixture Chat")
            self.assertEqual(data["sessions"][0]["turns"], 2)
            self.assertEqual(data["live"]["context_window"]["used_percentage"], 42)


class TestContextRisk(unittest.TestCase):
    def test_fresh_cursor_sessions_sorts_hottest_first(self):
        now = 1_000_000
        smap = {
            "a": {"captured_at": now - 10, "session_name": "cool",
                  "context_window": {"used_percentage": 20}},
            "b": {"captured_at": now - 5, "session_name": "hot",
                  "context_window": {"used_percentage": 88}},
            "c": {"captured_at": now - 9999, "session_name": "stale",
                  "context_window": {"used_percentage": 99}},
        }
        rows = bb.fresh_cursor_sessions(smap, now, stale_min=30)
        self.assertEqual([r[0] for r in rows], ["b", "a"])
        self.assertEqual(rows[0][2], 88)

    def test_collect_context_risks(self):
        claude = [("Sess A", 72.0, 144_000, 200_000, {}),
                  ("Sess B", 40.0, 8_000, 200_000, {})]
        cursor = [("c1", {"session_name": "Cursor Hot",
                          "context_window": {"used_percentage": 90,
                                             "context_window_size": 200_000}}, 90.0)]
        risks = bb.collect_context_risks(claude, cursor, warn=70)
        self.assertEqual(len(risks), 2)
        # Both rot on 200K bands; 180K beats 144K. Sess B (8K, 40%) isn't at risk.
        self.assertEqual(risks[0][0], "Cursor")
        self.assertEqual(risks[0][2], 90)
        self.assertEqual(risks[1][0], "Claude")

    def test_risk_ranks_by_tier_then_tokens(self):
        """A smaller session judged against a smaller window can be further gone:
        150K/200K is rot, 300K/1M only degraded, so the 150K one leads despite
        carrying half the tokens."""
        claude = [("Opus 1M", 30.0, 300_000, 1_000_000, {}),
                  ("Small", 75.0, 150_000, 200_000, {})]
        risks = bb.collect_context_risks(claude, [], warn=70)
        self.assertEqual([r[1] for r in risks], ["Small", "Opus 1M"])
        self.assertEqual(risks[0][4], 3)
        self.assertEqual(risks[1][4], 2)

    def test_low_percentage_high_tokens_is_flagged(self):
        """A 1M session at 30% would never trip a %-based threshold, but 300K is
        past the 1M-class 'degraded' floor, so it still surfaces."""
        risks = bb.collect_context_risks(
            [("Roomy", 30.0, 300_000, 1_000_000, {})], [], warn=70)
        self.assertEqual(len(risks), 1)
        self.assertIn("degraded", risks[0][5])


class TestUnifiedAgentRows(unittest.TestCase):
    """The merged LIVE AGENTS list — one list for every provider, replacing the
    three places context used to be printed."""

    NOW = 1_000_000

    def rows(self, mains=(), cursor=()):
        return bb.unified_agent_rows(list(mains), dict(bb.DEFAULTS), list(cursor),
                                     self.NOW)

    def cursor_entry(self, name, pct, size=256_000, age=60):
        return (name, {"session_name": name, "captured_at": self.NOW - age,
                       "context_window": {"used_percentage": pct,
                                          "context_window_size": size}}, float(pct))

    def test_providers_are_merged_and_tagged(self):
        rows = self.rows(
            mains=[("s1", {"model": "claude-opus-5", "last_ctx": 300_000,
                           "peak_ctx": 300_000, "mtime": self.NOW, "title": "C"})],
            cursor=[self.cursor_entry("X", 65)])
        self.assertEqual({r["prov"] for r in rows}, {"claude", "cursor"})
        # Every row must resolve to an icon — that's the only provider cue, since
        # colour is already carrying the rot band.
        for r in rows:
            self.assertIn(r["prov"], bb.AGENT_ICON)

    def test_ranked_by_rot_tier_then_tokens(self):
        """Same ordering the old risk strip used: how degraded beats how big. The
        Cursor session carries fewer tokens but is judged against a smaller window,
        so it outranks the roomy 1M Claude session."""
        rows = self.rows(
            mains=[("s1", {"model": "claude-opus-5", "last_ctx": 300_000,
                           "peak_ctx": 300_000, "mtime": self.NOW, "title": "Roomy"})],
            cursor=[self.cursor_entry("Hot", 90, size=200_000)])
        self.assertEqual([r["label"] for r in rows], ["Hot", "Roomy"])
        self.assertEqual(rows[0]["tier"], 3)      # 180K on 200K bands -> rot
        self.assertEqual(rows[1]["tier"], 2)      # 300K on 1M bands   -> degraded

    def test_at_risk_flags_both_independent_signals(self):
        """A session is at risk for either reason, and they don't have to agree:
        quality decay (tokens past the band) or imminent compaction (% of window)."""
        rows = self.rows(mains=[
            ("deep", {"model": "claude-opus-5", "last_ctx": 300_000,
                      "peak_ctx": 300_000, "mtime": self.NOW, "title": "deep"}),
            ("full", {"model": "claude-sonnet-5", "last_ctx": 150_000,
                      "peak_ctx": 150_000, "mtime": self.NOW, "title": "full"}),
            ("calm", {"model": "claude-sonnet-5", "last_ctx": 5_000,
                      "peak_ctx": 5_000, "mtime": self.NOW, "title": "calm"}),
        ])
        flagged = {r["label"]: r["at_risk"] for r in rows}
        self.assertTrue(flagged["deep"])   # 30% of 1M, but degraded on tokens
        self.assertTrue(flagged["full"])   # 75% of 200K, compaction near
        self.assertFalse(flagged["calm"])

    def test_cursor_rows_capped(self):
        rows = self.rows(cursor=[self.cursor_entry(f"s{i}", 50)
                                 for i in range(bb.CONTEXT_MAX_ROWS + 4)])
        self.assertEqual(len(rows), bb.CONTEXT_MAX_ROWS)

    def test_missing_cursor_window_size_does_not_crash(self):
        """Cursor omits context_window_size on some builds; the row must still
        render (no window label, banded against the default floors)."""
        rows = self.rows(cursor=[("s", {"session_name": "s",
                                        "captured_at": self.NOW,
                                        "context_window": {"used_percentage": 80}},
                                  80.0)])
        self.assertIsNone(rows[0]["win"])
        self.assertTrue(rows[0]["at_risk"])


class TestClaudeLiveAgents(unittest.TestCase):
    def test_subagents_attach_to_parent_and_orphans_go_to_none(self):
        now = datetime.now(timezone.utc)
        ts = now.timestamp()
        by_session = {
            "main1": {"last_ctx": 1000, "mtime": ts, "cwd": "/w", "agent": False,
                      "sid": "main1"},
            "agent-a": {"last_ctx": 500, "mtime": ts, "cwd": "/w", "agent": True,
                        "sid": "main1", "agent_id": "aaaa"},
            # Parent isn't among the live mains -> orphan bucket.
            "agent-b": {"last_ctx": 500, "mtime": ts, "cwd": "/w", "agent": True,
                        "sid": "ghost", "agent_id": "bbbb"},
        }
        with mock.patch.object(bb, "live_session_cwds", return_value=(1, {"/w": 1})):
            mains, by_parent = bb.claude_live_agents(by_session, now,
                                                     dict(bb.DEFAULTS))
        self.assertEqual([k for k, _ in mains], ["main1"])
        self.assertEqual([k for k, _ in by_parent["main1"]], ["agent-a"])
        self.assertEqual([k for k, _ in by_parent[None]], ["agent-b"])

    def test_no_sessions_short_circuits_before_shelling_out(self):
        """The process probe is the expensive call; it must not run when there's
        nothing that could be displayed."""
        with mock.patch.object(bb, "live_session_cwds") as probe:
            self.assertEqual(bb.claude_live_agents({}, datetime.now(timezone.utc),
                                                   dict(bb.DEFAULTS)), ([], {}))
            probe.assert_not_called()


class TestMenuRendering(unittest.TestCase):
    """The redesign is about output shape, so assert on the emitted menu itself."""

    NOW = 1_000_000

    @staticmethod
    def render(fn, *a, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*a, **kw)
        return buf.getvalue()

    def test_agents_carry_a_provider_icon_and_advice_line(self):
        cfg = dict(bb.DEFAULTS)
        mains = [("s1", {"model": "claude-sonnet-5", "last_ctx": 150_000,
                         "peak_ctx": 150_000, "mtime": self.NOW, "title": "Deep"})]
        cursor = [("c1", {"session_name": "Hot", "captured_at": self.NOW,
                          "context_window": {"used_percentage": 20,
                                             "context_window_size": 256_000}}, 20.0)]
        rows = bb.unified_agent_rows(mains, cfg, cursor, self.NOW)
        out = self.render(bb.emit_agents, rows, {}, self.NOW, cfg)
        self.assertIn(f"sfimage={bb.AGENT_ICON['claude']}", out)
        self.assertIn(f"sfimage={bb.AGENT_ICON['cursor']}", out)
        self.assertIn("LIVE AGENTS · 2", out)
        # 150K on 200K bands is rot -> the one actionable line shows up.
        self.assertIn("/compact", out)

    def test_no_advice_line_when_everything_is_healthy(self):
        cfg = dict(bb.DEFAULTS)
        mains = [("s1", {"model": "claude-sonnet-5", "last_ctx": 5_000,
                         "peak_ctx": 5_000, "mtime": self.NOW, "title": "Calm"})]
        rows = bb.unified_agent_rows(mains, cfg, [], self.NOW)
        out = self.render(bb.emit_agents, rows, {}, self.NOW, cfg)
        self.assertIn("LIVE AGENTS · 1", out)
        self.assertNotIn("/compact", out)
        self.assertNotIn("exclamationmark", out)

    def test_subagents_stay_in_the_main_menu(self):
        """They must render as sibling rows, not a submenu of the parent: a subagent
        burning context is the last thing that should need a hover to discover."""
        cfg = dict(bb.DEFAULTS)
        mains = [("p", {"model": "claude-opus-5", "last_ctx": 1_000, "peak_ctx": 1_000,
                        "mtime": self.NOW, "title": "Parent"})]
        kids = {"p": [("a", {"model": "claude-haiku-4-5", "last_ctx": 2_000,
                             "peak_ctx": 2_000, "mtime": self.NOW,
                             "agent_id": "aaaa"})]}
        rows = bb.unified_agent_rows(mains, cfg, [], self.NOW)
        out = self.render(bb.emit_agents, rows, kids, self.NOW, cfg)
        kid_line = [ln for ln in out.splitlines() if "haiku" in ln][0]
        self.assertFalse(kid_line.startswith("--"))
        self.assertIn(f"sfimage={bb.SUBAGENT_ICON}", kid_line)

    def test_stats_submenu_is_absent_on_a_fresh_install(self):
        """cdata is a fully-populated dict of zeroes before you have run anything;
        the Stats row must not appear and lead to empty headers."""
        empty_cursor = {"sessions": [], "today_turns": 0, "today_sessions": 0,
                        "live": None, "session_map": {}, "n_sessions": 0}
        out = self.render(bb.stats_submenu, None, None, empty_cursor,
                          datetime.now(timezone.utc).date(), timezone.utc, self.NOW)
        self.assertEqual(out, "")


class TestContextBands(unittest.TestCase):
    def test_bands_200k_class(self):
        for tokens, tier, label in ((0, 0, "sharp"), (31_999, 0, "sharp"),
                                    (32_000, 1, "drifting"), (60_000, 2, "degraded"),
                                    (128_000, 3, "rot"), (190_000, 3, "rot")):
            self.assertEqual(bb.ctx_band(tokens, 200_000), (tier, label), tokens)

    def test_bands_scale_with_window(self):
        """The 1M correction: a model built for 1M isn't judged by 200K's ruler."""
        self.assertEqual(bb.ctx_band(33_000, 1_000_000), (0, "sharp"))
        self.assertEqual(bb.ctx_band(33_000, 200_000), (1, "drifting"))
        self.assertEqual(bb.ctx_band(150_000, 1_000_000), (1, "drifting"))
        self.assertEqual(bb.ctx_band(150_000, 200_000), (3, "rot"))
        # ...but a 1M window is still not 1M of usable context.
        self.assertEqual(bb.ctx_band(450_000, 1_000_000), (3, "rot"))

    def test_unknown_window_falls_back_to_strictest(self):
        self.assertEqual(bb.ctx_band(70_000, None), (2, "degraded"))
        self.assertEqual(bb.ctx_band(70_000, 0), (2, "degraded"))

    def test_tags_carry_both_signals(self):
        tier, tags = bb.ctx_tags(150_000, 95.0, 200_000)
        self.assertEqual(tier, 3)
        self.assertEqual(tags, ["rot", "compacting"])
        tier, tags = bb.ctx_tags(5_000, 92.0, 200_000)   # tiny context, full window
        self.assertEqual(tier, 0)
        self.assertEqual(tags, ["compacting"])
        self.assertEqual(bb.ctx_tags(1_000, 10.0, 200_000), (0, []))

    def test_cursor_ctx_tokens(self):
        e = {"context_window": {"used_percentage": 61.9, "context_window_size": 256_000}}
        self.assertEqual(bb.cursor_ctx_tokens(e), 158_464)
        self.assertIsNone(bb.cursor_ctx_tokens({"context_window": {}}))
        self.assertIsNone(bb.cursor_ctx_tokens({}))


class TestCursorSessionsBridge(unittest.TestCase):
    def test_statusline_merges_sessions(self):
        """Load the Cursor statusline module and ensure multi-session merge works."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "burnbar-cursor-statusline.py")
        spec = importlib.util.spec_from_file_location("bb_cursor_sl", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            live = os.path.join(tmp, "live.json")
            sess = os.path.join(tmp, "sessions.json")
            mod.LIVE_PATH = live
            mod.SESSIONS_PATH = sess
            import io
            import contextlib
            payload1 = json.dumps({
                "session_id": "s1", "session_name": "One",
                "model": {"display_name": "Auto"},
                "context_window": {"used_percentage": 40, "context_window_size": 200000},
            })
            payload2 = json.dumps({
                "session_id": "s2", "session_name": "Two",
                "model": {"display_name": "Auto"},
                "context_window": {"used_percentage": 80, "context_window_size": 200000},
            })
            for p in (payload1, payload2):
                with contextlib.redirect_stdout(io.StringIO()):
                    with mock.patch("sys.stdin", io.StringIO(p)):
                        mod.main()
            with open(sess) as f:
                store = json.load(f)
            self.assertIn("s1", store["sessions"])
            self.assertIn("s2", store["sessions"])
            self.assertEqual(store["sessions"]["s2"]["context_window"]["used_percentage"], 80)

    def test_loads_legacy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = os.path.join(tmp, "usage.json")
            new = os.path.join(tmp, "claude", "usage.json")
            payload = {"rate_limits": {"five_hour": {"used_percentage": 10,
                                                     "resets_at": 9_999_999}}}
            with open(legacy, "w") as f:
                json.dump(payload, f)
            old_u, old_l = bb.USAGE_PATH, bb.USAGE_PATH_LEGACY
            bb.USAGE_PATH, bb.USAGE_PATH_LEGACY = new, legacy
            try:
                u = bb.load_usage()
            finally:
                bb.USAGE_PATH, bb.USAGE_PATH_LEGACY = old_u, old_l
            self.assertIsNotNone(u)
            self.assertEqual(u["rate_limits"]["five_hour"]["used_percentage"], 10)
            self.assertTrue(os.path.exists(new))


if __name__ == "__main__":
    unittest.main()
