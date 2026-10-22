"""会话清道夫（V0.4）。

扫描 AstrBot 核心库与当前 QQ 群/好友列表，找出已退群、已删好友和过期临时
会话的残留数据；执行清理前自动备份，支持面板回滚与清理进度。扫描阶段只读，
完全不读取、不修改任何插件数据，详情也只查询核心库 data_v4.db。
"""

from __future__ import annotations

from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover - 旧版本兜底
    get_astrbot_data_path = None

from .constants import DISPLAY_NAME, PLUGIN_NAME, PLUGIN_VERSION
from .core.backup import BackupStore
from .core.store import ScanStore
from .page_api import PageApi


@register(
    PLUGIN_NAME,
    "烬辞",
    "扫描、预览并清理已退群、已删好友、过期临时会话的核心库残留数据（V0.4，支持详情与备份回滚）",
    PLUGIN_VERSION,
)
class SessionJanitorPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = ScanStore(self.data_dir)
        self.backup_store = BackupStore(self.data_dir)
        self.db_path = self._resolve_db_path()
        self.page_api = PageApi(self)
        self.page_api.register()
        if not self.db_path.is_file():
            logger.warning("会话清道夫未找到 AstrBot 主库：%s", self.db_path)

    def _resolve_db_path(self) -> Path:
        if get_astrbot_data_path is not None:
            try:
                return Path(get_astrbot_data_path()) / "data_v4.db"
            except Exception:
                pass
        # data/plugin_data/<plugin_name> -> data/data_v4.db
        return self.data_dir.parent.parent / "data_v4.db"
