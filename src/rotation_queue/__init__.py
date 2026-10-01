"""学生互动轮转排队后端。

事件溯源 + 确定性公平排序的轮转排队服务：
- model：聚合投影与命令处理（纯函数，确定性）
- ranking：可解释的公平排序键
- store：SQLite 事件存储（原子事务 + 命令级幂等）
- service：应用服务编排
- views：读取模型与基于角色的隐私裁剪
- api：HTTP 接口与角色鉴权
"""
from .errors import ConflictError, DomainError, ForbiddenError, NotFoundError, ValidationError
from .events import Event
from .model import State, apply, handle
from .ranking import SignupView, build_ranking, rank_key
from .service import QueueService
from .store import EventStore

__all__ = [
    "ConflictError",
    "DomainError",
    "ForbiddenError",
    "NotFoundError",
    "ValidationError",
    "Event",
    "State",
    "apply",
    "handle",
    "SignupView",
    "build_ranking",
    "rank_key",
    "QueueService",
    "EventStore",
]
