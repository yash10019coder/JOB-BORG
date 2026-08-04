"""Rendered-field <-> answer-resolution glue for the Greenhouse form client.

Defines the ``FormSchema``/``FormField`` shape ``client.inspect()`` returns
and ``client.submit()`` consumes, the set of field types this slice knows
how to fill, and the schema-drift comparison used to fail closed when a
form has changed between draft time and send time.
"""
from dataclasses import dataclass, field

# -- Supported field types ---------------------------------------------------

TEXT = "text"
TEXTAREA = "textarea"
SINGLE_SELECT = "single_select"
MULTI_SELECT = "multi_select"
FILE = "file"
# A JS-driven "combobox" widget (react-select and similar): a text `<input
# role="combobox">` that opens a listbox of options on click/type rather than
# a native `<select>`. Greenhouse renders Country, Location, and most custom
# single-choice questions this way -- see client.py's `_fill_combobox()`.
COMBOBOX_SELECT = "combobox_select"

# Every field type this slice knows how to fill. A *required* rendered field
# whose type falls outside this set is unsupported and drafting/submission
# must fail closed (GreenhouseFormSchemaMismatch) rather than skip it.
SUPPORTED_FIELD_TYPES = frozenset(
    {TEXT, TEXTAREA, SINGLE_SELECT, MULTI_SELECT, FILE, COMBOBOX_SELECT}
)

# Field types that carry a discrete option set (used for select/checkbox-group
# controls); relevant for the option-set comparison in schema_matches().
_OPTION_BEARING_TYPES = frozenset({SINGLE_SELECT, MULTI_SELECT})


@dataclass(frozen=True)
class FormField:
    """One rendered form field, as discovered by ``inspect()``.

    ``field_type`` is the raw discovered type string -- for select-like
    controls this is always one of ``SUPPORTED_FIELD_TYPES``' select
    variants; for unrecognized native input types (e.g. ``"date"``,
    ``"number"``) it is the raw HTML input ``type`` attribute value, which
    by construction is *not* in ``SUPPORTED_FIELD_TYPES`` -- callers decide
    whether that's fatal based on ``required``.
    """

    label: str
    field_type: str
    required: bool
    options: tuple[str, ...] = field(default_factory=tuple)
    # The rendered control's DOM `id`, captured at discovery time so
    # `_fill_answers()` can relocate the exact same element directly rather
    # than re-deriving it from `label` text -- see client.py's
    # `_resolve_control_for_label()` for why label-text-based lookup alone
    # is unreliable (ambiguous or empty for some real Greenhouse widgets).
    # Empty string for schemas discovered before this field existed, or for
    # a control with no `id` (label wraps it implicitly); callers fall back
    # to label-based lookup in that case.
    control_id: str = ""

    @property
    def is_supported(self) -> bool:
        return self.field_type in SUPPORTED_FIELD_TYPES


@dataclass(frozen=True)
class FormSchema:
    """The full set of fields rendered on a Greenhouse application page."""

    fields: tuple[FormField, ...] = field(default_factory=tuple)

    def by_label(self) -> dict[str, FormField]:
        return {f.label: f for f in self.fields}


@dataclass(frozen=True)
class SubmissionResult:
    """The outcome of a successful ``submit()`` call."""

    success: bool
    confirmation_text: str = ""


def schema_to_dict(schema: FormSchema) -> dict:
    """Serialize a `FormSchema` for storage (e.g. `AutoApplyDraft.form_schema_snapshot`).

    Plain-dict/list/str shape only, so it round-trips through a JSONField
    without a custom encoder.
    """
    return {
        "fields": [
            {
                "label": f.label,
                "field_type": f.field_type,
                "required": f.required,
                "options": list(f.options),
                "control_id": f.control_id,
            }
            for f in schema.fields
        ]
    }


def schema_from_dict(data: dict | None) -> FormSchema | None:
    """Inverse of `schema_to_dict`. Returns `None` for `None`/empty input so
    callers can distinguish "no snapshot stored" (older draft, or drafted
    before this field existed) from an empty schema."""
    if not data:
        return None
    return FormSchema(
        fields=tuple(
            FormField(
                label=f["label"],
                field_type=f["field_type"],
                required=f["required"],
                options=tuple(f.get("options", ())),
                control_id=f.get("control_id", ""),
            )
            for f in data.get("fields", [])
        )
    )


def schema_matches(expected: FormSchema, actual: FormSchema) -> bool:
    """Whether ``actual`` (freshly re-inspected) still matches ``expected``.

    ``expected`` is the schema a submission was drafted/answered against;
    ``actual`` is what ``submit()`` sees on the live page immediately
    before filling. A mismatch on field set, type, required-ness, *or* the
    option **set** (order-independent) of a select-like field counts as
    drift -- comparing only type/label/required-ness would miss an employer
    silently adding/removing an option between draft and send.
    """
    expected_by_label = expected.by_label()
    actual_by_label = actual.by_label()
    if set(expected_by_label) != set(actual_by_label):
        return False
    for label, expected_field in expected_by_label.items():
        actual_field = actual_by_label[label]
        if expected_field.field_type != actual_field.field_type:
            return False
        if expected_field.required != actual_field.required:
            return False
        if expected_field.field_type in _OPTION_BEARING_TYPES:
            if set(expected_field.options) != set(actual_field.options):
                return False
    return True
