"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import correction, resource

# correction 在 resource 之前：/api/correction_batch/publish-list 等固定路径
# 不能被通用路由 /api/{module}/{entry_id} 抢先匹配。
ROUTERS = [correction, resource]
