import json
import unittest

from src.chairman_protocol import parse_protocol_analysis


def valid_analysis() -> dict:
    return {
        "is_hoa_board_protocol": True,
        "hoa_name": "ТСЖ «Некрасовское»",
        "address": "г. Сызрань, пер. Некрасовский, д. 38",
        "meeting_date": "06.05.2024",
        "quorum_confirmed": True,
        "chairman_election_on_agenda": True,
        "chairman_elected": True,
        "chairman_name": "Мещерякова Е.А.",
        "votes_for_percent": 100,
        "decision_confirmed": True,
        "signature_present": True,
        "rejection_reason": "",
        "confidence": 1,
    }


class ChairmanProtocolTests(unittest.TestCase):
    def test_valid_protocol_is_approved(self) -> None:
        analysis = parse_protocol_analysis(json.dumps(valid_analysis(), ensure_ascii=False))

        self.assertTrue(analysis.approved)
        self.assertEqual(analysis.chairman_name, "Мещерякова Е.А.")

    def test_missing_signature_is_rejected(self) -> None:
        payload = valid_analysis()
        payload["signature_present"] = False

        analysis = parse_protocol_analysis(f"```json\n{json.dumps(payload)}\n```")

        self.assertFalse(analysis.approved)

if __name__ == "__main__":
    unittest.main()
