"""会话清道夫（V0.7.2）。

扫描 AstrBot 核心库与当前 QQ 群/好友列表，找出已退群、已删好友和过期临时
会话的残留数据；支持数据库体检、预览、备份、回滚、会话详情、完整对话记录、
定时自动清理和执行后通知。扫描与体检只读，完全不读取、不修改任何插件数据。
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
from .core.automation import AutoCleaner
from .core.backup import BackupStore
from .core.store import ScanStore
from .page_api import PageApi


@register(
    PLUGIN_NAME,
    "烬辞",
    "扫描、预览并清理已退群、已删好友、过期临时会话的核心库残留数据（V0.7.2，支持数据库体检、详情、备份回滚和定时自动清理）",
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
        self.automation = AutoCleaner(self, self.page_api)
        self.automation.start()
        if not self.db_path.is_file():
            logger.warning("会话清道夫未找到 AstrBot 主库：%s", self.db_path)

    async def terminate(self) -> None:
        if getattr(self, "automation", None) is not None:
            await self.automation.stop()
        if getattr(self, "page_api", None) is not None:
            await self.page_api.shutdown()

    def _resolve_db_path(self) -> Path:
        if get_astrbot_data_path is not None:
            try:
                return Path(get_astrbot_data_path()) / "data_v4.db"
            except Exception:
                pass
        # data/plugin_data/<plugin_name> -> data/data_v4.db
        return self.data_dir.parent.parent / "data_v4.db"
