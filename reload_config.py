"""SIGHUP hot-reload: re-read active.conf and update all config."""
import sys
import threading

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

        aliases = ["claude-3-5-sonnet-20241022", "claude-3-opus-20240229",
                   "claude-3-5-haiku-20241022", "claude-sonnet-4-6",
                   "claude-haiku-4-5", "claude-opus-4-7", "default", model]
        proxy_state.MODEL_ALIASES = aliases
        setattr(target_module, "MODEL_ALIASES", aliases)

        for env_key, py_name, cast, cloud_def, local_def in target_module._RELOAD_SPEC if hasattr(target_module, "_RELOAD_SPEC") else proxy_state._RELOAD_SPEC:
            default = cloud_def if is_cloud else local_def
            raw = env.get(env_key, default)
            val = proxy_state._cast_config_value(raw, cast)
            setattr(proxy_state, py_name, val)
            setattr(target_module, py_name, val)

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
