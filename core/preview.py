"""清理预览：把扫描结果整理成“将要处理什么”的只读清单。

预览只计算条数、条件和风险提示，不执行删除。
"""

from __future__ import annotations

from typing import Any

from .models import STATUS_LABELS

STATUS_ORDER = ("orphan_group", "expired_temp", "missing_friend")
TABLE_LABELS = {
    "umo_aliases": "会话别名",
    "conversations": "对话记录",
    "preferences": "偏好/分析记录",
    "provider_stats": "调用统计",
    "platform_message_history": "平台消息历史",
}
TABLE_CONDITIONS = {
    "umo_aliases": "删除匹配 UMO 的别名行",
    "conversations": "删除 user_id 匹配 UMO 的对话行",
    "preferences": "删除 analysis_* 或会话配置中匹配的偏好行",
    "provider_stats": "删除 umo 匹配的调用统计行（可选）",
    "platform_message_history": "删除 user_id 匹配 UMO 的消息历史行",
}


def _target_view(target: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in target.items() if key != "preference_row_ids"}


def build_preview(
    scan: dict[str, Any],
    include_provider_stats: bool = False,
) -> dict[str, Any]:
    targets = [item for item in (scan.get("targets") or []) if item.get("selectable")]
    counts: dict[str, int] = {key: 0 for key in TABLE_LABELS}

    for target in targets:
        for table, value in (target.get("counts") or {}).items():
            if table not in counts:
                continue
            if table == "provider_stats" and not include_provider_stats:
                continue
            counts[table] += int(value or 0)

    total_rows = sum(counts.values())
    groups: list[dict[str, Any]] = []
    for status in STATUS_ORDER:
        items = [item for item in targets if item.get("status") == status]
        if not items:
            continue
        groups.append(
            {
                "status": status,
                "label": STATUS_LABELS.get(status, status),
                "count": len(items),
                "items": [_target_view(item) for item in items],
            }
        )

    actions = []
    for table, count in counts.items():
        if not count:
            continue
        actions.append(
            {
                "table": table,
                "label": TABLE_LABELS.get(table) or table,
                "condition": TABLE_CONDITIONS.get(table) or "",
                "count": count,
                "optional": table == "provider_stats",
                "note": "",
            }
        )

    warnings = list(scan.get("warnings") or [])
    if not include_provider_stats and any(
        int((target.get("counts") or {}).get("provider_stats") or 0)
        for target in targets
    ):
        warnings.append("调用统计未计入本次预览；需要时在预览页单独勾选")
    if not targets:
        warnings.append("没有可清理的候选；请确认平台列表已成功读取")

    return {
        "ok": True,
        "readonly": True,
        "generated_at": scan.get("generated_at"),
        "include_provider_stats": bool(include_provider_stats),
        "summary": {
            "targets": len(targets),
            "default_targets": sum(1 for item in targets if item.get("default_selected")),
            "rows": counts,
            "total_rows": total_rows,
        },
        "groups": groups,
        "actions": actions,
        "warnings": warnings,
    }
