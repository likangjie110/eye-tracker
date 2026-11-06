# 阶段 1 审计归档

本目录保存 harness 改造**阶段 1（全量扫描）**的原始证据，供计划评审与后续追溯。
归档日期：2026-11-06；代码基线：`harnessing` 起点 `9644527`（= `main` = tag `v0.3.0`）。

| 文件 | 内容 | 来源 |
|---|---|---|
| `ci-audit.md` | CI / 构建发布链路审计：5 个 workflow 全景、tag→Release 全路径、本地可复现性对照、51 条问题 | 子审计智能体（只读） |
| `quality-audit.md` | 测试与文档审计：pytest 基线、66 模块覆盖映射、29 条测试/文档问题、文档门禁边界 | 子审计智能体（只读） |
| `risk-audit.md` | 安全 / 隐私 / 可靠性审计：网络出口全景、更新链路信任边界、17 条问题、需复核项 | 子审计智能体（只读） |
| `scan-metrics.md` | 机械扫描输出：文件/函数/类大小、决策点、包依赖矩阵、循环依赖、重复函数、卫生标记 | `scripts/analysis/scan_metrics.py` |
| `local-gates-baseline.txt` | 改造前本地门禁实测输出（ruff / mypy / 隐私 / 模型校验） | 本机执行 |

**去重与归并**：三份审计共 97 条原始发现（CI 51 / 测试与文档 29 / 安全 17），与机械扫描、
人工复核合并后按同一根因归并为 `../tech-debt-tracker.md` 的 105 条（P0=0 / P1=21 / P2=84）。
审计报告保留原文，便于核对措辞与证据；台账是决策依据。

**复跑方式**：

```bash
uv run python scripts/analysis/scan_metrics.py            # Markdown 到 stdout
uv run python scripts/analysis/scan_metrics.py --json     # JSON 摘要
uv run pytest -q                                          # 2952 passed / 55 skipped（本机）
```
