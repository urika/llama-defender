"""SIGHUP hot-reload: re-read active.conf and update all config."""
import sys
import threading

import model_registry
import proxy_state
from proxy_logging import log


def reload_config(signum=None, frame=None, target_module=None):
    """SIGHUP handler: re-read active.conf and update proxy_state + target_module."""
    if target_module is None:
        target_module = sys.modules[__name__]

    with target_module._RELOAD_LOCK if hasattr(target_module, "_RELOAD_LOCK") else proxy_state._RELOAD_LOCK:
        env = proxy_state._parse_conf_env(getattr(target_module, "RELOAD_CONFIG_PATH", target_module.RELOAD_CONFIG_PATH))
        secret_env = proxy_state._parse_conf_env(getattr(target_module, "RELOAD_SECRET_PATH", proxy_state.RELOAD_SECRET_PATH))
        if secret_env:
            env.update({k: v for k, v in secret_env.items() if k not in env})
        if not env:
            log("[RELOAD] no config parsed from %s" % target_module.RELOAD_CONFIG_PATH, level="WARN")
            return

        if "LLAMA_BASE_URL" in env:
            base = env["LLAMA_BASE_URL"]
        else:
            host = env.get("LLAMA_HOST", "127.0.0.1")
            port = env.get("LLAMA_PORT", "8081")
            base = "http://%s:%s/v1" % (host, port)
        proxy_state.LLAMA_BASE = base
        setattr(target_module, "LLAMA_BASE", base)
        api_key = env.get("LLAMA_API_KEY", getattr(target_module, "LLAMA_API_KEY"))
        proxy_state.LLAMA_API_KEY = api_key
        setattr(target_module, "LLAMA_API_KEY", api_key)

        bt = env.get("BACKEND_TYPE", "")
        if not bt:
            low = base.lower()
            bt = "cloud" if ("deepseek" in low or "openai" in low or "api." in low) else "local"
        proxy_state.BACKEND_TYPE = bt
        setattr(target_module, "BACKEND_TYPE", bt)
        is_cloud = bt == "cloud"
        proxy_state.IS_CLOUD = is_cloud
        setattr(target_module, "IS_CLOUD", is_cloud)

        model = env.get("MODEL_NAME") or env.get("LLAMA_MODEL", getattr(target_module, "MODEL_NAME"))
        proxy_state.MODEL_NAME = model
        setattr(target_module, "MODEL_NAME", model)

        new_max = int(env.get("PROXY_MAX_CONCURRENT", "4" if is_cloud else "1"))
        old_max = getattr(target_module, "PROXY_MAX_CONCURRENT")
        proxy_state.PROXY_MAX_CONCURRENT = new_max
        setattr(target_module, "PROXY_MAX_CONCURRENT", new_max)
        if new_max != old_max:
            proxy_state._llama_lock = threading.Semaphore(new_max)
            setattr(target_module, "_llama_lock", threading.Semaphore(new_max))
            log("[RELOAD] Semaphore rebuilt: %d -> %d" % (old_max, new_max))

        aliases = proxy_state.get_model_aliases()
        proxy_state.MODEL_ALIASES = aliases
        setattr(target_module, "MODEL_ALIASES", aliases)

        # Invalidate model aliases cache so get_model_aliases() rebuilds
        proxy_state.invalidate_model_aliases_cache()

        # Invalidate sensitive path regex cache so pattern changes take effect
        proxy_state.invalidate_sensitive_patterns_cache()

        # Rebuild cloud lock if route cloud concurrent changed
        new_cloud_cc = int(env.get("PROXY_ROUTE_CLOUD_CONCURRENT",
                           str(getattr(target_module, "PROXY_ROUTE_CLOUD_CONCURRENT", 2))))
        old_cloud_cc = getattr(target_module, "PROXY_ROUTE_CLOUD_CONCURRENT", 2)
        if new_cloud_cc != old_cloud_cc:
            proxy_state._cloud_lock = threading.Semaphore(new_cloud_cc)
            setattr(target_module, "_cloud_lock", threading.Semaphore(new_cloud_cc))
            log("[RELOAD] Cloud semaphore rebuilt: %d -> %d" % (old_cloud_cc, new_cloud_cc))

        for env_key, py_name, cast, cloud_def, local_def in target_module._RELOAD_SPEC if hasattr(target_module, "_RELOAD_SPEC") else proxy_state._RELOAD_SPEC:
            default = cloud_def if is_cloud else local_def
            raw = env.get(env_key, default)
            val = proxy_state._cast_config_value(raw, cast)
            setattr(proxy_state, py_name, val)
            setattr(target_module, py_name, val)

        # Reload the model catalog (configs/models.json) and rebuild route
        # preferences. Runs AFTER the _RELOAD_SPEC loop so "$env" references
        # resolve against the freshly applied PROXY_CLOUD_MODEL. A rejected
        # (invalid) catalog keeps the previous one serving — logged, not fatal.
        model_registry.reload()
        prefs = model_registry.build_route_preferences()
        proxy_state.MODEL_ROUTE_PREFERENCES = prefs
        setattr(target_module, "MODEL_ROUTE_PREFERENCES", prefs)

        # Apply every catalog provider key_env found in the parsed conf env
        # (e.g. ZHIPU_API_KEY / KIMI_API_KEY from secret.local.conf) so provider
        # keys rotate on SIGHUP without per-provider _RELOAD_SPEC entries.
        for _pname in model_registry.list_providers():
            _ke = (model_registry.get_provider(_pname) or {}).get("key_env", "")
            if _ke and _ke != "LLAMA_API_KEY" and _ke in env:
                setattr(proxy_state, _ke, env[_ke])
                setattr(target_module, _ke, env[_ke])

        # Rebuild per-provider semaphores (catalog `concurrent` may have changed).
        proxy_state.rebuild_provider_locks()

        err = model_registry.last_error()
        if err:
            log("[RELOAD] %s" % err, level="WARN")
        else:
            log("[RELOAD] model catalog reloaded (source=%s, hash=%s)" % (
                "file" if model_registry.is_loaded_from_file() else "synthesized",
                model_registry.catalog_hash()))

        loop_thr = int(env.get("PROXY_LOOP_THRESHOLD", getattr(target_module, "PROXY_LOOP_THRESHOLD")))
        proxy_state.PROXY_LOOP_THRESHOLD = loop_thr
        setattr(target_module, "PROXY_LOOP_THRESHOLD", loop_thr)
        proxy_state.PROXY_LOOP_LEVEL2 = int(env.get("PROXY_LOOP_LEVEL2", str(loop_thr * 2)))
        setattr(target_module, "PROXY_LOOP_LEVEL2", int(env.get("PROXY_LOOP_LEVEL2", str(loop_thr * 2))))
        proxy_state.PROXY_LOOP_LEVEL3 = int(env.get("PROXY_LOOP_LEVEL3", str(loop_thr * 3)))
        setattr(target_module, "PROXY_LOOP_LEVEL3", int(env.get("PROXY_LOOP_LEVEL3", str(loop_thr * 3))))

        sat = (env.get("PROXY_CHARS_SATURATION") or env.get("PROXY_CTX_CHARS_LIMIT", "500000" if is_cloud else "180000"))
        proxy_state.PROXY_CHARS_SATURATION = int(sat)
        setattr(target_module, "PROXY_CHARS_SATURATION", int(sat))

        oom = (env.get("PROXY_OOM_SAFE_CHARS") or env.get("PROXY_PRE_TRUNCATE_CHARS", "10000000" if is_cloud else "200000"))
        proxy_state.PROXY_OOM_SAFE_CHARS = int(oom)
        setattr(target_module, "PROXY_OOM_SAFE_CHARS", int(oom))
        proxy_state.PROXY_PRE_TRUNCATE_CHARS = int(oom)
        setattr(target_module, "PROXY_PRE_TRUNCATE_CHARS", int(oom))

        log("[RELOAD] OK: backend=%s base=%s model=%s concurrent=%d clear=%s ctx_limit=%s frozen=%d truncate=%s"
            % (bt, base[:60], model, new_max, getattr(target_module, "PROXY_CLEAR_ENABLED"),
               getattr(target_module, "PROXY_CTX_LIMIT_ENABLED"), getattr(target_module, "PROXY_FROZEN_HEAD"),
               getattr(target_module, "PROXY_CTX_TRUNCATE_STRATEGY")))
