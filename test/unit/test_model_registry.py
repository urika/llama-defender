"""Unit tests for model_registry (Phase A model catalog).

Covers: legacy-equivalent synthesis (no catalog file), file loading,
$default/$env indirection, fallback chains, validation rules,
hot-swap rejection, provider env resolution, catalog hash, alias surface.
"""
import copy
import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import model_registry

# The pre-Phase-A hardcode of proxy_state.MODEL_ROUTE_PREFERENCES, with
# PROXY_CLOUD_MODEL="deepseek-v4-flash" — the regression anchor: synthesis
# (and the committed configs/models.json) must reproduce it exactly.
LEGACY_PREFS = {
    "claude-sonnet-4-6": {
        "route_bias": "auto", "threshold_factor": 1.0, "memory_bias": 0,
        "cloud_model": "deepseek-v4-flash", "behavior": "prefer",
    },
    "claude-opus-4-7": {
        "route_bias": "prefer_cloud", "threshold_factor": 0.8, "memory_bias": -5,
        "cloud_model": "deepseek-v4-pro", "behavior": "force_fallback",
    },
    "claude-haiku-4-5": {
        "route_bias": "prefer_local", "threshold_factor": 1.33, "memory_bias": 0,
        "cloud_model": "deepseek-v4-flash", "behavior": "prefer",
    },
}

LEGACY_ALIASES = [
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "default",
    "claude-3-5-sonnet-20241022",
    "claude-3-opus-20240229",
    "claude-3-5-haiku-20241022",
    "claude-opus-4-7",
]


def _minimal_catalog():
    """A valid minimal catalog used as the base for mutation tests."""
    return {
        "providers": {
            "p1": {"base_url": "https://p1.example/v1", "key_env": "P1_KEY", "concurrent": 2},
            "local": {"base_url_env": "LLAMA_BASE_URL", "key_env": "LLAMA_API_KEY",
                      "concurrent_env": "PROXY_MAX_CONCURRENT"},
        },
        "models": {
            "m1": {"provider": "p1", "tier": "flagship"},
            "m2": {"provider": "p1", "tier": "fast"},
            "local-default": {"provider": "local", "tier": "standard"},
        },
        "routes": {
            "claude-sonnet-4-6": {"route_bias": "auto", "cloud_model": "$default",
                                  "behavior": "prefer", "threshold_factor": 1.0,
                                  "memory_bias": 0},
        },
        "defaults": {"cloud_model": "m1"},
    }


class ModelRegistryTestBase(unittest.TestCase):
    def setUp(self):
        model_registry._reset()

    def tearDown(self):
        model_registry._reset()

    def _write_catalog(self, catalog, name="models.json"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(catalog, f)
        return path

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestSynthesis(ModelRegistryTestBase):
    """No catalog file => synthesized catalog must equal legacy behavior."""

    def test_missing_file_synthesizes(self):
        path = os.path.join(self.tmp.name, "does-not-exist.json")
        from_file = model_registry.load(
            path=path, env_cloud_model_getter=lambda: "deepseek-v4-flash"
        )
        self.assertFalse(from_file)
        self.assertFalse(model_registry.is_loaded_from_file())
        self.assertIsNone(model_registry.last_error())

    def test_synthesis_preferences_equal_legacy_hardcode(self):
        model_registry.load(
            path=os.path.join(self.tmp.name, "nope.json"),
            env_cloud_model_getter=lambda: "deepseek-v4-flash",
        )
        self.assertEqual(model_registry.build_route_preferences(), LEGACY_PREFS)

    def test_synthesis_preferences_track_env_model(self):
        holder = {"m": "deepseek-v4-flash"}
        model_registry.load(
            path=os.path.join(self.tmp.name, "nope.json"),
            env_cloud_model_getter=lambda: holder["m"],
        )
        prefs = model_registry.build_route_preferences()
        self.assertEqual(prefs["claude-sonnet-4-6"]["cloud_model"], "deepseek-v4-flash")
        # Simulate SIGHUP changing PROXY_CLOUD_MODEL then rebuilding —
        # the legacy import-time capture staleness is gone.
        holder["m"] = "glm-5.2"
        prefs = model_registry.build_route_preferences()
        self.assertEqual(prefs["claude-sonnet-4-6"]["cloud_model"], "glm-5.2")
        self.assertEqual(prefs["claude-haiku-4-5"]["cloud_model"], "glm-5.2")
        # opus pins the literal, unaffected by env.
        self.assertEqual(prefs["claude-opus-4-7"]["cloud_model"], "deepseek-v4-pro")

    def test_synthesis_alias_list_matches_legacy_order(self):
        model_registry.load(
            path=os.path.join(self.tmp.name, "nope.json"),
            env_cloud_model_getter=lambda: "deepseek-v4-flash",
        )
        self.assertEqual(model_registry.get_alias_list(), LEGACY_ALIASES)

    def test_synthesis_includes_env_model_in_models(self):
        model_registry.load(
            path=os.path.join(self.tmp.name, "nope.json"),
            env_cloud_model_getter=lambda: "glm-5.2",
        )
        models = model_registry.list_models()
        self.assertIn("glm-5.2", models)
        self.assertIn("deepseek-v4-pro", models)  # opus pin always present
        self.assertIn("local-default", models)


class TestFileLoad(ModelRegistryTestBase):
    def test_load_from_file(self):
        path = self._write_catalog(_minimal_catalog())
        self.assertTrue(model_registry.load(path=path))
        self.assertTrue(model_registry.is_loaded_from_file())
        self.assertIsNone(model_registry.last_error())
        self.assertEqual(model_registry.catalog_path(), path)

    def test_default_reference_resolution(self):
        cat = _minimal_catalog()
        path = self._write_catalog(cat)
        model_registry.load(path=path)
        route = model_registry.get_route("claude-sonnet-4-6")
        self.assertEqual(route["cloud_model"], "m1")  # $default -> defaults.cloud_model

    def test_env_reference_resolution(self):
        cat = _minimal_catalog()
        cat["defaults"]["cloud_model"] = "$env"
        path = self._write_catalog(cat)
        model_registry.load(path=path, env_cloud_model_getter=lambda: "k3")
        self.assertEqual(model_registry.get_route("claude-sonnet-4-6")["cloud_model"], "k3")

    def test_env_reference_direct_in_route(self):
        cat = _minimal_catalog()
        cat["routes"]["claude-sonnet-4-6"]["cloud_model"] = "$env"
        path = self._write_catalog(cat)
        model_registry.load(path=path, env_cloud_model_getter=lambda: "kimi-for-coding")
        self.assertEqual(
            model_registry.get_route("claude-sonnet-4-6")["cloud_model"],
            "kimi-for-coding",
        )

    def test_fallback_chain_list(self):
        cat = _minimal_catalog()
        cat["routes"]["claude-opus-4-7"] = {
            "route_bias": "prefer_cloud",
            "cloud_model": ["m1", "m2"],
            "behavior": "force_fallback",
            "threshold_factor": 0.8,
            "memory_bias": -5,
        }
        path = self._write_catalog(cat)
        model_registry.load(path=path)
        route = model_registry.get_route("claude-opus-4-7")
        self.assertEqual(route["cloud_model"], "m1")
        self.assertEqual(route["fallback_models"], ["m2"])

    def test_single_cloud_model_has_no_fallback_key(self):
        path = self._write_catalog(_minimal_catalog())
        model_registry.load(path=path)
        route = model_registry.get_route("claude-sonnet-4-6")
        self.assertNotIn("fallback_models", route)

    def test_unknown_alias_returns_none(self):
        path = self._write_catalog(_minimal_catalog())
        model_registry.load(path=path)
        self.assertIsNone(model_registry.get_route("claude-opus-4-7"))

    def test_alias_list_appends_extra_route_keys(self):
        cat = _minimal_catalog()
        cat["routes"]["glm-5.2"] = {
            "route_bias": "prefer_cloud", "cloud_model": "m1", "behavior": "prefer",
        }
        path = self._write_catalog(cat)
        model_registry.load(path=path)
        aliases = model_registry.get_alias_list()
        self.assertEqual(aliases[:len(LEGACY_ALIASES)], LEGACY_ALIASES)
        self.assertEqual(aliases[-1], "glm-5.2")

    def test_reload_reuses_stored_getter(self):
        cat = _minimal_catalog()
        cat["defaults"]["cloud_model"] = "$env"
        path = self._write_catalog(cat)
        holder = {"m": "m1"}
        model_registry.load(path=path, env_cloud_model_getter=lambda: holder["m"])
        holder["m"] = "m2"
        self.assertTrue(model_registry.reload())  # same file, stored params
        self.assertEqual(model_registry.get_route("claude-sonnet-4-6")["cloud_model"], "m2")


class TestValidation(ModelRegistryTestBase):
    def _expect_reject(self, mutate):
        cat = _minimal_catalog()
        mutate(cat)
        errors = model_registry._validate(cat)
        self.assertTrue(errors, "expected validation error for %r" % mutate)

    def test_model_name_claude_prefix_rejected(self):
        self._expect_reject(
            lambda c: c["models"].update({"claude-evil": {"provider": "p1"}})
        )

    def test_unknown_provider_reference_rejected(self):
        self._expect_reject(
            lambda c: c["models"].update({"m9": {"provider": "nope"}})
        )

    def test_provider_requires_key_env(self):
        self._expect_reject(
            lambda c: c["providers"].update({"p2": {"base_url": "https://x/v1"}})
        )

    def test_provider_requires_base_url(self):
        self._expect_reject(
            lambda c: c["providers"].update({"p2": {"key_env": "K"}})
        )

    def test_invalid_bias_rejected(self):
        self._expect_reject(
            lambda c: c["routes"]["claude-sonnet-4-6"].update({"route_bias": "sideways"})
        )

    def test_invalid_behavior_rejected(self):
        self._expect_reject(
            lambda c: c["routes"]["claude-sonnet-4-6"].update({"behavior": "maybe"})
        )

    def test_unknown_cloud_model_rejected(self):
        self._expect_reject(
            lambda c: c["routes"]["claude-sonnet-4-6"].update({"cloud_model": "ghost"})
        )

    def test_default_without_defaults_cloud_model_rejected(self):
        self._expect_reject(lambda c: c.pop("defaults"))

    def test_negative_price_rejected(self):
        self._expect_reject(
            lambda c: c["models"]["m1"].update({"price": {"input": -1, "output": 2}})
        )

    def test_unknown_fallback_entry_rejected(self):
        self._expect_reject(
            lambda c: c["routes"]["claude-sonnet-4-6"].update(
                {"cloud_model": ["m1", "ghost"]})
        )

    def test_per_provider_budget_unknown_provider_rejected(self):
        self._expect_reject(
            lambda c: c["defaults"].update({"per_provider_budget": {"ghost": 1.0}})
        )


class TestHotSwapRejection(ModelRegistryTestBase):
    def test_invalid_file_keeps_previous_catalog(self):
        good = self._write_catalog(_minimal_catalog(), "good.json")
        self.assertTrue(model_registry.load(path=good))

        bad = _minimal_catalog()
        bad["models"]["m1"]["provider"] = "ghost"
        bad_path = self._write_catalog(bad, "bad.json")
        self.assertFalse(model_registry.reload(path=bad_path))

        # Previous catalog keeps serving; error recorded.
        self.assertTrue(model_registry.is_loaded_from_file())
        self.assertIn("rejected", model_registry.last_error())
        self.assertEqual(model_registry.get_route("claude-sonnet-4-6")["cloud_model"], "m1")

    def test_invalid_json_keeps_previous_catalog(self):
        good = self._write_catalog(_minimal_catalog(), "good.json")
        model_registry.load(path=good)
        bad_path = os.path.join(self.tmp.name, "broken.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertFalse(model_registry.reload(path=bad_path))
        self.assertTrue(model_registry.is_loaded_from_file())
        self.assertIsNotNone(model_registry.last_error())

    def test_first_load_invalid_synthesizes(self):
        bad = _minimal_catalog()
        bad["routes"]["claude-sonnet-4-6"]["cloud_model"] = "ghost"
        bad_path = self._write_catalog(bad, "first-bad.json")
        from_file = model_registry.load(
            path=bad_path, env_cloud_model_getter=lambda: "deepseek-v4-flash"
        )
        self.assertFalse(from_file)
        self.assertFalse(model_registry.is_loaded_from_file())
        self.assertIsNotNone(model_registry.last_error())
        # Synthesis fallback serves the legacy preferences.
        self.assertEqual(model_registry.build_route_preferences(), LEGACY_PREFS)


class TestProviderResolution(ModelRegistryTestBase):
    def test_env_resolution(self):
        cat = _minimal_catalog()
        path = self._write_catalog(cat)
        model_registry.load(path=path)
        env = {"LLAMA_BASE_URL": "http://127.0.0.1:8081/v1",
               "PROXY_MAX_CONCURRENT": "3", "LLAMA_API_KEY": "sk-local"}
        creds = model_registry.get_provider_credentials(
            "local", env_lookup=lambda k, d=None: env.get(k, d)
        )
        self.assertEqual(creds["base_url"], "http://127.0.0.1:8081/v1")
        self.assertEqual(creds["api_key"], "sk-local")
        self.assertEqual(creds["concurrent"], 3)
        self.assertFalse(creds["anthropic_compatible"])

    def test_literal_resolution(self):
        path = self._write_catalog(_minimal_catalog())
        model_registry.load(path=path)
        env = {"P1_KEY": "sk-p1"}
        creds = model_registry.get_provider_credentials(
            "p1", env_lookup=lambda k, d=None: env.get(k, d)
        )
        self.assertEqual(creds["base_url"], "https://p1.example/v1")
        self.assertEqual(creds["api_key"], "sk-p1")
        self.assertEqual(creds["concurrent"], 2)

    def test_get_provider_for_model(self):
        path = self._write_catalog(_minimal_catalog())
        model_registry.load(path=path)
        self.assertEqual(model_registry.get_provider_for_model("m1")["key_env"], "P1_KEY")
        self.assertIsNone(model_registry.get_provider_for_model("ghost"))

    def test_unknown_provider_returns_none(self):
        path = self._write_catalog(_minimal_catalog())
        model_registry.load(path=path)
        self.assertIsNone(model_registry.get_provider_credentials("ghost"))


class TestCatalogHash(ModelRegistryTestBase):
    def test_hash_stable_across_loads(self):
        path = self._write_catalog(_minimal_catalog())
        model_registry.load(path=path)
        h1 = model_registry.catalog_hash()
        model_registry.reload()
        self.assertEqual(model_registry.catalog_hash(), h1)

    def test_hash_key_order_insensitive(self):
        c1 = _minimal_catalog()
        c2 = copy.deepcopy(c1)
        # Rebuild providers dict in reverse order — same content.
        c2["providers"] = dict(reversed(list(c2["providers"].items())))
        p1 = self._write_catalog(c1, "h1.json")
        p2 = self._write_catalog(c2, "h2.json")
        model_registry.load(path=p1)
        h1 = model_registry.catalog_hash()
        model_registry.load(path=p2)
        self.assertEqual(model_registry.catalog_hash(), h1)

    def test_hash_changes_on_content_change(self):
        c1 = _minimal_catalog()
        c2 = copy.deepcopy(c1)
        c2["models"]["m1"]["tier"] = "fast"
        p1 = self._write_catalog(c1, "h1.json")
        p2 = self._write_catalog(c2, "h2.json")
        model_registry.load(path=p1)
        h1 = model_registry.catalog_hash()
        model_registry.load(path=p2)
        self.assertNotEqual(model_registry.catalog_hash(), h1)


class TestProxyStateIntegration(unittest.TestCase):
    """proxy_state derives prefs/aliases from the committed configs/models.json."""

    def test_preferences_match_legacy(self):
        if os.environ.get("PROXY_CLOUD_MODEL"):
            self.skipTest("PROXY_CLOUD_MODEL set in env; default-model anchor invalid")
        import proxy_state
        self.assertEqual(proxy_state.MODEL_ROUTE_PREFERENCES, LEGACY_PREFS)

    def test_catalog_file_actually_loaded(self):
        import proxy_state
        # The committed catalog must validate (not silently synthesize).
        self.assertTrue(proxy_state._CATALOG_FROM_FILE)
        self.assertTrue(model_registry.is_loaded_from_file())

    def test_alias_surface_unchanged(self):
        import proxy_state
        self.assertEqual(proxy_state.get_model_aliases(), LEGACY_ALIASES)

    def test_catalog_models_present(self):
        import proxy_state
        # Real provider facts from the agent_go integration inputs.
        for name in ("glm-5.2", "glm-5.3", "k3", "deepseek-v4-pro", "deepseek-v4-flash"):
            self.assertIsNotNone(
                model_registry.get_model(name), "model %r missing from catalog" % name
            )


if __name__ == "__main__":
    unittest.main()
