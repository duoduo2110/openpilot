"""
Copyright (c) 2026-, sunnypilot contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

外置大模型加速器的客户端层（OPRP over TCP）。

本包只做「协议 + 客户端 + 配置 + 状态机骨架」，不接线 modeld：
- protocol.py  OPRP 线格式（自包含移植，header/payload 双 CRC32）
- config.py    /data/models/accelerator.json 的原子读写
- client.py    带 1 帧流水的 OPRP 客户端（按 req_id 配对、绝对 deadline）
- status.py    可序列化链路状态（延迟分位、帧计数、big-active）
- switching.py 换入/重连退避与 engaged 门控的纯函数
- backend.py   门面层落地实现（惰性连接、进度、shutdown）

包导入必须保持轻量：门槛逻辑在门面层 accelerators/__init__.py，依赖缺失返回负默认。
"""
