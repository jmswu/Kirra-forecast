import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import kirra_monitor as monitor

TZ = ZoneInfo("Australia/Brisbane")


def make_report(alert=True):
    now = datetime(2026, 11, 17, 6, 5, tzinfo=TZ)
    return {
        "generated_at": now,
        "verdict": "REEF WINDOW" if alert else "NO WINDOW",
        "confidence": "high" if alert else "medium",
        "headline": "Reef window Wednesday morning.",
        "antecedent": {
            "fraction": 0.7,
            "longest_run_hours": 30,
            "status_label": "PASS",
        },
        "best_window": (
            {
                "start": now + timedelta(hours=2),
                "end": now + timedelta(hours=6),
                "rating": "REEF",
                "min_swell_m": 0.3,
                "max_swell_m": 0.4,
            }
            if alert else None
        ),
        "alert": (
            {
                "key": "REEF:2026-11-17T08:00",
                "title": "Kirra freedive forecast",
                "subtitle": "REEF WINDOW",
                "message": "Reef window Wednesday morning.",
            }
            if alert else None
        ),
    }


class EmailTests(unittest.TestCase):
    def config(self):
        return {
            "host": "smtp.gmail.com",
            "port": 587,
            "security": "starttls",
            "username": "wu.zhuhua@gmail.com",
            "from": "wu.zhuhua@gmail.com",
            "to": "wu.zhuhua@gmail.com",
        }

    @mock.patch("kirra_monitor.load_smtp_password", return_value="app-password")
    @mock.patch("kirra_monitor.smtplib.SMTP")
    def test_smtp_starttls_and_send(self, smtp_class, _password):
        smtp = smtp_class.return_value.__enter__.return_value
        self.assertTrue(monitor.send_email("Subject", "Body", self.config()))
        smtp.ehlo.assert_called()
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("wu.zhuhua@gmail.com", "app-password")
        smtp.send_message.assert_called_once()

    @mock.patch("kirra_monitor.load_smtp_password", return_value=None)
    @mock.patch("kirra_monitor.smtplib.SMTP")
    def test_missing_password_does_not_connect(self, smtp_class, _password):
        self.assertFalse(monitor.send_email("Subject", "Body", self.config()))
        smtp_class.assert_not_called()

    @mock.patch("kirra_monitor.load_smtp_password", return_value="app-password")
    @mock.patch("kirra_monitor.smtplib.SMTP_SSL")
    def test_smtp_ssl_mode(self, smtp_class, _password):
        config = self.config()
        config["security"] = "ssl"
        smtp = smtp_class.return_value.__enter__.return_value
        self.assertTrue(monitor.send_email("Subject", "Body", config))
        smtp.starttls.assert_not_called()
        smtp.login.assert_called_once()


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.old_data_dir = monitor.DATA_DIR
        monitor.DATA_DIR = Path(self.tempdir.name)

    def tearDown(self):
        monitor.DATA_DIR = self.old_data_dir

    def config(self, email=True):
        return {
            "monitor": {},
            "alerts": {
                "macos": True,
                "email": {"enabled": email, "to": "test@example.com"},
            },
        }

    @mock.patch("kirra_monitor.send_email", return_value=True)
    @mock.patch("kirra_monitor.send_notification", return_value=True)
    def test_alert_sends_both_channels_once(self, notification, email):
        report = make_report(alert=True)
        self.assertTrue(monitor.maybe_notify(report, self.config(), enabled=True))
        self.assertEqual(notification.call_count, 1)
        self.assertEqual(email.call_count, 1)

        # Same window on the next run: no repeat alerts.
        self.assertFalse(monitor.maybe_notify(report, self.config(), enabled=True))
        self.assertEqual(notification.call_count, 1)
        self.assertEqual(email.call_count, 1)

    @mock.patch("kirra_monitor.send_email", return_value=True)
    @mock.patch("kirra_monitor.send_notification", return_value=True)
    def test_no_alert_sends_nothing(self, notification, email):
        report = make_report(alert=False)
        self.assertFalse(monitor.maybe_notify(report, self.config(), enabled=True))
        notification.assert_not_called()
        email.assert_not_called()

    @mock.patch("kirra_monitor.send_email", return_value=True)
    @mock.patch("kirra_monitor.send_notification", return_value=True)
    def test_no_notify_flag_sends_nothing(self, notification, email):
        report = make_report(alert=True)
        self.assertFalse(monitor.maybe_notify(report, self.config(), enabled=False))
        notification.assert_not_called()
        email.assert_not_called()

    @mock.patch("kirra_monitor.send_email", return_value=True)
    @mock.patch("kirra_monitor.send_notification", return_value=True)
    def test_email_can_be_disabled(self, notification, email):
        report = make_report(alert=True)
        self.assertTrue(monitor.maybe_notify(report, self.config(email=False), enabled=True))
        self.assertEqual(notification.call_count, 1)
        email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
