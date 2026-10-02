from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import stat
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hermes_workflow_state import GenerationStore, ProtocolError, load_json

NUTRIENTS = ("energy_kcal", "protein_g", "carbohydrate_g", "fat_g")
TWO_PLACES = Decimal("0.01")
STATE_DOCUMENT_VERSION = 2


class ContractError(RuntimeError):
    """A workflow input violates the deterministic contract."""


def _exact_dict(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _decimal(value: Any, label: str, *, minimum: Decimal = Decimal(0)) -> Decimal:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a decimal string")
    if not 1 <= len(value) <= 64:
        raise ContractError(f"{label} must be a bounded decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ContractError(f"{label} must be a decimal string") from exc
    if not result.is_finite() or result < minimum or result > Decimal(1000000000):
        raise ContractError(f"{label} is outside the supported range")
    return result


def _text(value: Any, label: str, maximum: int = 120) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ContractError(f"{label} must be bounded text without controls")
    return value


def _format(value: Decimal) -> str:
    return str(value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def _mass_g(ingredient: Mapping[str, Any]) -> Decimal:
    quantity = _decimal(ingredient["quantity"], "ingredient quantity")
    unit = ingredient["unit"]
    if unit == "mg":
        return quantity / Decimal(1000)
    if unit == "g":
        return quantity
    if unit == "kg":
        return quantity * Decimal(1000)
    if unit in {"ml", "l"}:
        if "density_g_per_ml" not in ingredient:
            raise ContractError("volume ingredients require density_g_per_ml")
        density = _decimal(
            ingredient["density_g_per_ml"], "ingredient density", minimum=Decimal("0.000001")
        )
        millilitres = quantity if unit == "ml" else quantity * Decimal(1000)
        return millilitres * density
    raise ContractError("ingredient unit is unsupported")


def calculate_portions(value: Any) -> dict[str, Any]:
    plan = _exact_dict(value, {"portions", "ingredients"}, "portion plan")
    portions = plan["portions"]
    if type(portions) is not int or not 1 <= portions <= 100:
        raise ContractError("portions must be an integer between 1 and 100")
    ingredients = plan["ingredients"]
    if not isinstance(ingredients, list) or not 1 <= len(ingredients) <= 100:
        raise ContractError("portion plan must contain 1 to 100 ingredients")

    totals = {nutrient: Decimal(0) for nutrient in NUTRIENTS}
    absolute_uncertainty = {nutrient: Decimal(0) for nutrient in NUTRIENTS}
    total_mass = Decimal(0)
    provenance: list[dict[str, str]] = []
    names: set[str] = set()
    for raw in ingredients:
        if not isinstance(raw, dict):
            raise ContractError("ingredient must be an object")
        required = {
            "name",
            "quantity",
            "unit",
            "nutrition_per_100g",
            "uncertainty_percent",
            "provenance",
        }
        allowed = required | {"density_g_per_ml"}
        if not required <= set(raw) or not set(raw) <= allowed:
            raise ContractError("ingredient fields do not match the supported contract")
        name = _text(raw["name"], "ingredient name")
        if name in names:
            raise ContractError("ingredient names must be unique")
        names.add(name)
        mass = _mass_g(raw)
        if mass <= 0:
            raise ContractError("ingredient mass must be positive")
        nutrition = _exact_dict(raw["nutrition_per_100g"], set(NUTRIENTS), "nutrition facts")
        uncertainty = _decimal(raw["uncertainty_percent"], "ingredient uncertainty")
        if uncertainty > 100:
            raise ContractError("ingredient uncertainty cannot exceed 100 percent")
        source = _exact_dict(raw["provenance"], {"source", "source_id", "version"}, "provenance")
        normalized_source = {
            key: _text(source[key], f"provenance {key}", 200)
            for key in ("source", "source_id", "version")
        }
        provenance.append(normalized_source)
        total_mass += mass
        factor = mass / Decimal(100)
        for nutrient in NUTRIENTS:
            amount = _decimal(nutrition[nutrient], f"nutrition {nutrient}") * factor
            totals[nutrient] += amount
            absolute_uncertainty[nutrient] += amount * uncertainty / Decimal(100)

    uncertainty_result = {}
    for nutrient in NUTRIENTS:
        percent = (
            Decimal(0)
            if totals[nutrient] == 0
            else absolute_uncertainty[nutrient] * 100 / totals[nutrient]
        )
        uncertainty_result[nutrient] = f"±{_format(percent)}%"
    return {
        "total_mass_g": _format(total_mass),
        "totals": {key: _format(totals[key]) for key in NUTRIENTS},
        "per_portion": {key: _format(totals[key] / portions) for key in NUTRIENTS},
        "uncertainty": uncertainty_result,
        "provenance": provenance,
    }


def _timestamp(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
        raise ContractError(f"{label} must be canonical UTC")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{label} must be canonical UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ContractError(f"{label} must be canonical UTC")
    return parsed


def _measurement(value: Any) -> dict[str, Any]:
    row = _exact_dict(
        value,
        {
            "id",
            "measured_at",
            "value_kg",
            "uncertainty_kg",
            "context",
            "provenance",
            "correction_of",
        },
        "measurement",
    )
    identifier = _text(row["id"], "measurement id", 128)
    measured_at = _timestamp(row["measured_at"], "measurement timestamp")
    weight = _decimal(row["value_kg"], "measurement value", minimum=Decimal(1))
    if weight > 1000:
        raise ContractError("measurement value is outside the supported range")
    uncertainty = _decimal(row["uncertainty_kg"], "measurement uncertainty")
    if uncertainty > 10:
        raise ContractError("measurement uncertainty is outside the supported range")
    context = _exact_dict(
        row["context"],
        {"device_id", "calibration_version", "placement", "conditions"},
        "measurement context",
    )
    normalized_context = {
        key: _text(context[key], f"measurement context {key}", 160)
        for key in ("device_id", "calibration_version", "placement", "conditions")
    }
    source = _exact_dict(row["provenance"], {"source", "source_id", "version"}, "provenance")
    provenance = {
        key: _text(source[key], f"provenance {key}", 200)
        for key in ("source", "source_id", "version")
    }
    correction_of = row["correction_of"]
    if correction_of is not None:
        correction_of = _text(correction_of, "correction target", 128)
        if correction_of == identifier:
            raise ContractError("measurement cannot correct itself")
    return {
        "id": identifier,
        "measured_at": measured_at,
        "value_kg": weight,
        "uncertainty_kg": uncertainty,
        "context": normalized_context,
        "provenance": provenance,
        "correction_of": correction_of,
    }


def analyze_weight_trend(value: Any) -> dict[str, Any]:
    if not isinstance(value, list) or not 1 <= len(value) <= 366:
        raise ContractError("weight trend requires 1 to 366 measurements")
    rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for raw in value:
        row = _measurement(raw)
        if row["id"] in by_id:
            raise ContractError(f"duplicate measurement id: {row['id']}")
        by_id[row["id"]] = row
        rows.append(row)

    superseded: set[str] = set()
    for row in rows:
        target = row["correction_of"]
        if target is None:
            continue
        if target not in by_id:
            raise ContractError(f"correction target does not exist: {target}")
        if target in superseded:
            raise ContractError("a measurement may have only one correction")
        if by_id[target]["correction_of"] is not None:
            raise ContractError("correction chains are unsupported")
        superseded.add(target)

    candidates = [row for row in rows if row["id"] not in superseded]
    effective: list[dict[str, Any]] = []
    duplicate_ids: list[str] = []
    seen_observations: set[tuple[Any, ...]] = set()
    for row in candidates:
        context = row["context"]
        fingerprint = (
            row["measured_at"],
            row["value_kg"],
            row["uncertainty_kg"],
            context["device_id"],
            context["calibration_version"],
            context["placement"],
            context["conditions"],
        )
        if fingerprint in seen_observations:
            duplicate_ids.append(row["id"])
            continue
        seen_observations.add(fingerprint)
        effective.append(row)

    effective.sort(key=lambda item: (item["measured_at"], item["id"]))
    first = effective[0]
    last = effective[-1]
    span_days = (last["measured_at"].date() - first["measured_at"].date()).days
    context_keys = {
        (
            row["context"]["device_id"],
            row["context"]["calibration_version"],
            row["context"]["placement"],
            row["context"]["conditions"],
        )
        for row in effective
    }
    reason = "qualified"
    qualified = True
    if len(effective) < 3:
        qualified = False
        reason = "insufficient-measurements"
    elif span_days < 7:
        qualified = False
        reason = "insufficient-span"
    elif len(context_keys) != 1:
        qualified = False
        reason = "measurement-context-changed"
    change = last["value_kg"] - first["value_kg"]
    result = {
        "qualified": qualified,
        "reason": reason,
        "effective_count": len(effective),
        "duplicate_ids": sorted(duplicate_ids),
        "superseded_ids": sorted(superseded),
        "span_days": span_days,
        "change_kg": _format(change),
        "uncertainty_kg": f"±{_format(first['uncertainty_kg'] + last['uncertainty_kg'])}",
        "provenance": [row["provenance"] for row in effective],
    }
    return result


WORKFLOWS = {
    "weight-check-in-and-trend",
    "daily-intake-closeout",
    "household-portion-planning",
}


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"private overlay has duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ContractError(f"non-finite number is forbidden: {value}")


def read_private_overlay(path: Path) -> dict[str, Any]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ContractError("private overlay is missing") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ContractError("private overlay must be a regular non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContractError("private overlay cannot be safely opened") from exc
    try:
        opened_mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(opened_mode):
            raise ContractError("private overlay must be a regular non-symlink file")
        if stat.S_IMODE(opened_mode) != 0o400:
            raise ContractError("private overlay must have mode 0400")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(1_000_001)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > 1_000_000:
        raise ContractError("private overlay exceeds the size cap")
    try:
        value = json.loads(raw, object_pairs_hook=_json_pairs, parse_constant=_reject_constant)
    except ContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("private overlay is malformed JSON") from exc
    document = _exact_dict(value, {"schema_version", "workflow", "data"}, "private overlay")
    if document["schema_version"] != 1:
        raise ContractError("private overlay schema is incompatible")
    if document["workflow"] not in WORKFLOWS:
        raise ContractError("private overlay workflow is unsupported")
    if not isinstance(document["data"], dict):
        raise ContractError("private overlay data must be an object")
    return document


def _preferences(value: Any) -> dict[str, Any]:
    preferences = dict(
        _exact_dict(
            value,
            {
                "schema_version",
                "workflow",
                "timezone",
                "start_date",
                "recurrence",
                "dependency",
            },
            "workflow preferences",
        )
    )
    if preferences["schema_version"] != 1:
        raise ContractError("workflow preference schema is incompatible")
    if preferences["workflow"] not in WORKFLOWS:
        raise ContractError("workflow is unsupported")
    timezone = _text(preferences["timezone"], "timezone", 100)
    try:
        preferences["zone"] = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ContractError("timezone is unknown") from exc
    try:
        preferences["start_date_parsed"] = dt.date.fromisoformat(preferences["start_date"])
    except (TypeError, ValueError) as exc:
        raise ContractError("start date must be an ISO local date") from exc
    recurrence = dict(
        _exact_dict(
            preferences["recurrence"],
            {"kind", "interval", "local_time", "weekdays"},
            "recurrence",
        )
    )
    preferences["recurrence"] = recurrence
    if recurrence["kind"] not in {"daily", "weekly"}:
        raise ContractError("recurrence kind must be daily or weekly")
    if type(recurrence["interval"]) is not int or not 1 <= recurrence["interval"] <= 31:
        raise ContractError("recurrence interval is outside the supported bound")
    try:
        recurrence["local_time_parsed"] = dt.time.fromisoformat(recurrence["local_time"])
    except (TypeError, ValueError) as exc:
        raise ContractError("recurrence local time is invalid") from exc
    if recurrence["local_time_parsed"].tzinfo is not None or recurrence["local_time_parsed"].second:
        raise ContractError("recurrence local time must be minute-precision local wall time")
    weekdays = recurrence["weekdays"]
    if (
        not isinstance(weekdays, list)
        or len(weekdays) != len(set(weekdays))
        or any(type(day) is not int or not 0 <= day <= 6 for day in weekdays)
        or (recurrence["kind"] == "weekly" and not weekdays)
        or (recurrence["kind"] == "daily" and weekdays)
    ):
        raise ContractError("recurrence weekdays do not match the recurrence kind")
    dependency = _exact_dict(preferences["dependency"], {"present", "revision"}, "dependency gate")
    if type(dependency["present"]) is not bool:
        raise ContractError("dependency presence must be boolean")
    _text(dependency["revision"], "dependency revision", 200)
    return preferences


def _resolve_local(local_date: dt.date, local_time: dt.time, zone: ZoneInfo) -> dt.datetime:
    """Resolve wall time deterministically: earliest fold, then first valid minute after a DST gap."""
    naive = dt.datetime.combine(local_date, local_time)
    for offset in range(181):
        candidate = naive + dt.timedelta(minutes=offset)
        for fold in (0, 1):
            aware = candidate.replace(tzinfo=zone, fold=fold)
            roundtrip = aware.astimezone(dt.timezone.utc).astimezone(zone)
            if roundtrip.replace(tzinfo=None) == candidate and roundtrip.fold == fold:
                return aware.astimezone(dt.timezone.utc)
    raise ContractError("recurrence wall time cannot be resolved")


def _latest_occurrence(
    preferences: Mapping[str, Any], now: dt.datetime
) -> tuple[str, dt.datetime] | None:
    now_utc = now.astimezone(dt.timezone.utc)
    local_today = now_utc.astimezone(preferences["zone"]).date()
    start = preferences["start_date_parsed"]
    recurrence = preferences["recurrence"]
    for age in range(3670):
        day = local_today - dt.timedelta(days=age)
        if day < start:
            return None
        elapsed = (day - start).days
        if recurrence["kind"] == "daily":
            selected = elapsed % recurrence["interval"] == 0
        else:
            week = elapsed // 7
            selected = (
                day.weekday() in recurrence["weekdays"] and week % recurrence["interval"] == 0
            )
        if not selected:
            continue
        due_at = _resolve_local(day, recurrence["local_time_parsed"], preferences["zone"])
        if due_at <= now_utc:
            return f"{preferences['workflow']}:{day.isoformat()}", due_at
    raise ContractError("recurrence search exceeded the supported horizon")


def _questions(data: Mapping[str, Any]) -> list[str]:
    questions = data.get("data_quality_questions")
    if not isinstance(questions, list) or len(questions) > 8:
        raise ContractError("data-quality questions must be a bounded list")
    return [_text(question, "data-quality question", 200) for question in questions]


def _render_workflow(workflow: str, data: Mapping[str, Any]) -> str:
    if workflow == "weight-check-in-and-trend":
        data = _exact_dict(data, {"measurements", "data_quality_questions"}, "weight workflow data")
    elif workflow == "daily-intake-closeout":
        data = _exact_dict(data, {"entries", "data_quality_questions"}, "daily closeout data")
    elif workflow == "household-portion-planning":
        data = _exact_dict(data, {"plan", "data_quality_questions"}, "household portion data")
    else:
        raise ContractError("workflow is unsupported")

    questions = _questions(data)
    if questions:
        prompt = (
            "ACTIONABLE NUTRITION DATA-QUALITY PROMPT\n"
            f"Workflow: {workflow}\n"
            "Resolve only these bounded questions; do not infer missing private values:\n"
            + "".join(f"- {question}\n" for question in questions)
        )
        if len(prompt.encode("utf-8")) > 1200:
            raise ContractError("actionable prompt exceeds the byte cap")
        return prompt

    if workflow == "weight-check-in-and-trend":
        measurements = data["measurements"]
        if not isinstance(measurements, list):
            raise ContractError("weight measurements must be a list")
        if not measurements:
            return "Weight check-in is due.\n"
        trend = analyze_weight_trend(measurements)
        if trend["qualified"]:
            return (
                f"Qualified weight trend over {trend['span_days']} days: {trend['change_kg']} kg "
                f"({trend['uncertainty_kg']}).\n"
            )
        return f"Weight check-in is due; trend not qualified ({trend['reason']}).\n"

    if workflow == "daily-intake-closeout":
        entries = data["entries"]
        if not isinstance(entries, list):
            raise ContractError("daily entries must be a list")
        if not entries:
            return "Daily intake closeout: no entries recorded.\n"
        summary = calculate_portions({"portions": 1, "ingredients": entries})
        totals = summary["totals"]
        return (
            f"Daily intake closeout: {totals['energy_kcal']} kcal; protein {totals['protein_g']} g; "
            f"carbohydrate {totals['carbohydrate_g']} g; fat {totals['fat_g']} g; "
            f"energy uncertainty {summary['uncertainty']['energy_kcal']}.\n"
        )

    plan = data["plan"]
    result = calculate_portions(plan)
    per_portion = result["per_portion"]
    return (
        f"Household portions: {plan['portions']}; per portion {per_portion['energy_kcal']} kcal, "
        f"protein {per_portion['protein_g']} g, carbohydrate {per_portion['carbohydrate_g']} g, "
        f"fat {per_portion['fat_g']} g; energy uncertainty {result['uncertainty']['energy_kcal']}.\n"
    )


class WorkflowRunner:
    def __init__(self, state_dir: Path, *, lock_timeout: float = 10.0) -> None:
        self.store = GenerationStore(
            state_dir,
            protocol="nutrition-due-state",
            schema_version=1,
            lock_timeout=lock_timeout,
        )

    @staticmethod
    def _rendered(selected: Any | None) -> list[str]:
        if selected is None:
            return []
        if set(selected.documents) != {"due-state.json"}:
            raise ContractError("nutrition due state has an unexpected document set")
        value = selected.documents["due-state.json"]
        if not isinstance(value, dict):
            raise ContractError("nutrition due state must be an object")
        version = value.get("schema_version")
        if version == 1:
            identity_field = "acknowledged_occurrences"
        elif version == STATE_DOCUMENT_VERSION:
            identity_field = "rendered_occurrences"
        else:
            raise ContractError("nutrition due state is malformed")
        document = _exact_dict(value, {"schema_version", identity_field}, "nutrition due state")
        identities = document[identity_field]
        if not isinstance(identities, list) or len(identities) > 10000:
            raise ContractError("nutrition due state is malformed")
        for identity in identities:
            _text(identity, "logical occurrence identity", 420)
        if identities != sorted(set(identities)):
            raise ContractError("nutrition due state is malformed")
        return identities

    def run(self, preference_value: Any, private_overlay: Path, now: dt.datetime) -> str:
        preferences = _preferences(preference_value)
        if not isinstance(now, dt.datetime) or now.tzinfo is None:
            raise ContractError("current time must be timezone-aware")
        occurrence = _latest_occurrence(preferences, now)
        if occurrence is None or not preferences["dependency"]["present"]:
            return ""
        logical_key, _due_at = occurrence

        overlay = read_private_overlay(private_overlay)
        if overlay["workflow"] != preferences["workflow"]:
            raise ContractError("private overlay workflow does not match the preferences")
        output = _render_workflow(preferences["workflow"], overlay["data"])
        identity = logical_key
        claimed = False

        def transform(selected: Any | None) -> dict[str, Any]:
            nonlocal claimed
            identities = self._rendered(selected)
            if identity in identities:
                return {
                    "due-state.json": {
                        "schema_version": STATE_DOCUMENT_VERSION,
                        "rendered_occurrences": identities,
                    }
                }
            claimed = True
            identities = sorted(identities + [identity])
            return {
                "due-state.json": {
                    "schema_version": STATE_DOCUMENT_VERSION,
                    "rendered_occurrences": identities,
                }
            }

        try:
            self.store.update(transform)
        except ProtocolError as exc:
            raise ContractError("nutrition workflow state is unavailable or malformed") from exc
        return output if claimed else ""


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Deterministic private nutrition workflow gate")
    result.add_argument("--preferences", type=Path, required=True)
    result.add_argument("--private-overlay", type=Path, required=True)
    result.add_argument("--state-dir", type=Path, required=True)
    result.add_argument(
        "--now", help="canonical UTC test/control timestamp; defaults to current UTC"
    )
    result.add_argument("--lock-timeout", type=float, default=10.0)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        preferences = load_json(args.preferences)
        now = (
            dt.datetime.now(dt.timezone.utc)
            if args.now is None
            else _timestamp(args.now, "current time")
        )
        output = WorkflowRunner(args.state_dir, lock_timeout=args.lock_timeout).run(
            preferences,
            args.private_overlay,
            now,
        )
        if output:
            os.write(1, output.encode("utf-8"))
        return 0
    except (ContractError, ProtocolError, OSError):
        # The overlay is private. Scheduled stderr must not echo values, paths,
        # identifiers, or parser details originating from that document.
        os.write(2, b"nutrition workflow input or state failed validation\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
