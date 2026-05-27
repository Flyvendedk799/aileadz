import os
import unittest
from unittest.mock import patch

from run import create_app


class RunConfigTest(unittest.TestCase):
    def test_mysql_credentials_come_from_environment(self):
        env = {
            "FLASK_SECRET_KEY": "test-secret",
            "MYSQL_HOST": "db.example.test",
            "MYSQL_USER": "futurematch",
            "MYSQL_PASSWORD": "from-env",
            "MYSQL_DB": "futurematch_db",
        }

        with patch.dict(os.environ, env, clear=False):
            app = create_app()

        self.assertEqual(app.secret_key, "test-secret")
        self.assertEqual(app.config["MYSQL_HOST"], "db.example.test")
        self.assertEqual(app.config["MYSQL_USER"], "futurematch")
        self.assertEqual(app.config["MYSQL_PASSWORD"], "from-env")
        self.assertEqual(app.config["MYSQL_DB"], "futurematch_db")


if __name__ == "__main__":
    unittest.main()
