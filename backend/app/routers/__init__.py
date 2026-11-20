"""业务路由汇总：SCD 固定路径优先注册，通用资源动态路由随后。"""
from __future__ import annotations

from app.routers import resource, scd

ROUTERS = [scd, resource]
