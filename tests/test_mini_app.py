import hashlib
import hmac
import json
import unittest
from urllib.parse import urlencode

from src.mini_app import MiniAppAuthorizationError, validate_max_init_data


def signed_init_data(bot_token: str, auth_date: int = 1_700_000_000) -> str:
    values = {
        "auth_date": str(auth_date),
        "query_id": "query-1",
        "user": json.dumps({"id": 42, "first_name": "Анна"}, separators=(",", ":")),
    }
    launch_params = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret_key, launch_params.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class MiniAppAuthorizationTests(unittest.TestCase):
    def test_valid_init_data_returns_user(self) -> None:
        init_data = signed_init_data("test-token")

        user = validate_max_init_data(init_data, "test-token", now=1_700_000_100)

        self.assertEqual(user["id"], 42)

    def test_modified_init_data_is_rejected(self) -> None:
        init_data = signed_init_data("test-token").replace("query-1", "query-2")

        with self.assertRaises(MiniAppAuthorizationError):
            validate_max_init_data(init_data, "test-token", now=1_700_000_100)

if __name__ == "__main__":
    unittest.main()
