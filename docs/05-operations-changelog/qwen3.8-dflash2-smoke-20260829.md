# Qwen3.8-27B + DFlash2 冒烟测试（2026-08-29）

> **背景**: 调研 Qwen3.8-27B 的 MTP/DFlash 方案后（见下），在本机（M5 Pro 48GB）实测 z-lab DFlash2 MLX 后端（路径 A）对 Qwen3.8-27B-4bit 的投机解码收益，并对比当前生产栈 rapid-mlx。
> **结论摘要**: DFlash2 实测 **2.0× 纯 AR**，但与当前 rapid-mlx 生产路径**持平**（36.6 vs ~39.5 tok/s），单独切换无增益。

## 一、调研要点（Qwen3.8-27B 投机解码生态）

- Qwen3.8-27B = dense 27B **hybrid**（64 层 = 16 全注意力 + 48 GatedDeltaNet），官方**内置 MTP 头**（`qwen3_5_mtp`）。
- MTP：`EigenLabs/Qwen3.8-27B-MTP-bf16`（0.85GB）/ `mlx-community/Qwen3.8-27B-MTP-4bit`；llama.cpp `--spec-type draft-mtp` +33-39%；oMLX/Ollama 实测 4-bit 26→61.6 tok/s（M5 Max 128GB 单流）。
- DFlash：`z-lab/Qwen3.8-27B-DFlash2`（DFlash2 块扩散 drafter），z-lab MLX 后端官方支持 Qwen3.8-27B，量化 target **block_size ≤ 5**。
- 本机栈现状：rapid-mlx 0.12.13 别名 `qwen3.8-27b-4bit` = `supports_spec_decode: false`（hybrid 门控，issue #1941）；本地 4bit 检查点已丢失 MTP 头（转换器丢头）。

## 二、冒烟测试搭建

| 项 | 说明 |
|---|---|
| venv | `.venv-dflash2`（Python 3.12 + z-lab/dflash `[local]`，MLX 0.32.0 + mlx-lm 0.31.3；与既有 `.venv-dflash`(bstnxbt dflash-mlx 0.1.8) 隔离） |
| drafter | `z-lab/Qwen3.8-27B-DFlash2`（3.85GB，sha256 校验通过） |
| 下载方法 | huggingface_hub 库连接失败 → **curl + 手工回填 HF 缓存**（blobs + refs/main + snapshots，同 Ornith 下载法） |
| 目标 | `mlx-community/Qwen3.8-27B-4bit`（16.1GB，已缓存；运行前停 Ornith 后端 + coder7b 释放内存） |

## 三、实测数据（M5 Pro 48GB，同一 prompt「Write a quicksort in Python.」）

| 路径 | tok/s | tokens | 耗时 | 说明 |
|---|---|---|---|---|
| 纯 AR（mlx_lm） | **17.94** | 215 | — | 基线 |
| **DFlash2**（block 5，4-bit drafter，temp 0.7） | **36.6** | 256 | 6.99s | **≈2.0× AR** |
| 当前生产 rapid-mlx | ~39.5 | — | — | 代理指标（历史值） |

计时口径：`dflash` CLI 不打印 tok/s → 用 Python 调 `stream_generate`，排除模型加载（load ≈ 4-5s），按 tokenizer 重编码统计 token 数。

## 四、结论

1. **DFlash2 在本机可行且生效**：2.0× 纯 AR，验证 z-lab MLX 后端对 Qwen3.8-27B 官方支持成立（与 24GB Mac mini 实测 1.8× 一致，低于官宣 3×，因 4-bit 量化 block_size 被 cap 到 5）。
2. **与 rapid-mlx 生产路径持平**：36.6 vs ~39.5 tok/s → **单独切路径 A 无增益**；z-lab dflash 是裸生成器（无 prefix cache/服务化），替换生产栈不划算。
3. 要超过 rapid-mlx：需 oMLX fork 的 **MTP + ANE** 组合（M3/M4 Max 实测 53-72 tok/s，但 48GB 内存门槛高，ANE 大上下文需 ~110GB wired）或等 rapid-mlx **#2195**（DFlash2 实验性支持，hybrid GDN rollback 已在 mlx-vlm 有先例）。

## 五、清理（2026-08-29）

已删除冒烟测试专用产物：
- `~/.cache/huggingface/hub/models--z-lab--Qwen3.8-27B-DFlash2/`（drafter 3.85GB）
- `.venv-dflash2/`（z-lab dflash 环境 ~2.4GB）
- `/tmp/dflash_smoke*.py`、`/tmp/dflash_smoke.log`

保留：`mlx-community/Qwen3.8-27B-4bit`（生产模型，16.1GB）、`mlx-community/Qwen3.8-27B-MTP-4bit` + `EigenLabs/Qwen3.8-27B-MTP-bf16`（MTP 头，供路径 B 后续用）、既有 `.venv-dflash`（bstnxbt dflash-mlx 0.1.8，Ornith 蒸馏用）。

## 六、复现方式（如需重测）

```bash
python3.12 -m venv .venv-dflash2 && ./.venv-dflash2/bin/pip install "dflash[local]"
# drafter: curl 下载 z-lab/Qwen3.8-27B-DFlash2 的 config.json + model.safetensors，回填 HF 缓存
./.venv-dflash2/bin/python -c "
import mlx.core as mx
from dflash.benchmark import apply_chat_template, load_mlx_models
from dflash.model_mlx import stream_generate
import time
mx.random.seed(0)
model, draft, tokenizer = load_mlx_models('mlx-community/Qwen3.8-27B-4bit','z-lab/Qwen3.8-27B-DFlash2',4)
p = apply_chat_template(tokenizer,[{'role':'user','content':'Write a quicksort in Python. '}],'off')
t=time.time(); n=0
for r in stream_generate(model,draft,tokenizer,p,block_size=5,max_tokens=256,temperature=0.7,top_p=1.0,top_k=20): n+=1
print(f'{n} tokens in {time.time()-t:.2f}s')
"
```