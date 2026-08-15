"""Model catalog registry — declarative providers/models/routes from configs/models.json.

TS multi-cloud catalog (Phase A of multi-cloud-model-catalog-design-20260815.md).

Three sections, aligned with agent_go's model-entity 3-layer design:
  - providers : ③ deployment topology (endpoints / key locations / concurrency)
  - models    : ① model-inherent facts (price, capabilities, request quirks)
  - routes    : alias → cloud-model binding strategy (proxy routing input)

Design invariants:
  - stdlib only, no imports from other project modules (bottom of the import
    graph; proxy_state calls into this module).
  - When the catalog file is missing, an equivalent catalog is SYNTHESIZED from
    the legacy env-driven defaults so proxy behavior is identical to the
    pre-registry hardcode (zero-diff deployment).
  - A present-but-invalid catalog is REJECTED on hot swap: the previously
    loaded catalog keeps serving and the error is recorded (fail-safe, not
    fail-open).
  - "$default" / "$env" indirection:
      routes.<alias>.cloud_model == "$default"  -> defaults.cloud_model
      defaults.cloud_model       == "$env"      -> live PROXY_CLOUD_MODEL
        (resolved via env_cloud_model_getter at build/reload time, fixing the
         legacy import-time capture staleness)
  - cloud_model may be a string (primary) or a non-empty list (primary +
    fallback chain, Phase B consumption).

Called by: proxy_state.py (startup), reload_config.py (SIGHUP).
"""
import copy
import hashlib
import json
import os
import threading

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CATALOG_PATH_ENV = "PROXY_MODELS_CATALOG"
_DEFAULT_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "configs", "models.json"
)

VALID_BIAS = ("auto", "prefer_cloud", "prefer_local")
VALID_BEHAVIOR = ("prefer", "force", "force_fallback")

# Legacy alias surface (order is user-visible via /v1/models — keep stable).
# claude-opus-4-7 must always be exposed so existing sessions never 404.
_LEGACY_ALIASES = [
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "default",
    "claude-3-5-sonnet-20241022",
    "claude-3-opus-20240229",
    "claude-3-5-haiku-20241022",
    "claude-opus-4-7",
]

# Legacy MODEL_ROUTE_PREFERENCES hardcode (proxy_state.py pre-Phase-A) — used
# verbatim as the synthesized routes so no-catalog deployments are identical.
_SYNTH_ROUTES = {
    "claude-sonnet-4-6": {
        "route_bias": "auto",
        "threshold_factor": 1.0,
        "memory_bias": 0,
        "cloud_model": "$default",
        "behavior": "prefer",
    },
    "claude-opus-4-7": {
        "route_bias": "prefer_cloud",
        "threshold_factor": 0.8,
        "memory_bias": -5,
        "cloud_model": "deepseek-v4-pro",
        "behavior": "force_fallback",
    },
    "claude-haiku-4-5": {
        "route_bias": "prefer_local",
        "threshold_factor": 1.33,
        "memory_bias": 0,
        "cloud_model": "$default",
        "behavior": "prefer",
    },
}

_SYNTH_MODEL_NAME = "deepseek-v4-pro"  # opus route pins this; always in synthesis


# ---------------------------------------------------------------------------
# Module state (immutable snapshot swapped under _lock)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state = None  # {"catalog", "from_file", "error", "path", "params"}


def _default_env_cloud_model():
    return os.environ.get("PROXY_CLOUD_MODEL", "deepseek-v4-flash")


def _synthesize(env_cloud_model="", cloud_base_url="", cloud_concurrent=2):
    """Build the legacy-equivalent catalog from env-driven defaults."""
    env_cloud_model = env_cloud_model or _default_env_cloud_model()
    cloud_base_url = cloud_base_url or os.environ.get(
        "PROXY_CLOUD_BASE_URL", "https://api.deepseek.com/v1"
    )
    models = {
        _SYNTH_MODEL_NAME: {"provider": "cloud", "tier": "flagship"},
        "local-default": {"provider": "local", "tier": "standard"},
    }
    models.setdefault(env_cloud_model, {"provider": "cloud", "tier": "fast"})
    return {
        "providers": {
            "cloud": {
                "base_url": cloud_base_url,
                "key_env": "PROXY_CLOUD_API_KEY",
                "concurrent": int(cloud_concurrent or 2),
                "anthropic_base_url": "https://api.deepseek.com/anthropic",
                "anthropic_compatible": True,
            },
            "local": {
                "base_url_env": "LLAMA_BASE_URL",
                "key_env": "LLAMA_API_KEY",
                "concurrent_env": "PROXY_MAX_CONCURRENT",
                "anthropic_compatible": False,
            },
        },
        "models": models,
        "routes": copy.deepcopy(_SYNTH_ROUTES),
        "defaults": {"cloud_model": "$env"},
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate(catalog):
    """Return a list of human-readable validation errors ([] == valid)."""
    errors = []
    if not isinstance(catalog, dict):
        return ["catalog root must be a JSON object"]

    providers = catalog.get("providers")
    models = catalog.get("models")
    routes = catalog.get("routes", {})
    defaults = catalog.get("defaults", {})

    if not isinstance(providers, dict) or not providers:
        errors.append("providers must be a non-empty object")
        providers = {}
    if not isinstance(models, dict) or not models:
        errors.append("models must be a non-empty object")
        models = {}
    if not isinstance(routes, dict):
        errors.append("routes must be an object")
        routes = {}
    if not isinstance(defaults, dict):
        errors.append("defaults must be an object")
        defaults = {}

    needs_default_ref = []

    for pname, p in providers.items():
        if not isinstance(p, dict):
            errors.append("provider %r must be an object" % pname)
            continue
        has_url = isinstance(p.get("base_url"), str) and p["base_url"]
        has_url_env = isinstance(p.get("base_url_env"), str) and p["base_url_env"]
        if not has_url and not has_url_env:
            errors.append("provider %r needs base_url or base_url_env" % pname)
        if not (isinstance(p.get("key_env"), str) and p["key_env"]):
            errors.append("provider %r needs key_env" % pname)
        if "concurrent" in p and not (_is_num(p["concurrent"]) and p["concurrent"] > 0):
            errors.append("provider %r concurrent must be a positive number" % pname)
        if "concurrent_env" in p and not (
            isinstance(p["concurrent_env"], str) and p["concurrent_env"]
        ):
            errors.append("provider %r concurrent_env must be a name" % pname)
        if "anthropic_compatible" in p and not isinstance(p["anthropic_compatible"], bool):
            errors.append("provider %r anthropic_compatible must be bool" % pname)

    for mname, m in models.items():
        if not isinstance(mname, str) or not mname:
            errors.append("model name must be a non-empty string")
            continue
        if mname.startswith("claude-"):
            # Alias namespace (routes keys) vs model namespace must stay disjoint.
            errors.append(
                "model %r must not start with 'claude-' (alias namespace)" % mname
            )
        if not isinstance(m, dict):
            errors.append("model %r must be an object" % mname)
            continue
        if m.get("provider") not in providers:
            errors.append("model %r references unknown provider %r" % (mname, m.get("provider")))
        price = m.get("price")
        if price is not None:
            if not isinstance(price, dict):
                errors.append("model %r price must be an object" % mname)
            else:
                for k in ("input", "output"):
                    v = price.get(k, 0)
                    if not (_is_num(v) and v >= 0):
                        errors.append("model %r price.%s must be >= 0" % (mname, k))
        for opt in ("capabilities", "request_quirks"):
            if opt in m and not isinstance(m[opt], dict):
                errors.append("model %r %s must be an object" % (mname, opt))

    def _check_model_ref(where, ref):
        if ref == "$default":
            needs_default_ref.append(True)
        elif ref != "$env" and ref not in models:
            errors.append("%s references unknown model %r" % (where, ref))

    for alias, r in routes.items():
        if not isinstance(r, dict):
            errors.append("route %r must be an object" % alias)
            continue
        if r.get("route_bias", "auto") not in VALID_BIAS:
            errors.append("route %r route_bias must be one of %s" % (alias, VALID_BIAS))
        if r.get("behavior", "prefer") not in VALID_BEHAVIOR:
            errors.append("route %r behavior must be one of %s" % (alias, VALID_BEHAVIOR))
        if "threshold_factor" in r and not _is_num(r["threshold_factor"]):
            errors.append("route %r threshold_factor must be a number" % alias)
        if "memory_bias" in r and not _is_num(r["memory_bias"]):
            errors.append("route %r memory_bias must be a number" % alias)
        cm = r.get("cloud_model")
        if isinstance(cm, str):
            if not cm:
                errors.append("route %r cloud_model must not be empty" % alias)
            else:
                _check_model_ref("route %r" % alias, cm)
        elif isinstance(cm, list) and cm:
            for ref in cm:
                if not isinstance(ref, str) or not ref:
                    errors.append("route %r fallback chain entries must be strings" % alias)
                else:
                    _check_model_ref("route %r" % alias, ref)
        else:
            errors.append("route %r needs cloud_model (string or non-empty list)" % alias)

    dm = defaults.get("cloud_model")
    if dm is None:
        if needs_default_ref:
            errors.append("routes use $default but defaults.cloud_model is missing")
    elif dm != "$env" and dm not in models:
        errors.append("defaults.cloud_model %r is not a known model" % dm)

    budget = defaults.get("daily_budget")
    if budget is not None and not (_is_num(budget) and budget >= 0):
        errors.append("defaults.daily_budget must be >= 0")
    ppb = defaults.get("per_provider_budget")
    if ppb is not None:
        if not isinstance(ppb, dict):
            errors.append("defaults.per_provider_budget must be an object")
        else:
            for k, v in ppb.items():
                if k not in providers:
                    errors.append("per_provider_budget %r is not a known provider" % k)
                elif not (_is_num(v) and v >= 0):
                    errors.append("per_provider_budget %r must be >= 0" % k)

    return errors


# ---------------------------------------------------------------------------
# Load / reload
# ---------------------------------------------------------------------------

def load(path=None, env_cloud_model_getter=None, cloud_base_url="", cloud_concurrent=2):
    """Load the catalog. Returns True when loaded from file, False when synthesized.

    On a present-but-invalid catalog: if a previous good state exists it is kept
    (hot-swap rejection, error recorded); otherwise the synthesized fallback is
    installed. Never raises for bad content.
    """
    global _state
    path = path or os.environ.get(CATALOG_PATH_ENV) or _DEFAULT_CATALOG_PATH
    params = {
        "env_cloud_model_getter": env_cloud_model_getter,
        "cloud_base_url": cloud_base_url,
        "cloud_concurrent": cloud_concurrent,
    }

    def _synth_state(error):
        getter = env_cloud_model_getter or _default_env_cloud_model
        try:
            env_model = getter() if callable(getter) else ""
        except Exception:
            env_model = ""
        return {
            "catalog": _synthesize(env_model, cloud_base_url, cloud_concurrent),
            "from_file": False,
            "error": error,
            "path": path,
            "params": params,
        }

    new_state = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        errs = _validate(raw)
        if errs:
            raise ValueError("; ".join(errs))
        new_state = {"catalog": raw, "from_file": True, "error": None,
                     "path": path, "params": params}
    except FileNotFoundError:
        new_state = _synth_state(None)  # normal no-catalog deployment
    except (OSError, ValueError) as e:
        msg = "catalog %s rejected: %s" % (path, e)
        with _lock:
            if _state is None:
                new_state = _synth_state(msg)
            else:
                # Hot-swap rejection: keep serving the previous catalog.
                _state = dict(_state, error=msg)
                return False

    with _lock:
        _state = new_state
    return new_state["from_file"]


def reload(path=None, env_cloud_model_getter=None, cloud_base_url=None,
           cloud_concurrent=None):
    """Re-run load() reusing stored path/params for omitted arguments."""
    with _lock:
        params = (_state or {}).get("params", {})
        prev_path = (_state or {}).get("path")
    return load(
        path=path or prev_path,
        env_cloud_model_getter=(
            env_cloud_model_getter
            if env_cloud_model_getter is not None
            else params.get("env_cloud_model_getter")
        ),
        cloud_base_url=cloud_base_url if cloud_base_url is not None
        else (params.get("cloud_base_url") or ""),
        cloud_concurrent=cloud_concurrent if cloud_concurrent is not None
        else params.get("cloud_concurrent", 2),
    )


def _reset():
    """Test helper: drop all state so the next load() starts clean."""
    global _state
    with _lock:
        _state = None


# ---------------------------------------------------------------------------
# Accessors (all return deep copies; None / [] when unknown)
# ---------------------------------------------------------------------------

def _catalog():
    if _state is None:
        load()
    return _state["catalog"]


def is_loaded_from_file():
    if _state is None:
        load()
    return _state["from_file"]


def last_error():
    if _state is None:
        load()
    return _state["error"]


def catalog_path():
    if _state is None:
        load()
    return _state["path"]


def catalog_hash():
    """Stable content hash (sha256, hex, 16 chars) for drift detection (R9)."""
    return hashlib.sha256(
        json.dumps(_catalog(), sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def get_model(name):
    m = _catalog()["models"].get(name)
    return copy.deepcopy(m) if m else None


def list_models():
    return sorted(_catalog()["models"].keys())


def get_provider(name):
    p = _catalog()["providers"].get(name)
    return copy.deepcopy(p) if p else None


def get_provider_for_model(model_name):
    """Resolved provider dict for a model (raw fields, no env expansion)."""
    m = _catalog()["models"].get(model_name)
    if not m:
        return None
    return get_provider(m.get("provider", ""))


def get_provider_credentials(provider_name, env_lookup=None):
    """Resolve a provider to concrete (base_url, api_key, concurrent).

    env_lookup(key, default) defaults to os.environ.get; callers that track
    hot-reloaded values on a module (proxy_state) pass their own lookup.
    """
    p = get_provider(provider_name)
    if p is None:
        return None
    env_get = env_lookup or (lambda k, d=None: os.environ.get(k, d))

    if p.get("base_url"):
        base_url = p["base_url"]
    else:
        base_url = env_get(p.get("base_url_env", ""), "") or ""

    if "concurrent" in p:
        concurrent = int(p["concurrent"])
    else:
        try:
            concurrent = int(env_get(p.get("concurrent_env", ""), "1"))
        except (TypeError, ValueError):
            concurrent = 1

    api_key = env_get(p.get("key_env", ""), "") or ""
    return {
        "name": provider_name,
        "base_url": base_url,
        "api_key": api_key,
        "concurrent": concurrent,
        "key_env": p.get("key_env", ""),
        "anthropic_base_url": p.get("anthropic_base_url", ""),
        "anthropic_compatible": bool(p.get("anthropic_compatible", False)),
    }


def _resolve_cloud_model(value, getter):
    if value == "$default":
        value = _catalog().get("defaults", {}).get("cloud_model", "")
    if value == "$env":
        try:
            return getter() if callable(getter) else ""
        except Exception:
            return ""
    return value


def _getter():
    with _lock:
        g = (_state or {}).get("params", {}).get("env_cloud_model_getter")
    return g


def get_route(alias):
    """Preference dict isomorphic to the legacy MODEL_ROUTE_PREFERENCES entry.

    Returns {route_bias, threshold_factor, memory_bias, cloud_model, behavior}
    (+ fallback_models when the catalog declares a chain). None when unknown.
    """
    r = _catalog()["routes"].get(alias)
    if not r:
        return None
    cm = r.get("cloud_model")
    chain = cm if isinstance(cm, list) else [cm]
    primary = _resolve_cloud_model(chain[0], _getter())
    out = {
        "route_bias": r.get("route_bias", "auto"),
        "threshold_factor": r.get("threshold_factor", 1.0),
        "memory_bias": r.get("memory_bias", 0),
        "cloud_model": primary,
        "behavior": r.get("behavior", "prefer"),
    }
    fallbacks = [_resolve_cloud_model(c, _getter()) for c in chain[1:]]
    fallbacks = [f for f in fallbacks if f]
    if fallbacks:
        out["fallback_models"] = fallbacks
    return out


def build_route_preferences():
    """Full alias → preference mapping (drop-in MODEL_ROUTE_PREFERENCES)."""
    return {alias: get_route(alias) for alias in _catalog()["routes"]}


def get_alias_list():
    """Stable agent-facing aliases: legacy surface + extra catalog route keys."""
    aliases = list(_LEGACY_ALIASES)
    for key in _catalog()["routes"]:
        if key not in aliases:
            aliases.append(key)
    return aliases
