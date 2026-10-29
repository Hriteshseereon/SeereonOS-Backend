"""Status labels and tones — port of src/domain/labels.ts (data from the generated catalogue)."""

from typing import Any

from app.catalog import cat
from app.frozen import FrozenDict

_C = cat('domain/labels')


class _LeaveTypeLabels(FrozenDict):
    """LEAVE_TYPE with the tenant's custom leave types (v9, D10): `LEAVE_TYPE[code]` / `.get(code)` of a code that is
    not built in resolves the custom type's name from the current request's leave_policies."""

    __slots__ = ()

    def __missing__(self, key: Any) -> Any:
        from app.engine.leave_types import custom_leave_name

        v = custom_leave_name(key)
        if v is None:
            raise KeyError(key)
        return v

    def get(self, key: Any, default: Any = None) -> Any:
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        from app.engine.leave_types import custom_leave_name

        return custom_leave_name(key, default)


PROJECT_STATUS = _C['PROJECT_STATUS']
PROJECT_TYPE = _C['PROJECT_TYPE']
BILLING_MODEL = _C['BILLING_MODEL']
STREAM = _C['STREAM']
TASK_STATUS = _C['TASK_STATUS']
COMPLEXITY = _C['COMPLEXITY']
WEEK_STATUS = _C['WEEK_STATUS']
LEAVE_TYPE = _LeaveTypeLabels(_C['LEAVE_TYPE'])
DEAL_STAGE = _C['DEAL_STAGE']
DEAL_STAGES_OPEN = _C['DEAL_STAGES_OPEN']
DEAL_SOURCE = _C['DEAL_SOURCE']
ACTIVITY_TYPE = _C['ACTIVITY_TYPE']
CR_STATUS = _C['CR_STATUS']
HEALTH = _C['HEALTH']
INTERNAL_CODES = _C['INTERNAL_CODES']
TASK_STAGE = _C['TASK_STAGE']
TASK_STAGE_ORDER = _C['TASK_STAGE_ORDER']
TASK_TAG_LABEL = _C['TASK_TAG_LABEL']
COMPLEXITY_ORDER = _C['COMPLEXITY_ORDER']
