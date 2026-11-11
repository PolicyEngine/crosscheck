"""crosscheck: a shared row format for comparing tax-benefit models case by case.

The package holds the format and nothing else. It runs no model and imports no engine.
"""

from crosscheck.records import (
    AI_MODEL_KINDS,
    FILE_ID_PATTERN,
    RECORD_TYPES,
    Alias,
    AuthorKind,
    Binding,
    BorrowedInput,
    Case,
    CauseKind,
    CellStatus,
    EngineLink,
    EngineRole,
    EntryStatus,
    Fact,
    LedgerCell,
    LedgerEntry,
    LedgerRuleError,
    Model,
    ModelKind,
    PolicyWorld,
    Quantity,
    RecordError,
    Run,
    Scalar,
    ScopedNote,
    Source,
    Switch,
    ToleranceMethod,
    ToleranceProfile,
    ToleranceRule,
    Value,
    ValueStatus,
    ValueType,
    close_entry,
    from_dict,
    key_of,
    to_dict,
)
from crosscheck.schema import SchemaError, export_schemas, json_schema, table_schema, validate
from crosscheck.table import IntegrityError, Table

__version__ = "0.1.0"

__all__ = [
    "AI_MODEL_KINDS",
    "FILE_ID_PATTERN",
    "RECORD_TYPES",
    "Alias",
    "AuthorKind",
    "Binding",
    "BorrowedInput",
    "Case",
    "CauseKind",
    "CellStatus",
    "EngineLink",
    "EngineRole",
    "EntryStatus",
    "Fact",
    "IntegrityError",
    "LedgerCell",
    "LedgerEntry",
    "LedgerRuleError",
    "Model",
    "ModelKind",
    "PolicyWorld",
    "Quantity",
    "RecordError",
    "Run",
    "Scalar",
    "SchemaError",
    "ScopedNote",
    "Source",
    "Switch",
    "Table",
    "ToleranceMethod",
    "ToleranceProfile",
    "ToleranceRule",
    "Value",
    "ValueStatus",
    "ValueType",
    "__version__",
    "close_entry",
    "export_schemas",
    "from_dict",
    "json_schema",
    "key_of",
    "table_schema",
    "to_dict",
    "validate",
]

# Store, tolerance, lineage and cell status (unit B).
from crosscheck.lineage import lineage_tokens, lineages, same_lineage, share_directly  # noqa: E402
from crosscheck.status import CellReport, cell_status  # noqa: E402
from crosscheck.store import StoreError, check_manifest, read_store, write_store  # noqa: E402
from crosscheck.tolerance import (  # noqa: E402
    Comparison,
    ToleranceError,
    agree,
    compare_runs,
    rule_for,
)

__all__ += [
    "CellReport",
    "Comparison",
    "StoreError",
    "ToleranceError",
    "agree",
    "cell_status",
    "check_manifest",
    "compare_runs",
    "lineage_tokens",
    "lineages",
    "read_store",
    "rule_for",
    "same_lineage",
    "share_directly",
    "write_store",
]

# Model registry (fix round 1).
from crosscheck.registry import (  # noqa: E402
    KNOWN_MODELS,
    NOT_RECORDED,
    TOOL_NAMES,
    known_model,
    registry_id,
    tool_name,
)

__all__ += [
    "KNOWN_MODELS",
    "NOT_RECORDED",
    "TOOL_NAMES",
    "known_model",
    "registry_id",
    "tool_name",
]
