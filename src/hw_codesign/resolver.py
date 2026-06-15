from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .models import Failure, FailureCategory, GateReport, ResolvedComponent, Status
from .supplier_adapters import supplier_adapter

# Supplier availability snapshots older than this are treated as missing evidence.
SUPPLIER_EVIDENCE_MAX_AGE_DAYS = 90


ROLE_BY_CATEGORY = {
    "power_input": "power_input", "fuse": "fuse", "reverse_polarity": "reverse_polarity",
    "tvs": "tvs", "efuse": "efuse", "mcu": "mcu", "imu": "imu", "can": "can",
    "estop": "estop", "safety_gate": "safety_gate", "can_connector": "can_connector",
    "debug": "debug", "usb": "usb", "usb_esd": "usb_esd", "motor_io": "motor_io",
}


def role_for_component(component: dict[str, Any]) -> str:
    category = component["category"]
    if category == "regulator":
        return "regulator_5v" if component["ref"] == "U4" else "regulator_3v3"
    if category == "termination": return "resistor_120r"
    if category == "discharge": return "resistor_100k"
    if category == "pullup": return "resistor_4k7"
    if category == "decoupling": return "capacitor_100n"
    if category == "bulk_cap": return "capacitor_22u" if component["ref"] == "C9" else "capacitor_10u_50v"
    return ROLE_BY_CATEGORY.get(category, category)


class ComponentResolver:
    def __init__(self, parts_root: Path | str):
        self.parts_root = Path(parts_root)
        self.supplier_availability_report = GateReport("supplier_availability", Status.BLOCKED)
        self.datasheet_evidence_report = GateReport("datasheet_evidence", Status.BLOCKED)

    @staticmethod
    def _file_hash(path: Path) -> str:
        return sha256(path.read_bytes()).hexdigest()

    def _load_supplier_records(self, provider: str) -> tuple[dict[str, dict[str, Any]], Path | None, list[Failure]]:
        path = self.parts_root / "suppliers" / f"{provider}.yaml"
        schema_path = self.parts_root / "schemas" / "supplier_catalog.schema.json"
        if not path.is_file() or not schema_path.is_file():
            return {}, None, [Failure(FailureCategory.BOM_ERROR, "supplier_catalog_missing", f"Supplier catalog is missing for provider {provider}")]
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        failures = [Failure(FailureCategory.BOM_ERROR, "invalid_supplier_catalog", error.message, path=str(path)) for error in Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8"))).iter_errors(payload)]
        if payload.get("provider") != provider:
            failures.append(Failure(FailureCategory.BOM_ERROR, "supplier_provider_mismatch", f"Catalog provider {payload.get('provider')} does not match requested provider {provider}", path=str(path)))
        records: dict[str, dict[str, Any]] = {}
        for record in payload.get("records", []):
            component_id = record.get("component_id")
            if component_id in records:
                failures.append(Failure(FailureCategory.BOM_ERROR, "ambiguous_supplier_record", f"Multiple {provider} records exist for {component_id}", path=str(path)))
            elif component_id:
                records[component_id] = record
        return records, path, failures

    def _load_evidence(self) -> tuple[dict[str, list[dict[str, Any]]], Path | None, list[Failure]]:
        path = self.parts_root / "evidence" / "datasheets.yaml"
        schema_path = self.parts_root / "schemas" / "evidence_catalog.schema.json"
        if not path.is_file() or not schema_path.is_file():
            return {}, None, [Failure(FailureCategory.BOM_ERROR, "datasheet_catalog_missing", "Datasheet evidence catalog is missing")]
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        failures = [Failure(FailureCategory.BOM_ERROR, "invalid_datasheet_catalog", error.message, path=str(path)) for error in Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8"))).iter_errors(payload)]
        by_component: dict[str, list[dict[str, Any]]] = {}
        evidence_ids: set[str] = set()
        for evidence in payload.get("evidence", []):
            if evidence.get("id") in evidence_ids:
                failures.append(Failure(FailureCategory.BOM_ERROR, "ambiguous_datasheet_evidence", f"Duplicate evidence id {evidence.get('id')}", path=str(path)))
            evidence_ids.add(evidence.get("id"))
            by_component.setdefault(evidence.get("component_id"), []).append(evidence)
        return by_component, path, failures

    def resolve(self, spec: dict[str, Any], role_set: str, components: list[dict[str, Any]]) -> tuple[list[ResolvedComponent], GateReport]:
        role_path = self.parts_root / "role_sets" / f"{role_set}.yaml"
        component_dir = self.parts_root / "components"
        if not role_path.is_file() or not component_dir.is_dir():
            return [], GateReport("component_resolution", Status.BLOCKED, [Failure(FailureCategory.BOM_ERROR, "parts_db_missing", "Curated parts database or role set is missing")])
        role_data = yaml.safe_load(role_path.read_text(encoding="utf-8")) or {}
        role_schema_path = self.parts_root / "schemas" / "role_set.schema.json"
        component_schema_path = self.parts_root / "schemas" / "component.schema.json"
        if not role_schema_path.is_file() or not component_schema_path.is_file():
            return [], GateReport("component_resolution", Status.BLOCKED, [Failure(FailureCategory.BOM_ERROR, "parts_schema_missing", "Parts database schemas are missing")])
        role_errors = list(Draft202012Validator(json.loads(role_schema_path.read_text(encoding="utf-8"))).iter_errors(role_data))
        if role_errors:
            return [], GateReport("component_resolution", Status.FAIL, [Failure(FailureCategory.BOM_ERROR, "invalid_role_set", error.message) for error in role_errors])
        database: dict[str, dict[str, Any]] = {}
        schema = json.loads(component_schema_path.read_text(encoding="utf-8"))
        database_failures: list[Failure] = []
        for path in sorted(component_dir.glob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            database_failures.extend(Failure(FailureCategory.BOM_ERROR, "invalid_component_db", error.message, path=str(path)) for error in Draft202012Validator(schema).iter_errors(payload))
            for item in payload.get("components", [payload] if payload.get("id") else []):
                if item["id"] in database:
                    database_failures.append(Failure(FailureCategory.BOM_ERROR, "ambiguous_component_id", f"Duplicate curated component id {item['id']}", path=str(path)))
                else:
                    database[item["id"]] = {**item, "_database_file": str(path)}
        if database_failures:
            return [], GateReport("component_resolution", Status.FAIL, database_failures)
        provider = spec.get("sourcing", {}).get("provider", "curated")
        try:
            adapter = supplier_adapter(provider)
        except ValueError as exc:
            failure = Failure(FailureCategory.BOM_ERROR, "supplier_adapter_unavailable", str(exc))
            self.supplier_availability_report = GateReport("supplier_availability", Status.BLOCKED, [failure], backend={"provider": provider})
            return [], GateReport("component_resolution", Status.BLOCKED, [failure])
        supplier_records, supplier_path, supplier_failures = self._load_supplier_records(provider)
        evidence_records, evidence_path, evidence_failures = self._load_evidence()
        failures: list[Failure] = []
        resolved: list[ResolvedComponent] = []
        critical = set(role_data.get("critical_roles", []))
        for instance in components:
            role = role_for_component(instance)
            selection = role_data.get("roles", {}).get(role)
            if not selection:
                failures.append(Failure(FailureCategory.BOM_ERROR, "role_unresolved", f"No component selection for role {role}", path=instance["ref"]))
                continue
            resolution = selection.get("resolution", "unresolved")
            stored_part = database.get(selection.get("component_id"))
            part = dict(stored_part) if stored_part else None
            if not part:
                failures.append(Failure(FailureCategory.BOM_ERROR, "component_missing", f"Selected component {selection.get('component_id')} is absent from curated DB", path=instance["ref"]))
                continue
            if role in critical and resolution != "curated":
                failures.append(Failure(FailureCategory.BOM_ERROR, "critical_role_not_curated", f"Critical role {role} must resolve to curated data", path=instance["ref"]))
            if resolution == "registry_candidate":
                failures.append(Failure(FailureCategory.BOM_ERROR, "registry_candidate_not_release_eligible", f"Registry candidate for {role} requires curation", path=instance["ref"]))
            database_file = Path(part.pop("_database_file"))
            supplier_record = supplier_records.get(part["id"])
            evidence = evidence_records.get(part["id"], [])
            normalized_offer = adapter.normalize(supplier_record).to_dict() if supplier_record else None
            part["supplier_offer"] = normalized_offer
            part["datasheet_evidence"] = evidence
            provenance = {
                "role_set": str(role_path),
                "role_set_sha256": self._file_hash(role_path),
                "database_file": str(database_file),
                "database_file_sha256": self._file_hash(database_file),
                "resolver": "in_repo_curated_v2",
                "supplier_provider": provider,
                "supplier_catalog": str(supplier_path) if supplier_path else None,
                "supplier_catalog_sha256": self._file_hash(supplier_path) if supplier_path else None,
                "supplier_record": normalized_offer,
                "datasheet_catalog": str(evidence_path) if evidence_path else None,
                "datasheet_catalog_sha256": self._file_hash(evidence_path) if evidence_path else None,
                "datasheet_evidence_ids": [item["id"] for item in evidence],
            }
            resolved.append(ResolvedComponent(instance["ref"], role, part["id"], resolution, part, provenance))
        availability_failures = list(supplier_failures)
        availability_blocked = any(failure.code == "supplier_catalog_missing" for failure in supplier_failures)
        availability_failed = any(failure.code != "supplier_catalog_missing" for failure in supplier_failures)
        evidence_gate_failures = list(evidence_failures)
        evidence_blocked = any(failure.code == "datasheet_catalog_missing" for failure in evidence_failures)
        evidence_failed = any(failure.code != "datasheet_catalog_missing" for failure in evidence_failures)
        for item in resolved:
            offer = item.data.get("supplier_offer")
            sourcing_waived = item.data.get("sourcing", {}).get("status") == "waived"
            if not offer:
                availability_blocked = True
                availability_failures.append(Failure(FailureCategory.BOM_ERROR, "supplier_record_missing", f"No {provider} supplier record for {item.component_id}", path=item.ref))
            elif sourcing_waived:
                continue
            elif offer.get("availability") in {"out_of_stock", "discontinued"}:
                availability_failed = True
                availability_failures.append(Failure(FailureCategory.BOM_ERROR, "supplier_unavailable", f"{item.ref} is {offer['availability']} at {provider}", path=item.ref))
            elif offer.get("availability") != "available" or not offer.get("sku") or not offer.get("observed_at"):
                availability_blocked = True
                availability_failures.append(Failure(FailureCategory.BOM_ERROR, "supplier_availability_unknown", f"Current availability is not evidenced for {item.ref} at {provider}", path=item.ref))
            elif _evidence_is_stale(offer["observed_at"]):
                availability_blocked = True
                availability_failures.append(Failure(FailureCategory.BOM_ERROR, "supplier_evidence_stale", f"{item.ref} availability evidence at {provider} is older than {SUPPLIER_EVIDENCE_MAX_AGE_DAYS} days", path=item.ref, details={"observed_at": offer["observed_at"], "max_age_days": SUPPLIER_EVIDENCE_MAX_AGE_DAYS}))
            evidence = item.data.get("datasheet_evidence", [])
            if not evidence:
                evidence_failed = True
                evidence_gate_failures.append(Failure(FailureCategory.BOM_ERROR, "datasheet_evidence_missing", f"No datasheet evidence is attached to {item.ref}", path=item.ref))
            elif not any(entry.get("review_status") == "approved" and {"pins", "package", "footprint"}.issubset(set(entry.get("supports", []))) for entry in evidence):
                evidence_failed = True
                evidence_gate_failures.append(Failure(FailureCategory.BOM_ERROR, "datasheet_evidence_unapproved", f"Approved pin/package/footprint datasheet evidence is missing for {item.ref}", path=item.ref))
        availability_status = Status.FAIL if availability_failed else (Status.BLOCKED if availability_blocked else Status.PASS)
        evidence_status = Status.FAIL if evidence_failed else (Status.BLOCKED if evidence_blocked else Status.PASS)
        self.supplier_availability_report = GateReport("supplier_availability", availability_status, availability_failures, metrics={"provider": provider, "components_checked": len(resolved)}, artifacts=[str(supplier_path)] if supplier_path else [], backend={"provider": provider, "normalized_contract": "supplier_offer_v1"})
        self.datasheet_evidence_report = GateReport("datasheet_evidence", evidence_status, evidence_gate_failures, metrics={"components_checked": len(resolved)}, artifacts=[str(evidence_path)] if evidence_path else [])
        status = Status.FAIL if failures else Status.PASS
        return resolved, GateReport("component_resolution", status, failures, metrics={"resolved": len(resolved), "requested": len(components), "critical_roles": len(critical), "supplier_provider": provider}, artifacts=[str(role_path)], backend={"name": "ComponentResolver", "mutates_database": False})

    @staticmethod
    def serialize(items: list[ResolvedComponent]) -> list[dict[str, Any]]:
        return [asdict(item) for item in items]


def _evidence_is_stale(observed_at: str) -> bool:
    """Return True if the observation timestamp is older than SUPPLIER_EVIDENCE_MAX_AGE_DAYS."""
    try:
        ts = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        return datetime.now(UTC) - ts > timedelta(days=SUPPLIER_EVIDENCE_MAX_AGE_DAYS)
    except (ValueError, AttributeError):
        return True
