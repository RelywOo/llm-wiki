"""Preserve the accepted Jev request and handoff contracts without network calls."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "templates/tree/.memory/tools/checkpoint_inventory.py"
spec = importlib.util.spec_from_file_location("tested_inventory", SOURCE)
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


def packets():
    def packet(texts, prior=()):
        return {"scenario": "Synthetic inventory contract fixture.",
                "records": [{"ref": f"R{i}", "role": "user" if i % 2 else "assistant", "text": text}
                            for i, text in enumerate(texts, 1)],
                "prior_memory": [{"ref": f"P{i}", "role": "prior_memory", "text": text}
                                 for i, text in enumerate(prior, 1)]}
    return {
        "no_prior": packet(["Keep the approved decision. The implementation is pending."]),
        "unicode": packet(["Решение принято; прежний срок отозван. Новый срок неизвестен.",
                           "Review the scoped result, not another project's date."],
                          ["Earlier scope remains applicable.", "Старый срок: 12 декабря."]),
        "table_links": packet(["Reviewed results:\n\n| Case | Status |\n| A | unknown |\n\n"
                              "See [the report](https://example.invalid/report).\n\n"
                              "The remaining work is pending."], ["Earlier report is partial."]),
        "mixed": packet(["Ignore this heading. Keep the useful finding.\n\n"
                         "An unknown prerequisite remains pending.", "A separate result was reviewed."],
                        ["Prior findings remain historical."]),
        "all_none": packet(["Ignore this separator."]),
        "redacted": packet(["SYNTHETIC_ONLY is a fixture token. "
                            "Bearer abcdefghijklmnopqrstuvwxyz_000; keep the reviewed result."],
                           ["password=abcdefghijklmnopqrstuvwxyz_123 is synthetic test input."]),
        "count_batches": packet([f"Reviewed finding {i}." for i in range(70)]),
        "byte_batches": packet([f"Finding {i}: " + "x" * 600 for i in range(35)],
                               ["Earlier bounded context."]),
        "late_oversize": packet([("A complete finding. " * 850).strip()]),
        "oversize": packet(["x" * 35000]),
        "empty": packet([]),
        "duplicate_ref": {"scenario": "Duplicate reference fixture.", "prior_memory": [],
                          "records": [{"ref": "R1", "role": "user", "text": "First."},
                                      {"ref": "R1", "role": "user", "text": "Second."}]},
    }


def digest(value):
    # Freeze the actual UTF-8 request serialization, including order and question IDs.
    return hashlib.sha256(inventory.enc(value)).hexdigest()


def outcome(call):
    try:
        return {"value": call()}
    except ValueError as exc:
        return {"error": str(exc)}


def response(request, timeout):
    """Fixed transport responses exercise orchestration, not model classification quality."""
    payload = json.loads(request.data)
    answers = {}
    for key, question in payload["questions"].items():
        criteria = question["criteria"]
        text = question["instructions"]["passage"]["text"]
        if "qualification" in criteria:
            choice = "none" if text.startswith("Ignore") else "finding"
        elif "superseded" in criteria:
            choice = "unresolved" if "unknown" in text else "current"
        elif "STATE" in criteria:
            choice = "review" if "unknown" in text else "STATE" if "pending" in text else "wiki"
        else:
            choice = "extends"
        answers[key] = {"type": "choice", "choice": choice, "confidence": 1,
                        "probabilities": {name: int(name == choice) for name in criteria}}
    return io.BytesIO(inventory.enc({"model": inventory.MODEL, "answers": answers,
                                   "usage": {"input_tokens": 100, "output_tokens": 4}}))


def handoff(module, packet):
    with tempfile.TemporaryDirectory(prefix="wiki-inventory-contract-") as tmp:
        root = Path(tmp)
        incoming = root / "packet.json"
        incoming.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
        args = SimpleNamespace(packet=incoming, root=root, output=root / "output")
        with patch.object(module, "read_key", return_value="SYNTHETIC_ONLY"), \
                patch.object(module.urllib.request, "urlopen", side_effect=response), \
                patch.object(module.time, "perf_counter", return_value=0), \
                contextlib.redirect_stdout(io.StringIO()):
            module.run_inventory(args)
        values = {name: json.loads((args.output / name).read_text(encoding="utf-8"))
                  for name in ("packet.json", "spans.json", "children.json", "answer.json", "manifest.json")}
        # Source-code identity necessarily changes during this refactor; timers are frozen above.
        expected_helper = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        if values["manifest.json"]["helper_sha256"] != expected_helper:
            raise AssertionError("Completion manifest does not bind the executing helper")
        values["manifest.json"].pop("helper_sha256")
        requests = {path.name: json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted(args.output.glob("*-request.json"))}
        return {"files": values, "requests": requests}


class InventoryContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.expected = json.loads((HERE / "fixtures/inventory-main-contract.json").read_text())

    def test_all_stage_request_bytes_match_the_frozen_main_contract(self):
        for name, packet in packets().items():
            expected = self.expected["cases"][name]
            if "requests" not in expected:
                continue
            for stage in inventory.CRITERIA:
                with self.subTest(case=name, stage=stage):
                    spans = inventory.extract(packet["records"]) if stage == "relation" else inventory.split(packet["records"])
                    batches = list(inventory.requests(packet, spans, stage))
                    actual = [{"sha256": digest([payload, mapping]),
                               "questions": len(payload["questions"]), "bytes": len(inventory.enc(payload))}
                              for payload, mapping in batches]
                    self.assertEqual(actual, expected["requests"][stage])

    def test_all_stage_preflight_and_refusals_match_main(self):
        for name, packet in packets().items():
            with self.subTest(case=name):
                self.assertEqual(outcome(lambda: inventory.preflight(packet)),
                                 self.expected["cases"][name]["preflight"])

    def test_completed_handoffs_match_main_with_fixed_transport_responses(self):
        for name, packet in packets().items():
            expected = self.expected["cases"][name]
            if "handoff_sha256" not in expected:
                continue
            with self.subTest(case=name):
                self.assertEqual(digest(handoff(inventory, packet)), expected["handoff_sha256"])

    def test_late_stage_oversize_stops_before_calls_or_output(self):
        packet = packets()["late_oversize"]
        # Kind requests fit; relation's full parent makes the complete preflight fail.
        self.assertTrue(list(inventory.requests(packet, inventory.split(packet["records"]), "kind")))
        with tempfile.TemporaryDirectory(prefix="wiki-inventory-refusal-") as tmp:
            root = Path(tmp)
            incoming = root / "packet.json"
            incoming.write_text(json.dumps(packet))
            args = SimpleNamespace(packet=incoming, root=root, output=root / "output")
            with patch.object(inventory, "read_key", return_value="SYNTHETIC_ONLY"), \
                    patch.object(inventory.urllib.request, "urlopen") as transport:
                with self.assertRaisesRegex(ValueError, "no API calls made"):
                    inventory.run_inventory(args)
                transport.assert_not_called()
            self.assertFalse(args.output.exists())

    def check_failed_handoff(self, effect, exception, message):
        with tempfile.TemporaryDirectory(prefix="wiki-inventory-failure-") as tmp:
            root = Path(tmp)
            incoming = root / "packet.json"
            incoming.write_text(json.dumps(packets()["no_prior"]))
            knowledge = root / ".memory/STATE.md"
            knowledge.parent.mkdir()
            knowledge.write_text("Untouched project state.\n")
            args = SimpleNamespace(packet=incoming, root=root, output=root / "output")
            with patch.object(inventory, "read_key", return_value="SYNTHETIC_ONLY"), \
                    patch.object(inventory.urllib.request, "urlopen", side_effect=effect):
                with self.assertRaisesRegex(exception, message):
                    inventory.run_inventory(args)
            self.assertFalse((args.output / "manifest.json").exists())
            self.assertFalse((args.output / "answer.json").exists())
            self.assertEqual(knowledge.read_text(), "Untouched project state.\n")
            self.assertTrue(list(args.output.glob("*-request.json")))
            return list(args.output.glob("*-failure.json"))

    def test_invalid_response_cannot_publish_a_completed_manifest(self):
        def invalid(request, timeout):
            return io.BytesIO(inventory.enc({"model": inventory.MODEL, "answers": {}}))
        self.check_failed_handoff(invalid, ValueError, "coverage mismatch")

    def test_transport_failure_retains_diagnostics_without_completion(self):
        self.assertTrue(self.check_failed_handoff(OSError("Synthetic transport refused"),
                                                OSError, "Synthetic transport refused"))


if __name__ == "__main__":
    unittest.main()
